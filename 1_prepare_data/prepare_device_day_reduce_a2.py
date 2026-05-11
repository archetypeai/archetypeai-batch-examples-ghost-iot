#!/usr/bin/env python3
"""
Hierarchical device-day reduce — pre-fold 24 hourly narratives into super-hour
groups before the final per-device fold. Needed when at least one device's
24-hour pack exceeds the 16 KB / ~4K-token cliff (observed at 1 GB on the
busiest device after Stage B).

Stage A: hourly preds + manifest → super-hour records (group K hours per group)
Stage B: super-hour preds + super-hour manifest → device-day records (fold all
         super-hours per device)

Same shape as prepare_bucket_reduce_a2.py but operates over (home, device)
buckets instead of (home, device, hour) buckets.

Usage:
    # Stage A — pre-fold hours into super-hours (24 devices × 6 = 144 records)
    python 1_prepare_data/prepare_device_day_reduce_a2.py --stage a \\
      --predictions data/predictions_per_device_hour.jsonl \\
      --manifest    data/manifest_per_device_hour.jsonl \\
      --output      data/device_day_reduce_a2.jsonl \\
      --output-manifest data/manifest_device_day_a2.jsonl \\
      --group-size 4

    # ... run batch, download, concat to data/predictions_device_day_a2.jsonl ...

    # Stage B — final fold super-hours into device-day (24 records)
    python 1_prepare_data/prepare_device_day_reduce_a2.py --stage b \\
      --predictions data/predictions_device_day_a2.jsonl \\
      --manifest    data/manifest_device_day_a2.jsonl \\
      --output      data/device_day_reduce.jsonl \\
      --output-manifest data/manifest_device_day.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict


SYSTEM_A = (
    "You are a network security analyst. The text below is a sequence of hourly "
    "summaries for one device covering a contiguous block of hours within a UTC "
    "day. Stay grounded in those summaries — do not invent traffic. Synthesize "
    "patterns across the block."
)

INSTRUCTION_A = (
    "Read the hourly summaries above and write a single paragraph describing "
    "what this device was doing across the block of hours. Cover dominant "
    "protocols, traffic intensity, and anything unusual. 3-6 sentences. Another "
    "call will combine your output with other super-hour summaries to produce "
    "the full daily narrative."
)

SYSTEM_B = (
    "You are a network security analyst writing a daily summary. The text below "
    "is a sequence of super-hour summaries for one device — each already a "
    "synthesis of several hours. Stay grounded in those summaries; do not invent "
    "device behavior. Synthesize patterns across the day."
)

INSTRUCTION_B = (
    "Read the super-hour summaries above and write a single daily narrative for "
    "this device. Cover overall activity arc (when busy vs idle), dominant "
    "protocols and what the device appears to be doing, anything unusual across "
    "the day, and how this device's behavior fits its type. 4-8 sentences."
)

SOFT_CAP_BYTES = 16_384


def load_jsonl(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def hour_label(h: int) -> str:
    return f"{h:02d}:00-{(h + 1) % 24:02d}:00 UTC"


def stage_a(args):
    """Pre-fold: hourly preds → super-hour records (per device)."""
    predictions = load_jsonl(args.predictions)
    manifest = load_jsonl(args.manifest)
    if len(predictions) != len(manifest):
        sys.exit(f"Length mismatch: predictions={len(predictions)}, manifest={len(manifest)}")
    print(f"Loaded {len(predictions)} hourly partials. Grouping into super-hours of {args.group_size}.")

    # Join positionally; group by (home, device)
    by_device: dict[tuple, list[tuple[int, dict, str]]] = defaultdict(list)
    for mf, pr in zip(manifest, predictions):
        key = (mf["home_id"], mf["device_id"])
        by_device[key].append((mf["hour_utc"], mf, pr.get("prediction", "")))

    print(f"Devices: {len(by_device)} (expected 24).")

    n_records = 0
    over_cap = 0
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf_f:
        for key in sorted(by_device.keys()):
            home_id, device_id = key
            hours = sorted(by_device[key], key=lambda t: t[0])
            n_hours = len(hours)
            n_super = math.ceil(n_hours / args.group_size)

            first_mf = hours[0][1]
            home_label = first_mf["home_label"]
            human = first_mf.get("human")
            device_type = first_mf["device_type"]
            device_mac = first_mf["device_mac"]
            date = first_mf["date"]

            for sh_idx in range(n_super):
                lo = sh_idx * args.group_size
                hi = min(lo + args.group_size, n_hours)
                slice_hours = hours[lo:hi]
                n_in_super = hi - lo
                # super-hour time range from first to last hour in this group
                first_hour = slice_hours[0][0]
                last_hour = slice_hours[-1][0]
                sh_range = f"{first_hour:02d}:00-{(last_hour + 1) % 24:02d}:00 UTC"

                owner_str = human if human else "shared"
                prompt = (
                    f"Date: {date} UTC. Home: {home_id} ({home_label}). "
                    f"Owner: {owner_str}. Device: {device_id} (type={device_type}, mac={device_mac}). "
                    f"Super-hour {sh_idx + 1} of {n_super} covering hours {sh_range} ({n_in_super} hourly summaries)."
                )

                preamble = (
                    f"=== HOURLY SUMMARIES ({n_in_super}) FOR {device_id} IN {home_label} "
                    f"ON {date} UTC, SUPER-HOUR {sh_idx + 1} OF {n_super} ({sh_range}) ==="
                )
                pieces = [preamble, ""]
                for (h, mf, pred) in slice_hours:
                    pieces.append(f"--- {hour_label(h)} ---")
                    pieces.append(pred.strip() if pred else "(no prediction)")
                    pieces.append("")
                pieces.append(f"=== END OF SUPER-HOUR {sh_idx + 1} ===")
                data = "\n".join(pieces) + "\n\n" + INSTRUCTION_A
                input_pack_bytes = len(data.encode("utf-8"))
                if input_pack_bytes > SOFT_CAP_BYTES:
                    over_cap += 1
                    print(f"  WARN  {device_id} super-hour {sh_idx + 1}: {input_pack_bytes:,} B > {SOFT_CAP_BYTES:,}")

                record = {
                    "system": SYSTEM_A,
                    "instruction": INSTRUCTION_A,
                    "prompt": prompt,
                    "inputs": [{"type": "text", "format": "plain", "data": data}],
                }
                out_f.write(json.dumps(record) + "\n")

                mf_entry = {
                    "line_index": n_records,
                    "home_id": home_id,
                    "home_label": home_label,
                    "gateway_mac": first_mf["gateway_mac"],
                    "human": human,
                    "device_id": device_id,
                    "device_type": device_type,
                    "device_mac": device_mac,
                    "super_hour_index": sh_idx,
                    "super_hour_count": n_super,
                    "super_hour_range": sh_range,
                    "n_hours_in_super": n_in_super,
                    "hour_lo": first_hour,
                    "hour_hi": last_hour,
                    "input_pack_bytes": input_pack_bytes,
                    "date": date,
                }
                mf_f.write(json.dumps(mf_entry) + "\n")
                n_records += 1

    print(f"\nWrote {n_records} super-hour records to {args.output}")
    print(f"Wrote sidecar manifest: {args.output_manifest}")
    if over_cap:
        print(f"WARN: {over_cap} super-hour records exceeded the {SOFT_CAP_BYTES:,}-byte soft cap.")


def stage_b(args):
    """Final fold: super-hour preds → device-day records (24 records)."""
    predictions = load_jsonl(args.predictions)
    manifest = load_jsonl(args.manifest)
    if len(predictions) != len(manifest):
        sys.exit(f"Length mismatch: predictions={len(predictions)}, manifest={len(manifest)}")
    print(f"Loaded {len(predictions)} super-hour partials.")

    by_device: dict[tuple, list[tuple[int, dict, str]]] = defaultdict(list)
    for mf, pr in zip(manifest, predictions):
        key = (mf["home_id"], mf["device_id"])
        by_device[key].append((mf["super_hour_index"], mf, pr.get("prediction", "")))

    print(f"Devices: {len(by_device)} (expected 24).")

    n_records = 0
    over_cap = 0
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf_f:
        for key in sorted(by_device.keys()):
            home_id, device_id = key
            super_hours = sorted(by_device[key], key=lambda t: t[0])

            first_mf = super_hours[0][1]
            home_label = first_mf["home_label"]
            human = first_mf.get("human")
            device_type = first_mf["device_type"]
            device_mac = first_mf["device_mac"]
            date = first_mf["date"]

            n_super = len(super_hours)
            preamble = (
                f"=== SUPER-HOUR SUMMARIES ({n_super}) FOR {device_id} IN {home_label} "
                f"ON {date} UTC ==="
            )
            pieces = [preamble, ""]
            for (sh_idx, mf, pred) in super_hours:
                sh_range = mf["super_hour_range"]
                pieces.append(f"--- super-hour {sh_idx + 1}/{n_super} ({sh_range}) ---")
                pieces.append(pred.strip() if pred else "(no prediction)")
                pieces.append("")
            pieces.append("=== END OF SUPER-HOUR SUMMARIES ===")
            data = "\n".join(pieces) + "\n\n" + INSTRUCTION_B

            owner_str = human if human else "shared"
            prompt = (
                f"Date: {date} UTC. Home: {home_id} ({home_label}). "
                f"Owner: {owner_str}. Device: {device_id} (type={device_type}, mac={device_mac}). "
                f"{n_super} super-hour narratives below cover the full UTC day."
            )

            input_pack_bytes = len(data.encode("utf-8"))
            if input_pack_bytes > SOFT_CAP_BYTES:
                over_cap += 1
                print(f"  WARN  {device_id} device-day: {input_pack_bytes:,} B > {SOFT_CAP_BYTES:,}")

            record = {
                "system": SYSTEM_B,
                "instruction": INSTRUCTION_B,
                "prompt": prompt,
                "inputs": [{"type": "text", "format": "plain", "data": data}],
            }
            out_f.write(json.dumps(record) + "\n")

            mf_f.write(json.dumps({
                "line_index": n_records,
                "home_id": home_id,
                "home_label": home_label,
                "gateway_mac": first_mf["gateway_mac"],
                "human": human,
                "device_id": device_id,
                "device_type": device_type,
                "device_mac": device_mac,
                "date": date,
                "input_pack_bytes": input_pack_bytes,
                "n_super_hours": n_super,
            }) + "\n")
            n_records += 1

    print(f"\nWrote {n_records} device-day records to {args.output}")
    print(f"Wrote sidecar manifest: {args.output_manifest}")
    if over_cap:
        print(f"WARN: {over_cap} device-day records exceeded the {SOFT_CAP_BYTES:,}-byte soft cap.")


def main():
    parser = argparse.ArgumentParser(description="Hierarchical device-day reduce.")
    parser.add_argument("--stage", choices=["a", "b"], required=True,
                        help="a = pre-fold hours into super-hours; b = fold super-hours into device-day.")
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--output-manifest", required=True)
    parser.add_argument("--group-size", type=int, default=4,
                        help="Stage A only — max hours per super-hour group (default: 4).")
    args = parser.parse_args()

    if args.stage == "a":
        stage_a(args)
    else:
        stage_b(args)


if __name__ == "__main__":
    main()
