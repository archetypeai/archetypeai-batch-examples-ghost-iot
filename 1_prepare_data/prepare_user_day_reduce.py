#!/usr/bin/env python3
"""
REDUCE stage 2a: user-day. Fold per-user device-day narratives -> 1 narrative per human.

Inputs:
  --predictions  data/predictions_device_day.jsonl   (joined output from
                 4_download_outputs/extract_predictions.py over the device-day
                 batch outputs — keyed by file_id OR by line_index)
  --manifest     data/manifest_device_day.jsonl     (sidecar from
                 prepare_device_day_reduce.py)

Output:
  --output           data/user_day_reduce.jsonl    (6 records — one per human)
  --output-manifest  data/manifest_user_day.jsonl

Topology drives the per-user device list (personal devices only — shared
devices like smart_speaker and thermostat are NOT attributed to any user;
they're folded into the house-level summary instead).

Stage 2 (device-day) emits a single 24-record JSONL through one batch job, so
the downloaded output is a single multi-line file. extract_predictions.py
keys it by file_id when one is recoverable; otherwise the predictions JSONL
can be a sequence of {line_index, prediction} records that we join against
manifest_device_day's line_index.

Usage:
    python 1_prepare_data/prepare_user_day_reduce.py \\
      --predictions data/predictions_device_day.jsonl \\
      --manifest data/manifest_device_day.jsonl \\
      --output data/user_day_reduce.jsonl \\
      --output-manifest data/manifest_user_day.jsonl
"""

from __future__ import annotations

import argparse
import json
import os

from topology import REPO_DIR, load_topology

DEFAULT_PREDICTIONS = os.path.join(REPO_DIR, "data", "predictions_device_day.jsonl")
DEFAULT_MANIFEST = os.path.join(REPO_DIR, "data", "manifest_device_day.jsonl")
DEFAULT_OUTPUT = os.path.join(REPO_DIR, "data", "user_day_reduce.jsonl")
DEFAULT_OUTPUT_MANIFEST = os.path.join(REPO_DIR, "data", "manifest_user_day.jsonl")
SOFT_WARN_BYTES = 25 * 1024

SYSTEM = (
    "You are a network security analyst writing a daily summary for one person, "
    "based on what their personal devices did. The text below contains daily "
    "narratives for each of this person's devices. Stay grounded in those "
    "device-level summaries — do not invent activity that isn't stated. Your "
    "job is to combine the per-device picture into a per-person picture."
)

INSTRUCTION = (
    "Read the per-device daily summaries above and write a single narrative "
    "describing what this person did across their devices today. Cover when "
    "they were active, what activities the device mix suggests (commuting, "
    "working, browsing, watching media, etc), and any anomalies. Aim for "
    "4-8 sentences."
)


def load_predictions_keyed(path: str, manifest: list[dict]) -> dict[int, str]:
    """line_index -> prediction. Accepts JSONL with either file_id or line_index keys."""
    out: dict[int, str] = {}
    by_line = {entry["line_index"]: entry for entry in manifest}
    have_line_index_key = False
    have_file_id_key = False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            li = r.get("line_index")
            if li is not None:
                have_line_index_key = True
                out[li] = r.get("prediction", "")
            else:
                # No line_index — drop on the floor and warn later. (Only
                # device-day predictions are expected; they always have one.)
                pass
            if "file_id" in r:
                have_file_id_key = True
    if not out:
        raise SystemExit(f"No predictions parsed from {path}")
    if have_file_id_key and not have_line_index_key:
        raise SystemExit(
            f"Predictions in {path} are keyed by file_id but device-day stage uses "
            f"line_index. Re-run extract_predictions.py and ensure line_index is preserved."
        )
    _ = by_line  # placeholder for symmetry; unused
    return out


def load_manifest(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def main():
    parser = argparse.ArgumentParser(description="Build user-day reduce JSONL (24 device-day -> 6 records).")
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--output-manifest", default=DEFAULT_OUTPUT_MANIFEST)
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    topo = load_topology()
    manifest = load_manifest(args.manifest)
    predictions = load_predictions_keyed(args.predictions, manifest)
    date = args.date or manifest[0].get("date") or "unknown"

    # Build (home_id, device_id) -> prediction
    by_dev: dict[tuple[str, str], str] = {}
    for entry in manifest:
        pred = predictions.get(entry["line_index"], "")
        by_dev[(entry["home_id"], entry["device_id"])] = pred

    n = 0
    n_warn = 0
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    print(f"Building user-day reduce records for {len(topo.all_humans)} humans...")
    print()
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf:
        for home in topo.homes:
            for human in home.humans:
                # Personal devices only — exclude shared devices.
                packs: list[str] = [
                    f"=== DEVICE-DAY SUMMARIES FOR {human.name} ({len(human.devices)} personal devices) "
                    f"IN {home.label} ON {date} UTC ==="
                ]
                for dev in human.devices:
                    pred = by_dev.get((home.home_id, dev.device_id), "")
                    packs.append("")
                    packs.append(f"--- {dev.device_id} (type={dev.type}, mac={dev.mac}) ---")
                    packs.append(pred.strip() if pred else "(no prediction)")
                packs.append("")
                packs.append("=== END OF DEVICE-DAY SUMMARIES ===")
                pack = "\n".join(packs)

                prompt = (
                    f"Date: {date} UTC. Home: {home.home_id} ({home.label}). "
                    f"Person: {human.name}. Personal devices: "
                    + ", ".join(f"{d.device_id} ({d.type})" for d in human.devices)
                    + "."
                )

                data_field = pack + "\n\n" + INSTRUCTION
                size = len(data_field.encode("utf-8"))
                if size > SOFT_WARN_BYTES:
                    n_warn += 1
                    print(f"  WARN  {human.name:<8} pack {size:,} B > {SOFT_WARN_BYTES:,}")

                record = {
                    "system": SYSTEM,
                    "instruction": INSTRUCTION,
                    "prompt": prompt,
                    "inputs": [{"type": "text", "format": "plain", "data": data_field}],
                }
                out_f.write(json.dumps(record) + "\n")

                mf.write(json.dumps({
                    "line_index": n,
                    "home_id": home.home_id,
                    "home_label": home.label,
                    "human": human.name,
                    "device_ids": [d.device_id for d in human.devices],
                    "date": date,
                    "input_pack_bytes": size,
                }) + "\n")
                n += 1

    print()
    print(f"Wrote {n} records to {args.output}")
    print(f"Wrote sidecar manifest: {args.output_manifest}")
    if n_warn:
        print(f"WARN: {n_warn} records exceeded the soft threshold.")


if __name__ == "__main__":
    main()
