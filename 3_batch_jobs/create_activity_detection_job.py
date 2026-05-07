#!/usr/bin/env python3
"""
Create and monitor an Activity Detection batch job on the Archetype AI platform.

Runs Newton's language generation on a JSONL input file to produce natural
language descriptions. For this repo, the two expected inputs are:

    ghost_iot_home_yesterday.jsonl     (1 prompt  — home-level summary)
    ghost_iot_devices_yesterday.jsonl  (N prompts — one per active device)

Usage:
    python create_activity_detection_job.py                                      # default: home
    python create_activity_detection_job.py --file ghost_iot_devices_yesterday.jsonl
    python create_activity_detection_job.py --file ghost_iot_home_yesterday.jsonl --name my-run

    # Section 9: many files in one batch job. Manifest is one filename per line
    # (as written by 2_upload/upload_directory.py).
    python create_activity_detection_job.py --file-list data/uploaded_file_ids.txt --name multihome-per-device-hour

Flow:
    1. POST /v0.5/batch/jobs          -> create batch job
    2. GET  /v0.5/batch/jobs/{id}     -> poll status
    3. GET  /v0.5/batch/jobs/{id}/events -> view logs
"""

import argparse
import json
import os
import time

import requests

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
with open(ENV_PATH) as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v

API_KEY = os.environ["ATAI_API_KEY"]
API_ENDPOINT = os.environ["ATAI_API_ENDPOINT"]
BASE_URL = f"{API_ENDPOINT}/v0.5"
AUTH = {"Authorization": f"Bearer {API_KEY}"}

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


def build_payload(file_ids: list[str], name: str, max_new_tokens: int) -> dict:
    return {
        "name": name,
        "pipeline_type": "batch",
        "pipeline_key": "activity-detection",
        "inputs": {
            "worker.data": [{"file_id": fid} for fid in file_ids],
        },
        "parameters": {
            "worker": {
                "parallelism": 1,
                "config": {
                    "generation": {
                        "do_sample": True,
                        "max_new_tokens": max_new_tokens,
                        "repetition_penalty": 1,
                        "temperature": 0.7,
                        "top_k": 20,
                        "top_p": 0.8,
                    },
                },
            }
        },
    }


def load_file_list(path: str) -> list[str]:
    """Read a manifest of one filename per line (blank lines and # comments ignored)."""
    out: list[str] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    if not out:
        raise SystemExit(f"Manifest is empty: {path}")
    return out


