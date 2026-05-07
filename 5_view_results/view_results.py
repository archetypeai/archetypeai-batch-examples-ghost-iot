#!/usr/bin/env python3
"""
Pretty-print Activity Detection outputs.

Two modes:

  1. Outputs directory + optional input JSONL (original simple/MapReduce flows):
       python view_results.py outputs/ghost-iot-home-yesterday-activity-detection
       python view_results.py outputs/ghost-iot-devices-yesterday-activity-detection \\
           --input data/ghost_iot_devices_yesterday.jsonl

  2. Section 9 multi-home batch — joined predictions JSONL + sidecar manifest:
       python view_results.py data/predictions_per_device_hour.jsonl \\
           --manifest data/manifest_per_device_hour.jsonl
       python view_results.py outputs/multihome-device-day \\
           --manifest data/manifest_device_day.jsonl

The positional argument accepts either a directory of raw output JSONL files
(`inp_*_output.jsonl`) or a joined predictions JSONL produced by
4_download_outputs/extract_predictions.py. When `--manifest` is supplied,
each prediction is labeled with its scope (home, human, device, hour) drawn
from the sidecar.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def load_predictions_from_dir(outputs_dir: str) -> list[dict]:
    paths = sorted(glob.glob(os.path.join(outputs_dir, "*.jsonl")))
    if not paths:
        print(f"No .jsonl files found in {outputs_dir}", file=sys.stderr)
        sys.exit(1)
    records: list[dict] = []
    for p in paths:
        with open(p, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
    records.sort(key=lambda r: r.get("line_index", 0))
    return records


def load_predictions_from_file(jsonl_path: str) -> list[dict]:
    with open(jsonl_path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_inputs(input_jsonl: str) -> list[dict]:
    with open(input_jsonl, "r") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_manifest(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def extract_mac(prompt: str) -> str | None:
    m = re.search(r"Device MAC:\s*([0-9a-fA-F]+)", prompt)
    return m.group(1) if m else None


def manifest_label(entry: dict) -> str:
    parts = []
    if "home_label" in entry:
        parts.append(entry["home_label"])
    elif "home_id" in entry:
        parts.append(entry["home_id"])
    if "human" in entry and entry["human"]:
        parts.append(entry["human"])
    elif "humans" in entry:
        parts.append("(" + ", ".join(entry["humans"]) + ")")
    elif entry.get("human") is None and "device_id" in entry:
        parts.append("shared")
    if "device_id" in entry:
        dt = f" ({entry['device_type']})" if "device_type" in entry else ""
        parts.append(f"{entry['device_id']}{dt}")
    if "hour_utc" in entry:
        h = entry["hour_utc"]
        parts.append(f"{h:02d}:00-{(h + 1) % 24:02d}:00 UTC")
    return " • ".join(parts) if parts else ""


def index_manifest(manifest: list[dict]) -> tuple[dict[str, dict], dict[int, dict]]:
    by_file_id: dict[str, dict] = {}
    by_line_index: dict[int, dict] = {}
    for entry in manifest:
        if "file_id" in entry:
            by_file_id[entry["file_id"]] = entry
        if "line_index" in entry:
            by_line_index[entry["line_index"]] = entry
    return by_file_id, by_line_index


def main():
    parser = argparse.ArgumentParser(description="Pretty-print Activity Detection outputs.")
    parser.add_argument("source",
                        help="Directory of output JSONL files OR a joined predictions JSONL "
                             "(from 4_download_outputs/extract_predictions.py).")
    parser.add_argument("--input", default=None,
                        help="Original input JSONL — used (in non-manifest mode) to label "
                             "predictions with the prompt's MAC.")
    parser.add_argument("--manifest", default=None,
                        help="Sidecar manifest JSONL with scope metadata (home, human, "
                             "device, hour). When supplied, each prediction is labeled by scope.")
    parser.add_argument("--show-prompt", action="store_true",
                        help="Print the original prompt above each prediction (requires --input).")
    args = parser.parse_args()

    if os.path.isdir(args.source):
        predictions = load_predictions_from_dir(args.source)
        source_label = args.source
    elif os.path.isfile(args.source):
        predictions = load_predictions_from_file(args.source)
        source_label = args.source
    else:
        sys.exit(f"Source not found: {args.source}")

    inputs = load_inputs(args.input) if args.input else None
    manifest = load_manifest(args.manifest) if args.manifest else None

    print(f"Loaded {len(predictions)} prediction(s) from {source_label}")
    if inputs is not None:
        print(f"Paired with {len(inputs)} input record(s) from {args.input}")
    if manifest is not None:
        print(f"Joined to {len(manifest)} sidecar entries from {args.manifest}")
    print()

    if manifest is not None:
        by_file_id, by_line_index = index_manifest(manifest)
        # Emit predictions in manifest order so output is grouped by topology
        # (home -> human -> device -> hour).
        # Build prediction lookup
        pred_by_file_id: dict[str, dict] = {}
        pred_by_line_index: dict[int, dict] = {}
        for r in predictions:
            if "file_id" in r:
                pred_by_file_id[r["file_id"]] = r
            if "line_index" in r:
                pred_by_line_index[r["line_index"]] = r

        for entry in manifest:
            label = manifest_label(entry)
            print("=" * 72)
            print(label or "(no scope)")
            print("=" * 72)
            r = None
            if "file_id" in entry and entry["file_id"] in pred_by_file_id:
                r = pred_by_file_id[entry["file_id"]]
            elif "line_index" in entry and entry["line_index"] in pred_by_line_index:
                r = pred_by_line_index[entry["line_index"]]
            if r is None:
                print("(no prediction)")
            else:
                pred = r.get("prediction")
                err = r.get("error")
                if err:
                    print(f"ERROR: {err}")
                elif pred is None or pred == "":
                    print("(empty prediction)")
                else:
                    print(pred.strip())
            print()
        return

    # ----- Legacy mode -----
    for rec in predictions:
        idx = rec.get("line_index", "?")
        pred = rec.get("prediction")
        err = rec.get("error")

        label = f"[{idx}]"
        if inputs is not None and isinstance(idx, int) and 0 <= idx < len(inputs):
            mac = extract_mac(inputs[idx].get("prompt", ""))
            if mac:
                label = f"[{idx}] device mac={mac}"

        print("=" * 72)
        print(label)
        print("=" * 72)

        if args.show_prompt and inputs is not None and isinstance(idx, int) and 0 <= idx < len(inputs):
            print("PROMPT:")
            print(inputs[idx].get("prompt", "").strip())
            print()
            print("PREDICTION:")

        if err:
            print(f"ERROR: {err}")
        elif pred is None:
            print("(no prediction)")
        else:
            print(pred.strip())
        print()


if __name__ == "__main__":
    main()
