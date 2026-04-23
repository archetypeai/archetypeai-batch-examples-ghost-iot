#!/usr/bin/env python3
"""
Pretty-print Activity Detection outputs for the GHOST-IoT demo.

Reads the JSONL prediction files under an outputs directory and prints each
prediction as a titled paragraph. If --input is given (the original JSONL sent
to the job), each prediction is paired with the prompt that produced it so
per-device narratives are labeled with their MAC.

Usage:
    python view_results.py outputs/ghost-iot-home-yesterday-activity-detection
    python view_results.py outputs/ghost-iot-devices-yesterday-activity-detection \\
        --input data/ghost_iot_devices_yesterday.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def load_predictions(outputs_dir: str) -> list[dict]:
    """Read all pred_*.jsonl files in outputs_dir, merged and sorted by line_index."""
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


def load_inputs(input_jsonl: str) -> list[dict]:
    with open(input_jsonl, "r") as f:
        return [json.loads(line) for line in f if line.strip()]


def extract_mac(prompt: str) -> str | None:
    m = re.search(r"Device MAC:\s*([0-9a-fA-F]+)", prompt)
    return m.group(1) if m else None


def main():
    parser = argparse.ArgumentParser(description="Pretty-print Activity Detection outputs.")
    parser.add_argument("outputs_dir", help="Directory containing downloaded prediction JSONL files")
    parser.add_argument("--input", default=None,
                        help="Optional: original input JSONL, to label predictions by MAC/prompt")
    parser.add_argument("--show-prompt", action="store_true",
                        help="Also print the original prompt above each prediction")
    args = parser.parse_args()

    predictions = load_predictions(args.outputs_dir)
    inputs = load_inputs(args.input) if args.input else None

    print(f"Loaded {len(predictions)} prediction(s) from {args.outputs_dir}")
    if inputs is not None:
        print(f"Paired with {len(inputs)} input record(s) from {args.input}")
    print()

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
