#!/usr/bin/env python3
"""
Prepare a JSONL file with one prompt per device active on the target UTC day
(default 2019-10-19) for Newton's Activity Detection pipeline.

Streaming design: single pass over the source CSV. For each matching flow,
the flow text line is appended to per-device temp files (one temp file per
active device). At the end, each temp file is packaged into a JSONL record.
Memory is O(num_devices) — it does NOT scale with CSV size — so this script
handles multi-GB inputs fine.

Disk note: per-device temp files roughly double the filtered data on disk
during processing (each flow line is written to two device temp files, one
for mac_a and one for mac_b). Ensure free disk ~= 2× the size of the target-
day slice.

Usage:
    python prepare_device_level_jsonl.py
    python prepare_device_level_jsonl.py --date 2019-10-19 --source data/wlan0_ipv4_flows_1gb.csv
    python prepare_device_level_jsonl.py --max-flows-per-device 50000  # cap per-device flow log

Output: data/ghost_iot_devices_yesterday.jsonl (one line per active device)
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
OUTPUT_JSONL = os.path.join(REPO_DIR, "data", "ghost_iot_devices_yesterday.jsonl")
PROGRESS_EVERY = 1_000_000  # rows

SYSTEM = (
    "You are a network security analyst reviewing a single smart-home device's "
    "WiFi traffic. Flows are bidirectional network conversations between two "
    "endpoints (a and b). The device under review appears as either a or b in "
    "each flow; the other endpoint is its peer."
)

INSTRUCTION = (
    "Analyze the attached flow log for a single device and describe what it did "
    "on this day. Summarize its activity level (flow count, time span, traffic "
    "volume in each direction), identify the application protocols it used and "
    "the ports it connected to, note how many distinct peers it talked to, and "
    "based on the evidence infer what kind of device this is (phone, laptop, "
    "smart speaker, IoT sensor, router/gateway, etc.) and what it was doing. "
    "Flag anything unusual."
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
    parser = argparse.ArgumentParser(description="Build per-device Activity Detection JSONL (streaming).")
    parser.add_argument("--date", default=DEFAULT_DATE, help="Date to summarize, YYYY-MM-DD UTC (default: %(default)s)")
    parser.add_argument("--input", "--source", dest="input", default=INPUT_CSV, help="Path to wlan0 flows CSV")
    parser.add_argument("--output", default=OUTPUT_JSONL, help="Output JSONL path")
    parser.add_argument("--max-flows-per-device", type=int, default=None,
                        help="Cap each device's flow log at this many lines (default: no cap)")
    args = parser.parse_args()

    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print(f"Date:   {args.date}")
    if args.max_flows_per_device:
        print(f"Max flows per device: {args.max_flows_per_device:,}")
    print()

    # Date window
    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = int(day.timestamp())
    window_end = int((day + timedelta(days=1)).timestamp())

    temp_dir = os.path.dirname(args.output) or "."
    os.makedirs(temp_dir, exist_ok=True)

    # Per-device state: mac -> {"path": str, "fh": file, "count": int}
    device_state: dict[str, dict] = {}

    def get_device(mac: str) -> dict:
        state = device_state.get(mac)
        if state is not None:
            return state
        fd, path = tempfile.mkstemp(prefix=f"dev_", suffix=".flows", dir=temp_dir)
        fh = os.fdopen(fd, "w")
        fh.write(FLOW_LOG_PREAMBLE + "\n\n")
        # count = emitted (written to temp file); matched = all involving flows
        state = {"path": path, "fh": fh, "count": 0, "matched": 0, "first": True}
        device_state[mac] = state
        return state

    # --- Streaming pass ----------------------------------------------------
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
                mac_a = row.get("mac_a")
                mac_b = row.get("mac_b")
                if not mac_a or not mac_b:
                    continue

                matched += 1
                line = flow_row_to_line(row)

                for mac in ({mac_a, mac_b} if mac_a != mac_b else {mac_a}):
                    state = get_device(mac)
                    state["matched"] += 1
                    if args.max_flows_per_device is not None and state["count"] >= args.max_flows_per_device:
                        continue
                    if state["first"]:
                        state["first"] = False
                    else:
                        state["fh"].write("\n")
                    state["fh"].write(line)
                    state["count"] += 1

                if total % PROGRESS_EVERY == 0:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed > 0 else 0
                    print(f"  Scanned {total:,} rows ({matched:,} matched, {len(device_state)} devices)  "
                          f"{rate/1000:.0f}k rows/s  {elapsed:.1f}s")

        # Close all temp file handles so we can re-read them.
        for state in device_state.values():
            state["fh"].close()

        elapsed = time.time() - t0
        print(f"  Scan complete: {total:,} total rows, {matched:,} matched, "
              f"{len(device_state)} active devices in {elapsed:.1f}s.")

        # --- Build JSONL ---------------------------------------------------
        # Sort devices by flow count (descending) so busiest devices come first.
        devices_sorted = sorted(device_state.items(), key=lambda kv: kv[1]["count"], reverse=True)

        count = 0
        total_jsonl_bytes = 0
        with open(args.output, "w") as out_f:
            for mac, state in devices_sorted:
                with open(state["path"], "r") as flows_f:
                    flow_log = flows_f.read()
                matched = state["matched"]
                emitted = state["count"]
                if matched == emitted:
                    prompt = (
                        f"Device MAC: {mac}. Date: {args.date} UTC. "
                        f"Scope: home wlan0 interface. "
                        f"Flow count: {matched} (all flows involving this device as a or b)."
                    )
                else:
                    prompt = (
                        f"Device MAC: {mac}. Date: {args.date} UTC. "
                        f"Scope: home wlan0 interface. "
                        f"Total flows involving this device: {matched}. "
                        f"The attached flow log contains the first {emitted} of them "
                        f"as a representative sample (rest omitted to keep the payload in context)."
                    )
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

        print()
        print(f"Wrote {count} JSONL records to {args.output}")
        print(f"  Total JSONL size: {fmt_bytes(total_jsonl_bytes)}")
        if devices_sorted:
            top_mac, top_state = devices_sorted[0]
            top_size = os.path.getsize(top_state["path"])
            print(f"  Top device: mac={top_mac}  flows={top_state['count']:,}  "
                  f"flow_log={fmt_bytes(top_size)}")

    finally:
        # Cleanup all temp files regardless of success.
        for state in device_state.values():
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