def create_job(payload: dict) -> dict:
    resp = requests.post(
        f"{BASE_URL}/batch/jobs",
        headers={**AUTH, "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def get_job(job_id: str) -> dict:
    resp = requests.get(f"{BASE_URL}/batch/jobs/{job_id}", headers=AUTH)
    resp.raise_for_status()
    return resp.json()


def get_events(job_id: str, limit: int = 100) -> dict:
    resp = requests.get(
        f"{BASE_URL}/batch/jobs/{job_id}/events",
        headers=AUTH,
        params={"limit": limit},
    )
    resp.raise_for_status()
    return resp.json()


def default_job_name(file_id: str) -> str:
    stem = file_id
    if stem.endswith(".jsonl"):
        stem = stem[:-len(".jsonl")]
    stem = stem.replace("_", "-")
    return f"{stem}-activity-detection"


def main():
    parser = argparse.ArgumentParser(description="Create an Activity Detection batch job.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--file", default=None,
                     help="Single file_id (filename as registered on the platform). "
                          "Default: ghost_iot_home_yesterday.jsonl when neither --file nor --file-list is given.")
    src.add_argument("--file-list", default=None,
                     help="Path to a manifest file containing one file_id per line. "
                          "All file_ids are bundled into a single batch job's worker.data array. "
                          "Used by section 9 to fan out across hundreds of small JSONL inputs.")
    parser.add_argument("--name", default=None,
                        help="Job name (default: derived from --file / --file-list)")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Seconds between status polls (default: 10)")
    parser.add_argument("--max-new-tokens", type=int, default=1024,
                        help="Maximum tokens Newton may generate per record (default: 1024). "
                             "Bump to 2048+ for reduce calls that need to synthesize multi-paragraph summaries.")
    parser.add_argument("--filter-pattern", default=None,
                        help="Regex applied to each filename in --file-list. Only matching entries are submitted. "
                             "Use to skip stale entries from prior runs — e.g. --filter-pattern '__c\\d{4}\\.jsonl$' "
                             "keeps only chunked single-record files.")
    parser.add_argument("--max-files-per-job", type=int, default=None,
                        help="If set, split the file list into batches of this size and create one job per batch "
                             "(suffixed -part-NNN-of-NNN). The platform's /batch/jobs endpoint has a request-size "
                             "limit (observed: 37K file_ids → HTTP 413 Payload Too Large). Recommended: 5000.")
    args = parser.parse_args()

    if args.file_list:
        file_ids = load_file_list(args.file_list)
        if args.filter_pattern:
            import re
            pat = re.compile(args.filter_pattern)
            before = len(file_ids)
            file_ids = [f for f in file_ids if pat.search(f)]
            print(f"Filter '{args.filter_pattern}' kept {len(file_ids)}/{before} files.")
            if not file_ids:
                raise SystemExit("Filter matched 0 files. Check the regex.")
        primary = file_ids[0]
        scope_label = f"{len(file_ids)} files via {os.path.basename(args.file_list)}"
        derived_name = os.path.basename(args.file_list).rsplit(".", 1)[0].replace("_", "-")
    else:
        single = args.file or "ghost_iot_home_yesterday.jsonl"
        file_ids = [single]
        primary = single
        scope_label = single
        derived_name = default_job_name(single).removesuffix("-activity-detection")

    name = args.name or f"{derived_name}-activity-detection"

    print("=" * 60)
    print(" Archetype AI Activity Detection Job")
    print("=" * 60)
    print(f" inputs:  {scope_label}")
    if len(file_ids) > 1:
        print(f"          first: {primary}")
        print(f"          last:  {file_ids[-1]}")
    print(f" name:    {name}")
    print()

    # Multi-job split path: too many files for one /batch/jobs POST.
    if args.max_files_per_job and len(file_ids) > args.max_files_per_job:
        chunks = [file_ids[i:i + args.max_files_per_job]
                  for i in range(0, len(file_ids), args.max_files_per_job)]
        print(f"[1/1] Splitting {len(file_ids):,} files into {len(chunks)} jobs of "
              f"≤{args.max_files_per_job:,} each...")
        job_summaries = []
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out_path = os.path.join(repo_dir, "data", f"jobs_{name}.jsonl")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as out_f:
            for i, file_chunk in enumerate(chunks, 1):
                sub_name = f"{name}-part-{i:03d}-of-{len(chunks):03d}"
                sub_payload = build_payload(file_chunk, sub_name, args.max_new_tokens)
                sub_job = create_job(sub_payload)
                summary = {
                    "job_id": sub_job["id"],
                    "name": sub_name,
                    "n_files": len(file_chunk),
                    "first": file_chunk[0],
                    "last": file_chunk[-1],
                    "status": sub_job.get("status", "?"),
                }
                job_summaries.append(summary)
                out_f.write(json.dumps(summary) + "\n")
                print(f"  [{i:>3}/{len(chunks)}] {sub_name}  job_id={sub_job['id']}  files={len(file_chunk):,}")
        print()
        print("=" * 60)
        print(f" Created {len(chunks)} jobs. Saved manifest to:")
        print(f"   {out_path}")
        print()
        print(" Monitor each via the UI or:")
        print(f"   for j in $(jq -r .job_id {out_path}); do")
        print(f"     curl -s -H \"Authorization: Bearer $ATAI_API_KEY\" \\")
        print(f"       \"$ATAI_API_ENDPOINT/v0.5/batch/jobs/$j\" | jq -r '.status'; done")
        print()
        print(" Download all outputs:")
        print(f"   for j in $(jq -r .job_id {out_path}); do")
        print(f"     python 4_download_outputs/download_outputs.py $j outputs/{name}; done")
        print("=" * 60)
        return

    payload = build_payload(file_ids, name, args.max_new_tokens)
    print("[1/3] Creating activity detection job...")
    job = create_job(payload)
    job_id = job["id"]

    print(f"      job_id:   {job_id}")
    print(f"      name:     {job['name']}")
    print(f"      pipeline: {job['pipeline_key']} v{job.get('pipeline_version', '?')}")
    print(f"      status:   {job['status']}")
    print()

    print("[2/3] Monitoring job status...")
    prev_status = None
    while True:
        job = get_job(job_id)
        status = job["status"]
        if status != prev_status:
            print(f"      [{time.strftime('%H:%M:%S')}] {status}")
            prev_status = status
        if status in TERMINAL_STATUSES:
            break
        time.sleep(args.poll_interval)
    print()

    print("[3/3] Job events:")
    events = get_events(job_id)
    for event in reversed(events.get("events", [])):
        level = event["level"]
        msg = event["message"]
        ts = event["created_at"][11:19]
        marker = "!!" if level == "ERROR" else "  "
        print(f"  {marker} [{ts}] {level:<5} {msg}")

    print()
    print("=" * 60)
    print(f" Job {job_id}")
    print(f" Status: {job['status']}")
    if job.get("completed_at"):
        print(f" Completed: {job['completed_at']}")
    if job.get("failed_at"):
        print(f" Failed:    {job['failed_at']}")
    print("=" * 60)
    print()
    print("Next: download outputs with")
    print(f"  python 4_download_outputs/download_outputs.py {job_id} outputs/{name}")


if __name__ == "__main__":
    main()
