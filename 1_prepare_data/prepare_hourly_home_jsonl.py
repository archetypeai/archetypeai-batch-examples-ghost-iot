#!/usr/bin/env python3
"""
MAP-stage prep: produce a JSONL with ONE record per UTC hour of the target day.

Given a source WiFi flow CSV, this script streams the rows, buckets the
matching flows by UTC hour, and writes a 24-line JSONL. Each line asks Newton
to summarize activity for that one hour. The resulting JSONL is tractable to
upload and run through Activity Detection: per-record size stays bounded by
--max-flows-per-hour regardless of how big the source CSV is.

Designed to be paired with prepare_daily_summary_from_hourly_jsonl.py (the
REDUCE stage) which synthesizes the 24 downloaded hourly predictions into one
daily narrative.

Usage:
    python prepare_hourly_home_jsonl.py
    python prepare_hourly_home_jsonl.py --input data/wlan0_ipv4_flows_1gb.csv \\
        --output data/ghost_iot_home_hourly_1gb.jsonl
    python prepare_hourly_home_jsonl.py --max-flows-per-hour 10000

Output: JSONL with 24 records (hours 0..23 UTC). Empty hours emit a short
"no flows" record so line_index maps cleanly to UTC hour.
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
OUTPUT_JSONL = os.path.join(REPO_DIR, "data", "ghost_iot_home_hourly.jsonl")
DEFAULT_MAX_FLOWS_PER_HOUR = 5000
PROGRESS_EVERY = 1_000_000

SYSTEM = (
    "You are a network security analyst reviewing smart-home WiFi traffic. "
    "Flows are bidirectional network conversations between two endpoints (a and b). "
    "Each flow has byte/packet counters for each direction, a transport protocol, "
    "an application-layer protocol, and start/end timestamps."
)

INSTRUCTION = (
    "Analyze the attached flow log and describe what happened on the home "
    "network during this specific hour. Summarize activity level (flow count, "
    "traffic volume), identify the dominant application protocols and what "
    "they suggest the devices were doing, identify the most-active device by "
    "bytes and infer what kind of device it likely is from its protocol mix, "
    "and flag anything unusual. Keep the summary focused on this hour — a "
    "separate call will combine all 24 hourly summaries into a daily narrative."
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


def main():
    parser = argparse.ArgumentParser(description="Build hourly home-level Activity Detection JSONL (map stage).")
    parser.add_argument("--date", default=DEFAULT_DATE, help="Date to summarize, YYYY-MM-DD UTC (default: %(default)s)")
    parser.add_argument("--input", "--source", dest="input", default=INPUT_CSV, help="Path to wlan0 flows CSV")
    parser.add_argument("--output", default=OUTPUT_JSONL, help="Output JSONL path")
    parser.add_argument("--max-flows-per-hour", type=int, default=DEFAULT_MAX_FLOWS_PER_HOUR,
                        help=f"Cap each hour's flow log at this many rows (default: {DEFAULT_MAX_FLOWS_PER_HOUR})")
    args = parser.parse_args()

    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print(f"Date:   {args.date}")
    print(f"Max flows per hour: {args.max_flows_per_hour:,}")
    print()

    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = int(day.timestamp())
    window_end = int((day + timedelta(days=1)).timestamp())

    temp_dir = os.path.dirname(args.output) or "."
    os.makedirs(temp_dir, exist_ok=True)

    # Per-hour state: hour -> {"path": str, "fh": file, "emitted": int, "matched": int, "first": bool}
    hour_state: dict[int, dict] = {}

    def get_bucket(hour: int) -> dict:
        state = hour_state.get(hour)
        if state is not None:
            return state
        fd, path = tempfile.mkstemp(prefix=f"hour_{hour:02d}_", suffix=".flows", dir=temp_dir)
        fh = os.fdopen(fd, "w")
        fh.write(FLOW_LOG_PREAMBLE + "\n\n")
        state = {"path": path, "fh": fh, "emitted": 0, "matched": 0, "first": True}
        hour_state[hour] = state
        return state

    t0 = time.time()
    total = 0
    matched = 0

    try:
        with open(args.input, "r") as fin:
            reader = csv.DictReader(fin)
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
                hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
                bucket = get_bucket(hour)
                bucket["matched"] += 1

                if bucket["emitted"] >= args.max_flows_per_hour:
                    continue
                if bucket["first"]:
                    bucket["first"] = False
                else:
                    bucket["fh"].write("\n")
                bucket["fh"].write(flow_row_to_line(row))
                bucket["emitted"] += 1

                if total % PROGRESS_EVERY == 0:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed > 0 else 0
                    print(f"  Scanned {total:,} rows ({matched:,} matched, {len(hour_state)} hours seen)  "
                          f"{rate/1000:.0f}k rows/s  {elapsed:.1f}s")

        for state in hour_state.values():
            state["fh"].close()

        elapsed = time.time() - t0
        print(f"  Scan complete: {total:,} total rows, {matched:,} matched in {elapsed:.1f}s.")
        print(f"  Hours with flows: {sorted(hour_state.keys())}")
        print()

        # --- Emit one JSONL record per hour 0..23 ------------------------------
        count = 0
        total_jsonl_bytes = 0
        with open(args.output, "w") as out_f:
            for hour in range(24):
                hour_label = f"{hour:02d}:00-{(hour + 1) % 24:02d}:00 UTC"
                state = hour_state.get(hour)
                if state is None:
                    prompt = (
                        f"Date: {args.date} UTC. Scope: home wlan0 interface. "
                        f"Hour: {hour_label}. Flow count: 0. (No flows recorded in this hour.)"
                    )
                    flow_log = FLOW_LOG_PREAMBLE + "\n\n(no flows)"
                else:
                    matched_h = state["matched"]
                    emitted_h = state["emitted"]
                    if matched_h == emitted_h:
                        prompt = (
                            f"Date: {args.date} UTC. Scope: home wlan0 interface. "
                            f"Hour: {hour_label}. Flow count: {matched_h}."
                        )
                    else:
                        prompt = (
                            f"Date: {args.date} UTC. Scope: home wlan0 interface. "
                            f"Hour: {hour_label}. Total flows this hour: {matched_h}. "
                            f"The attached flow log contains the first {emitted_h} of them "
                            f"as a representative sample."
                        )
                    with open(state["path"], "r") as flows_f:
                        flow_log = flows_f.read()

                record = {
                    "system": SYSTEM,
                    "instruction": INSTRUCTION,
                    "prompt": prompt,
                    "inputs": [
                        {"type": "text", "format": "plain", "data": flow_log},
                    ],
                }
                line_out = json.dumps(record) + "\n"
                out_f.write(line_out)
                total_jsonl_bytes += len(line_out)
                count += 1

        print(f"Wrote {count} JSONL records to {args.output}")
        print(f"  Total JSONL size: {fmt_bytes(total_jsonl_bytes)}")
        for hour in range(24):
            state = hour_state.get(hour)
            if state is None:
                print(f"    hour {hour:02d}:  0 matched, 0 emitted")
            else:
                print(f"    hour {hour:02d}: {state['matched']:>8,} matched, {state['emitted']:>5,} emitted")

    finally:
        for state in hour_state.values():
            try:
                if not state["fh"].closed:
                    state["fh"].close()
            except Exception:
                pass
            try:
                os.unlink(state["path"])
            except OSError:
                pass


if __name__ == "__main__":
    main()
