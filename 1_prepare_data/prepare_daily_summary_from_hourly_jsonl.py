#!/usr/bin/env python3
"""
REDUCE-stage prep: build a 1-line JSONL that asks Newton to synthesize a daily
narrative from 24 hourly predictions produced by the map stage.

Pair the hourly input file (one record per hour, each labeled in its prompt)
with the downloaded predictions (one per line_index) to build a single prompt
that says: "here are 24 hourly summaries for 2019-10-19 UTC, produce a daily
narrative." Output is tiny — the reduce stage never risks overflowing context.

Usage:
    python prepare_daily_summary_from_hourly_jsonl.py \\
        --hourly-input data/ghost_iot_home_hourly_1gb.jsonl \\
        --predictions outputs/ghost-iot-home-hourly-1gb/inp_*.jsonl \\
        --output data/ghost_iot_home_daily_from_hourly_1gb.jsonl

If --predictions is a directory, every *.jsonl inside it is concatenated
(lines sorted by line_index).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEM = (
    "You are a network security analyst reviewing smart-home WiFi traffic. "
    "You have been given 24 hour-by-hour summaries of activity on one UTC day. "
    "Your job is to synthesize them into a single coherent daily narrative."
)

INSTRUCTION = (
    "Read the 24 hourly summaries attached as extra context. Each one describes "
    "activity on the home wlan0 interface during a one-hour window of the target "
    "date. Produce a single daily summary that: "
    "(1) describes the overall activity arc of the day (when it was quiet, when "
    "it peaked, any notable bursts or lulls); "
    "(2) identifies protocols and devices that were dominant across the day, "
    "and whether they were concentrated in specific hours or continuous; "
    "(3) highlights anything unusual or worth a security analyst's attention; "
    "(4) does not just concatenate the hourly summaries — integrate the insights. "
    "Assume the hourly summaries are reliable but may be slightly noisy on the "
    "fine-grained details; focus on the patterns that span multiple hours."
)

HOUR_RE = re.compile(r"Hour:\s*(\d{2}):00-(\d{2}):00 UTC")
DATE_RE = re.compile(r"Date:\s*(\d{4}-\d{2}-\d{2})\s*UTC")


def load_jsonl(path: str) -> list[dict]:
    with open(path, "r") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_predictions(predictions_arg: str) -> list[dict]:
    if os.path.isdir(predictions_arg):
        paths = sorted(glob.glob(os.path.join(predictions_arg, "*.jsonl")))
    else:
        paths = sorted(glob.glob(predictions_arg))
    if not paths:
        raise SystemExit(f"No prediction files matched: {predictions_arg}")
    out: list[dict] = []
    for p in paths:
        out.extend(load_jsonl(p))
    out.sort(key=lambda r: r.get("line_index", 0))
    return out


def extract_hour_label(prompt: str) -> str | None:
    m = HOUR_RE.search(prompt)
    if m:
        return f"{m.group(1)}:00-{m.group(2)}:00 UTC"
    return None


def extract_date(prompt: str) -> str | None:
    m = DATE_RE.search(prompt)
    return m.group(1) if m else None


def main():
    parser = argparse.ArgumentParser(description="Reduce hourly predictions into a daily-summary prompt.")
    parser.add_argument("--hourly-input", required=True,
                        help="The original map-stage JSONL (one record per hour, each prompt labeled with Hour:)")
    parser.add_argument("--predictions", required=True,
                        help="Path or glob for the downloaded hourly-prediction JSONL files, OR a directory")
    parser.add_argument("--output", required=True, help="Output JSONL path (1 line)")
    args = parser.parse_args()

    hourly_inputs = load_jsonl(args.hourly_input)
    predictions = load_predictions(args.predictions)

    if len(predictions) != len(hourly_inputs):
        print(
            f"WARNING: prediction count ({len(predictions)}) != hourly input count "
            f"({len(hourly_inputs)}). Will pair by line_index where possible.",
            file=sys.stderr,
        )

    # Build an index_by_line lookup from predictions
    pred_by_idx = {p.get("line_index", i): p for i, p in enumerate(predictions)}

    date_str = None
    lines: list[str] = []
    for idx, input_rec in enumerate(hourly_inputs):
        prompt = input_rec.get("prompt", "")
        hour_label = extract_hour_label(prompt) or f"(hour slot {idx})"
        if date_str is None:
            date_str = extract_date(prompt)

        pred = pred_by_idx.get(idx)
        if pred is None:
            summary = "(no prediction available)"
        else:
            summary = (pred.get("prediction") or "").strip()
            if pred.get("error"):
                summary = f"(error: {pred['error']})"
            if not summary:
                summary = "(empty prediction — model produced no output for this hour)"

        lines.append(f"=== Hour {hour_label} ===\n{summary}")

    joined = "\n\n".join(lines)
    header = (
        f"The 24 hourly summaries below were produced by a prior Activity "
        f"Detection call, one per UTC hour of {date_str or 'the target day'}. "
        f"They are listed in chronological order (00:00 → 23:59 UTC)."
    )
    hourly_pack = header + "\n\n" + joined

    prompt = (
        f"Date: {date_str or 'unknown'} UTC. Scope: home wlan0 interface. "
        f"Input: 24 hourly summaries (attached as extra context). "
        f"Produce a single daily narrative synthesizing them."
    )

    record = {
        "system": SYSTEM,
        "instruction": INSTRUCTION,
        "prompt": prompt,
        "inputs": [
            {"type": "text", "format": "plain", "data": hourly_pack},
        ],
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as out_f:
        out_f.write(json.dumps(record) + "\n")

    out_size = os.path.getsize(args.output)
    print(f"Paired {len(hourly_inputs)} hourly inputs with {len(predictions)} predictions.")
    print(f"Wrote 1 JSONL record to {args.output} ({out_size:,} bytes)")
    print()
    print(f"Hourly pack size:   {len(hourly_pack):,} chars")
    print(f"Prompt:             {prompt}")
    print()
    print("--- first 2 hour labels ---")
    for l in lines[:2]:
        preview = l.split("\n", 1)
        header_line = preview[0]
        body = preview[1][:200] if len(preview) > 1 else ""
        print(header_line)
        print(body + ("..." if len(preview) > 1 and len(preview[1]) > 200 else ""))
        print()


if __name__ == "__main__":
    main()
