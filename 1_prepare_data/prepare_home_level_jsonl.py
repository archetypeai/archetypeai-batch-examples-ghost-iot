#!/usr/bin/env python3
"""
Prepare a single-line JSONL prompt containing the home's raw WiFi flow log
for the target UTC day (default 2019-10-19) for Newton's Activity Detection
pipeline.

Streaming design: the source CSV is read row by row, and filtered flow lines
are written directly to a temp file. Memory footprint is constant regardless
of input file size — this script handles multi-GB CSVs fine.

Usage:
    python prepare_home_level_jsonl.py
    python prepare_home_level_jsonl.py --date 2019-10-19 --source data/wlan0_ipv4_flows_1gb.csv
    python prepare_home_level_jsonl.py --max-flows 100000   # cap flow log size

Output: data/ghost_iot_home_yesterday.jsonl (exactly 1 line)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

DEFAULT_DATE = "2019-10-19"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_CSV = os.path.join(REPO_DIR, "data", "wlan0_ipv4_flows_db.csv")
OUTPUT_JSONL = os.path.join(REPO_DIR, "data", "ghost_iot_home_yesterday.jsonl")
PROGRESS_EVERY = 1_000_000  # rows

SYSTEM = (
    "You are a network security analyst reviewing smart-home WiFi traffic. "
    "Flows are bidirectional network conversations between two endpoints (a and b). "
    "Each flow has byte/packet counters for each direction, a transport protocol, "
    "an application-layer protocol, and start/end timestamps."
)

INSTRUCTION = (
    "Analyze the attached flow log and describe what happened on the home "
    "network on this day. Summarize the overall activity level (flow count, "
    "total traffic volume, time span), identify the dominant application "
    "protocols and what they suggest the devices were doing, call out the peak "
    "hour or any notable temporal pattern, identify the most-active device by "
    "bytes and infer what kind of device it likely is (phone, laptop, smart "
    "speaker, IoT sensor, etc.) from its protocol mix, and flag anything unusual."
)

FLOW_LOG_PREAMBLE = (
    "Flow log fields (pipe-separated): "
    "time_utc|mac_a|mac_b|prot|tran|port_a|port_b|bytes_a|bytes_b|pkts_a|pkts_b. "
    "Transport: 6=TCP, 17=UDP, 1=ICMP, 58=ICMPv6, 2=IGMP."
)


def fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def flow_row_to_line(r: dict) -> str:
    t = datetime.fromtimestamp(int(r["ts_start"]), tz=timezone.utc).strftime("%H:%M:%S")
    return (
        f"{t}|{r['mac_a']}|{r['mac_b']}|{r['prot']}|{r['tran_prot']}|"
        f"{r['port_a']}|{r['port_b']}|{r['bytes_a']}|{r['bytes_b']}|"
        f"{r['packets_a']}|{r['packets_b']}"
    )


def stream_filter(input_csv: str, date_str: str, max_flows: int | None):
    """Stream the CSV, yield (flow_text_line, original_row) for rows on the target date."""
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = int(day.timestamp())
    window_end = int((day + timedelta(days=1)).timestamp())

    total = 0
    matched = 0
    emitted = 0
    t0 = time.time()

    with open(input_csv, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            try:
                ts = int(row["ts_start"])
            except (TypeError, ValueError, KeyError):
                continue
            if not (window_start <= ts < window_end):
                continue
            if not row.get("mac_a") or not row.get("mac_b"):
                continue
            matched += 1
            if max_flows is not None and emitted >= max_flows:
                # Still count remaining matched rows for accurate reporting
                continue
            yield flow_row_to_line(row), row
            emitted += 1

            if total % PROGRESS_EVERY == 0:
                elapsed = time.time() - t0
                rate = total / elapsed if elapsed > 0 else 0
                print(f"  Scanned {total:,} rows ({matched:,} matched, {emitted:,} emitted)  "
                      f"{rate/1000:.0f}k rows/s  {elapsed:.1f}s")

    elapsed = time.time() - t0
    print(f"  Scan complete: {total:,} total rows, {matched:,} matched, {emitted:,} emitted in {elapsed:.1f}s.")


def main():
    parser = argparse.ArgumentParser(description="Build home-level Activity Detection JSONL (streaming).")
    parser.add_argument("--date", default=DEFAULT_DATE, help="Date to summarize, YYYY-MM-DD UTC (default: %(default)s)")
    parser.add_argument("--input", "--source", dest="input", default=INPUT_CSV, help="Path to wlan0 flows CSV")
    parser.add_argument("--output", default=OUTPUT_JSONL, help="Output JSONL path")
    parser.add_argument("--max-flows", type=int, default=None,
                        help="Cap flow log at this many lines (default: no cap — all matching flows included)")
    args = parser.parse_args()

    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print(f"Date:   {args.date}")
    if args.max_flows:
        print(f"Max flows: {args.max_flows:,}")
    print()

    # Pass 1 (streaming): write flow lines to a temp file. Memory stays flat.
    flow_count = 0
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".flows", delete=False,
        dir=os.path.dirname(args.output) or None,
    ) as tmp:
        tmp_path = tmp.name
        tmp.write(FLOW_LOG_PREAMBLE + "\n\n")
        first = True
        for line, _ in stream_filter(args.input, args.date, args.max_flows):
            if first:
                first = False
            else:
                tmp.write("\n")
            tmp.write(line)
            flow_count += 1

    flow_log_size = os.path.getsize(tmp_path)
    print(f"Flow log written to temp file: {fmt_bytes(flow_log_size)} ({flow_count:,} flows).")

    # Build context-only prompt
    suffix = "" if flow_count else " (No flows recorded on this date.)"
    prompt = (
        f"Date: {args.date} UTC. Scope: home wlan0 interface. "
        f"Flow count: {flow_count}.{suffix}"
    )

    # Stream the temp file into the JSONL record
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(tmp_path, "r") as flows_f, open(args.output, "w") as out_f:
        flow_log = flows_f.read()
        record = {
            "system": SYSTEM,
            "instruction": INSTRUCTION,
            "prompt": prompt,
            "inputs": [
                {"type": "text", "format": "plain", "data": flow_log},
            ],
        }
        out_f.write(json.dumps(record) + "\n")

    os.unlink(tmp_path)

    final_size = os.path.getsize(args.output)
    print()
    print(f"Wrote 1 JSONL record to {args.output}")
    print(f"  prompt length : {len(prompt):,} chars")
    print(f"  inputs[0].data: {fmt_bytes(flow_log_size)} ({flow_count:,} flows)")
    print(f"  JSONL file    : {fmt_bytes(final_size)}")
    print()
    print("--- prompt ---")
    print(prompt)


if __name__ == "__main__":
    main()
