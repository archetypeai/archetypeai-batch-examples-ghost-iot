#!/usr/bin/env python3
"""Run the section 8 multi-home map-reduce end to end: chunks -> device-hour ->
device-day -> user-day + house-day.

Each stage builds its input with the 1_prepare_data/ script, then runs the same
upload -> batch job -> download steps as the single-file examples, one job per
stage. One job per stage keeps every output's line_index equal to its input
line, so each stage joins to its manifest by position (no split, no renumbering).

Usage:
    python run_multihome.py --csv data/wifi_flows_multihome_12mb.csv --run mh12
    python run_multihome.py --csv ... --run mh12 --model-variant newton/c:2.6.0-8b-fp8-base
    python run_multihome.py --csv ... --run quick --homes home_a   # 1 home: a third of the records
    python run_multihome.py --csv ... --run mh12 --background     # detached; tail -f outputs/logs/run_multihome.log

Every artifact lands in data/multihome/<run>/. A stage whose predictions file
already exists is skipped, and each job id is saved in jobs.json as soon as the
job is created, so rerunning the same command resumes after a failure and waits
on a job that is still running rather than starting it again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

from background import add_background_flag, maybe_detach

REPO = os.path.dirname(os.path.abspath(__file__))
PREP = os.path.join(REPO, "1_prepare_data")
PY = sys.executable
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def run(cmd: list[str]) -> str:  # prep and job scripts inherit os.environ, incl. TOPOLOGY_JSON
    print("$ " + " ".join(os.path.relpath(c, REPO) if c.startswith(REPO) else c for c in cmd), flush=True)
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"command failed ({out.returncode}):\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}")
    return out.stdout


def wait_for(job_id: str) -> str:
    """Poll a job until it ends; return its status."""
    from atai_api import AUTH, BASE_URL
    import requests
    last = None
    while True:
        resp = requests.get(f"{BASE_URL}/batch/jobs/{job_id}", headers=AUTH, timeout=60)
        resp.raise_for_status()
        job = resp.json()
        if job["status"] != last:
            print(f"  {time.strftime('%H:%M:%S')} {job_id} {job['status']}", flush=True)
            last = job["status"]
        if job["status"] in TERMINAL:
            return job["status"]
        time.sleep(30)


def batch(src: str, stage: str, run_id: str, max_new_tokens: int, model_variant: str | None) -> list[dict]:
    """Upload one JSONL, run one Activity Detection job on it, return its
    predictions ordered by line_index.

    The job id is saved in jobs.json the moment the job exists, so a rerun after
    the driver stops (closed terminal, reboot) waits on the same job instead of
    starting a new one."""
    jobs_path = os.path.join(os.path.dirname(src), "jobs.json")
    jobs = json.load(open(jobs_path)) if os.path.exists(jobs_path) else {}
    job_id = jobs.get(stage)
    if job_id:
        print(f"  reattaching to {job_id}", flush=True)
        status = wait_for(job_id)
    else:
        digest = hashlib.sha1(open(src, "rb").read()).hexdigest()[:8]
        name = f"{run_id}_{stage}_{digest}.jsonl"  # content-addressed: a rebuilt input gets a new name
        staged = os.path.join(os.path.dirname(src), name)
        shutil.copyfile(src, staged)
        run([PY, os.path.join(REPO, "2_upload/upload_multipart.py"), staged])

        cmd = [PY, "-u", os.path.join(REPO, "3_batch_jobs/create_activity_detection_job.py"),
               "--file", name, "--name", f"{run_id}-{stage}", "--max-new-tokens", str(max_new_tokens),
               "--poll-interval", "30"]
        if model_variant:
            cmd += ["--model-variant", model_variant]
        print("$ " + " ".join(os.path.relpath(c, REPO) if c.startswith(REPO) else c for c in cmd), flush=True)
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = []
        for line in proc.stdout:
            out.append(line)
            m = re.search(r"job_id:\s+(job_\w+)", line)
            if m and not job_id:
                job_id = m.group(1)
                jobs[stage] = job_id
                json.dump(jobs, open(jobs_path, "w"), indent=1)
                print(f"  {stage}: job {job_id}", flush=True)
        if proc.wait() != 0 or not job_id:
            sys.exit(f"{stage}: job script failed:\n{''.join(out)[-2000:]}")
        status = re.search(r"Status: (\w+)", "".join(out)).group(1)
    if status != "COMPLETED":
        sys.exit(f"{stage}: job {job_id} ended {status}. To start this stage over, "
                 f"remove \"{stage}\" from {os.path.relpath(jobs_path, REPO)} and rerun.")

    out_dir = os.path.join(REPO, "outputs", run_id, stage)
    run([PY, os.path.join(REPO, "4_download_outputs/download_outputs.py"), job_id, out_dir])
    preds = []
    for fn in sorted(os.listdir(out_dir)):
        preds += [json.loads(line) for line in open(os.path.join(out_dir, fn)) if line.strip()]
    preds.sort(key=lambda r: r["line_index"])
    n_in = sum(1 for line in open(src) if line.strip())
    errors = [p for p in preds if p.get("error") or not p.get("prediction")]
    print(f"  {stage}: {len(preds)}/{n_in} predictions, {len(errors)} errors (job {job_id})", flush=True)
    if len(preds) != n_in or len(errors) > 0.01 * n_in:
        sys.exit(f"{stage}: too many missing or failed records; see outputs/{run_id}/{stage}/")
    return preds


def write_jsonl(path: str, rows) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="Multi-home flows CSV (generate_synthetic_multihome_csv.py)")
    ap.add_argument("--run", required=True, help="Run id: names the directory, files and jobs")
    ap.add_argument("--max-chunk-bytes", type=int, default=16384, help="Chunk cap (default 16 KB, see §10.6)")
    ap.add_argument("--model-variant", default=None, help="Passed to every batch job (default: platform default)")
    ap.add_argument("--homes", nargs="+", metavar="HOME_ID",
                    help="Only these homes from data/topology.json (home_a, home_b, home_c); flows of other "
                         "homes are skipped. One home is a third of the records in every stage.")
    add_background_flag(ap, default_log="outputs/logs/run_multihome.log")
    a = ap.parse_args()
    maybe_detach(a)

    d = os.path.join(REPO, "data", "multihome", a.run)
    os.makedirs(d, exist_ok=True)
    p = lambda name: os.path.join(d, name)  # noqa: E731

    if a.homes:
        topo = json.load(open(os.path.join(REPO, "data", "topology.json")))
        unknown = set(a.homes) - {h["home_id"] for h in topo["homes"]}
        if unknown:
            sys.exit(f"unknown home id(s): {', '.join(sorted(unknown))}")
        topo["homes"] = [h for h in topo["homes"] if h["home_id"] in a.homes]
        subset = p("topology.json")
        if os.path.exists(subset) and json.load(open(subset)) != topo:
            sys.exit(f"{os.path.relpath(subset, REPO)} has other homes; use a new --run")
        json.dump(topo, open(subset, "w"), indent=1)
        os.environ["TOPOLOGY_JSON"] = subset
    elif os.path.exists(p("topology.json")):
        sys.exit(f"run {a.run} was started with --homes; pass the same --homes to resume it")

    def stage(key: str, build: list[str] | None, src: str, tokens: int) -> str:
        """Build `src` with `build` (a prep command), batch it, write predictions."""
        out = p(f"predictions_{key}.jsonl")
        if os.path.exists(out):
            print(f"[{key}] done already: {os.path.relpath(out, REPO)}")
            return out
        print(f"\n[{key}]", flush=True)
        if build:
            run(build)
        preds = batch(src, key, a.run, tokens, a.model_variant)
        write_jsonl(out, ({"line_index": r["line_index"], "prediction": r.get("prediction") or ""} for r in preds))
        return out

    # 1. Chunk every (device, hour) bucket, then one inference per chunk.
    chunks_dir, manifest = p("per_device_hour"), p("manifest_chunked.jsonl")
    if not os.path.exists(manifest):
        run([PY, f"{PREP}/prepare_per_device_hour_jsonls.py", "--input", a.csv, "--output-dir", chunks_dir,
             "--manifest", manifest, "--max-chunk-bytes", str(a.max_chunk_bytes)])
    entries = [json.loads(line) for line in open(manifest)]
    chunks = p("chunks.jsonl")
    with open(chunks, "w") as f:  # manifest order, so prediction i belongs to entries[i]
        for e in entries:
            f.write(open(os.path.join(chunks_dir, e["file_id"])).readline().rstrip("\n") + "\n")
    chunk_preds = stage("chunks", None, chunks, 1024)
    keyed = p("predictions_chunks_keyed.jsonl")
    write_jsonl(keyed, ({"file_id": e["file_id"], "line_index": 0, "prediction": json.loads(line)["prediction"]}
                        for e, line in zip(entries, open(chunk_preds))))

    # 2. Fold chunks into one narrative per (device, hour): A (3 chunks) -> A2 (4 partials) -> B.
    a_pred = stage("bucket_a", [PY, f"{PREP}/prepare_bucket_reduce.py", "--stage", "a", "--predictions", keyed,
                                "--manifest", manifest, "--output", p("bucket_a.jsonl"),
                                "--output-manifest", p("manifest_bucket_a.jsonl"), "--group-size", "3"],
                   p("bucket_a.jsonl"), 1024)
    a2_pred = stage("bucket_a2", [PY, f"{PREP}/prepare_bucket_reduce_a2.py", "--predictions", a_pred,
                                  "--manifest", p("manifest_bucket_a.jsonl"), "--output", p("bucket_a2.jsonl"),
                                  "--output-manifest", p("manifest_bucket_a2.jsonl"), "--group-size", "4"],
                    p("bucket_a2.jsonl"), 1024)
    hour_pred = stage("device_hour", [PY, f"{PREP}/prepare_bucket_reduce.py", "--stage", "b", "--predictions", a2_pred,
                                      "--manifest", p("manifest_bucket_a2.jsonl"), "--output", p("bucket_b.jsonl"),
                                      "--output-manifest", p("manifest_device_hour.jsonl")],
                      p("bucket_b.jsonl"), 1024)

    # 3. Device-day: 4 hours per super-hour, then super-hours -> day.
    sh_pred = stage("super_hour", [PY, f"{PREP}/prepare_device_day_reduce_a2.py", "--stage", "a",
                                   "--predictions", hour_pred, "--manifest", p("manifest_device_hour.jsonl"),
                                   "--output", p("super_hour.jsonl"), "--output-manifest", p("manifest_super_hour.jsonl"),
                                   "--group-size", "4"],
                    p("super_hour.jsonl"), 1024)
    day_pred = stage("device_day", [PY, f"{PREP}/prepare_device_day_reduce_a2.py", "--stage", "b",
                                    "--predictions", sh_pred, "--manifest", p("manifest_super_hour.jsonl"),
                                    "--output", p("device_day.jsonl"), "--output-manifest", p("manifest_device_day.jsonl")],
                     p("device_day.jsonl"), 2048)

    # 4. Roll device-days up per person and per house.
    for key, script in (("user_day", "prepare_user_day_reduce.py"), ("house_day", "prepare_house_day_reduce.py")):
        stage(key, [PY, f"{PREP}/{script}", "--predictions", day_pred, "--manifest", p("manifest_device_day.jsonl"),
                    "--output", p(f"{key}.jsonl"), "--output-manifest", p(f"manifest_{key}.jsonl")],
              p(f"{key}.jsonl"), 2048)

    print(f"\nDone. View the narratives with:\n"
          f"  python 5_view_results/view_results.py data/multihome/{a.run}/predictions_house_day.jsonl "
          f"--manifest data/multihome/{a.run}/manifest_house_day.jsonl")


if __name__ == "__main__":
    main()
