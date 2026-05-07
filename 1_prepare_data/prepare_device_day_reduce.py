#!/usr/bin/env python3
"""
REDUCE stage 1: device-day. Fold 24 hourly narratives -> 1 daily narrative per device.

Inputs:
  --predictions  data/predictions_per_device_hour.jsonl  (joined output from
                 4_download_outputs/extract_predictions.py — keyed by file_id)
  --manifest     data/manifest_per_device_hour.jsonl     (sidecar from
                 1_prepare_data/prepare_per_device_hour_jsonls.py)

Output:
  --output           data/device_day_reduce.jsonl     (24 records — one per device)
  --output-manifest  data/manifest_device_day.jsonl  (24 lines, line_index -> scope)

Each output JSONL record concatenates that device's 24 hourly predictions
into `inputs[0].data` with a `=== HOURLY SUMMARIES (24) ... === ... === END
OF HOURLY SUMMARIES ===` envelope. The instruction lives AFTER the pack so
the model's last context is "synthesize this, don't paraphrase the last hour".

Usage:
    python 1_prepare_data/prepare_device_day_reduce.py \\
      --predictions data/predictions_per_device_hour.jsonl \\
      --manifest data/manifest_per_device_hour.jsonl \\
      --output data/device_day_reduce.jsonl \\
      --output-manifest data/manifest_device_day.jsonl
"""

from __future__ import annotations

import argparse
import json
import os

from topology import REPO_DIR, load_topology

DEFAULT_PREDICTIONS = os.path.join(REPO_DIR, "data", "predictions_per_device_hour.jsonl")
DEFAULT_MANIFEST = os.path.join(REPO_DIR, "data", "manifest_per_device_hour.jsonl")
DEFAULT_OUTPUT = os.path.join(REPO_DIR, "data", "device_day_reduce.jsonl")
DEFAULT_OUTPUT_MANIFEST = os.path.join(REPO_DIR, "data", "manifest_device_day.jsonl")
SOFT_WARN_BYTES = 25 * 1024  # ~25 KB — narrative-heavy threshold for C model

SYSTEM = (
    "You are a network security analyst writing a daily summary. The text below "
    "is a sequence of hourly summaries that have already been written for one "
    "device. Stay grounded in those hourly summaries — do not invent device "
    "behavior or flow details that are not stated. Synthesize patterns across "
    "the day rather than restating each hour."
)

INSTRUCTION = (
    "Read the 24 hourly summaries above and write a single daily narrative for "
    "this device. Cover overall activity arc (when the device was busy vs idle), "
    "dominant protocols and what the device appears to be doing, anything that "
    "looked unusual across the day, and how this device's behavior fits its "
    "type. Aim for 4-8 sentences."
)


def load_predictions(path: str) -> dict[int, str]:
    """line_index -> prediction text. Stage B emits a single multi-record JSONL,
    so all 576 predictions share one file_id and we key purely by line_index."""
    out: dict[int, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            li = r.get("line_index")
            if li is None:
                continue
            out[li] = r.get("prediction", "")
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


def hour_label(h: int) -> str:
    return f"{h:02d}:00-{(h + 1) % 24:02d}:00 UTC"


def build_hourly_pack(device_label: str, home_label: str, date: str,
                      hourly: list[tuple[int, str]]) -> str:
    parts = [f"=== HOURLY SUMMARIES ({len(hourly)}) FOR {device_label} IN {home_label} ON {date} UTC ==="]
    for hour, pred in hourly:
        parts.append("")
        parts.append(f"--- {hour_label(hour)} ---")
        parts.append(pred.strip() if pred else "(no prediction)")
    parts.append("")
    parts.append("=== END OF HOURLY SUMMARIES ===")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Build device-day reduce JSONL (576 hourly -> 24 records).")
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--output-manifest", default=DEFAULT_OUTPUT_MANIFEST)
    parser.add_argument("--date", default=None, help="Override date label (default: read from manifest)")
    args = parser.parse_args()

    topo = load_topology()
    predictions = load_predictions(args.predictions)
    manifest = load_manifest(args.manifest)
    if not manifest:
        raise SystemExit(f"Manifest is empty: {args.manifest}")
    date = args.date or manifest[0].get("date") or "unknown"

    # Group manifest by (home_id, device_id) -> sorted list of (hour, prediction)
    grouped: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for entry in manifest:
        key = (entry["home_id"], entry["device_id"])
        pred = predictions.get(entry["line_index"], "")
        grouped.setdefault(key, []).append((entry["hour_utc"], pred))
    for k in grouped:
        grouped[k].sort(key=lambda x: x[0])

    # Iterate in topology order so output line_index is deterministic.
    print(f"Building device-day reduce records for {len(topo.all_devices)} devices...")
    print()
    n = 0
    n_warn = 0
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf:
        for home in topo.homes:
            for dev in home.all_devices:
                hourly_pairs = grouped.get((home.home_id, dev.device_id), [])
                pack = build_hourly_pack(
                    device_label=f"{dev.device_id} (type={dev.type}, mac={dev.mac})",
                    home_label=f"{home.home_id} ({home.label})",
                    date=date,
                    hourly=hourly_pairs,
                )

                owner_str = dev.owner if dev.owner is not None else "shared"
                prompt = (
                    f"Date: {date} UTC. Home: {home.home_id} ({home.label}). "
                    f"Owner: {owner_str}. Device: {dev.device_id} (type={dev.type}, mac={dev.mac}). "
                    f"Hourly narratives below cover the full UTC day."
                )

                # Pack first, instruction trailing -> instruction is the last context.
                data_field = pack + "\n\n" + INSTRUCTION

                size = len(data_field.encode("utf-8"))
                if size > SOFT_WARN_BYTES:
                    n_warn += 1
                    print(f"  WARN  {dev.device_id:<25}  pack size {size:,} B exceeds {SOFT_WARN_BYTES:,} soft threshold")

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
                    "gateway_mac": home.gateway_mac,
                    "human": dev.owner,
                    "device_id": dev.device_id,
                    "device_type": dev.type,
                    "device_mac": dev.mac,
                    "date": date,
                    "input_pack_bytes": size,
                    "n_hourly_inputs": len(hourly_pairs),
                }) + "\n")
                n += 1

    print()
    print(f"Wrote {n} records to {args.output}")
    print(f"Wrote sidecar manifest: {args.output_manifest}")
    if n_warn:
        print(f"WARN: {n_warn} records exceeded the {SOFT_WARN_BYTES:,}-byte soft threshold "
              f"— consider truncating the longest hourly summaries before submission.")


if __name__ == "__main__":
    main()
