#!/usr/bin/env python3
"""
REDUCE stage 2b: house-day. Fold all of a home's device-day narratives -> 1 narrative per home.

Inputs are the same as user-day prep (predictions + sidecar manifest from
the device-day batch). The grouping is different: every device in a home
contributes — including shared devices like smart_speaker and thermostat
that user-day skips.

Output:
  --output           data/house_day_reduce.jsonl    (3 records — one per home)
  --output-manifest  data/manifest_house_day.jsonl

Usage:
    python 1_prepare_data/prepare_house_day_reduce.py \\
      --predictions data/predictions_device_day.jsonl \\
      --manifest data/manifest_device_day.jsonl \\
      --output data/house_day_reduce.jsonl \\
      --output-manifest data/manifest_house_day.jsonl
"""

from __future__ import annotations

import argparse
import json
import os

from topology import REPO_DIR, load_topology

DEFAULT_PREDICTIONS = os.path.join(REPO_DIR, "data", "predictions_device_day.jsonl")
DEFAULT_MANIFEST = os.path.join(REPO_DIR, "data", "manifest_device_day.jsonl")
DEFAULT_OUTPUT = os.path.join(REPO_DIR, "data", "house_day_reduce.jsonl")
DEFAULT_OUTPUT_MANIFEST = os.path.join(REPO_DIR, "data", "manifest_house_day.jsonl")
SOFT_WARN_BYTES = 25 * 1024

SYSTEM = (
    "You are a network security analyst writing a daily summary for one home, "
    "based on what every device in that home did. The text below contains "
    "daily narratives for each device — both personal devices owned by "
    "household members and shared devices like smart speakers and thermostats. "
    "Stay grounded in those device-level summaries; do not invent activity that "
    "isn't stated."
)

INSTRUCTION = (
    "Read the per-device daily summaries above and write a single narrative "
    "describing what happened in this home today. Identify the dominant traffic "
    "patterns, when household activity peaked, the role each device played, "
    "and anything unusual at the home level. Aim for 6-10 sentences."
)


def load_predictions_keyed(path: str) -> dict[int, str]:
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
    if not out:
        raise SystemExit(f"No predictions parsed from {path}")
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
    parser = argparse.ArgumentParser(description="Build house-day reduce JSONL (24 device-day -> 3 records).")
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--output-manifest", default=DEFAULT_OUTPUT_MANIFEST)
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    topo = load_topology()
    manifest = load_manifest(args.manifest)
    predictions = load_predictions_keyed(args.predictions)
    date = args.date or manifest[0].get("date") or "unknown"

    # (home_id, device_id) -> prediction
    by_dev: dict[tuple[str, str], str] = {}
    for entry in manifest:
        by_dev[(entry["home_id"], entry["device_id"])] = predictions.get(entry["line_index"], "")

    n = 0
    n_warn = 0
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    print(f"Building house-day reduce records for {len(topo.homes)} homes...")
    print()
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf:
        for home in topo.homes:
            all_devs = list(home.all_devices)
            packs: list[str] = [
                f"=== DEVICE-DAY SUMMARIES FOR {home.label} ({len(all_devs)} devices, "
                f"{len(home.humans)} humans) ON {date} UTC ==="
            ]
            for dev in all_devs:
                owner_str = dev.owner if dev.owner is not None else "shared"
                pred = by_dev.get((home.home_id, dev.device_id), "")
                packs.append("")
                packs.append(f"--- {dev.device_id} (type={dev.type}, owner={owner_str}, mac={dev.mac}) ---")
                packs.append(pred.strip() if pred else "(no prediction)")
            packs.append("")
            packs.append("=== END OF DEVICE-DAY SUMMARIES ===")
            pack = "\n".join(packs)

            prompt = (
                f"Date: {date} UTC. Home: {home.home_id} ({home.label}). "
                f"Gateway: {home.gateway_mac}. "
                f"Members: " + ", ".join(h.name for h in home.humans) + ". "
                f"Total devices: {len(all_devs)} ({sum(len(h.devices) for h in home.humans)} personal "
                f"+ {len(home.shared_devices)} shared)."
            )

            data_field = pack + "\n\n" + INSTRUCTION
            size = len(data_field.encode("utf-8"))
            if size > SOFT_WARN_BYTES:
                n_warn += 1
                print(f"  WARN  {home.label:<8} pack {size:,} B > {SOFT_WARN_BYTES:,}")

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
                "humans": [h.name for h in home.humans],
                "n_devices": len(all_devs),
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
