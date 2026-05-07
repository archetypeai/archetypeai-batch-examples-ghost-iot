#!/usr/bin/env python3
"""
Build per-chunk single-record JSONL files for the section-8 batch many-files demo.

For each (device, hour) bucket in a multi-home WiFi flow CSV, this script
greedily packs flow rows into chunks of at most --max-chunk-bytes (default
10 KB ≈ 150 GHOST-IoT-formatted flow rows ≈ 2.5K tokens), then writes EACH
chunk to its own single-record JSONL file. **No flows are dropped — every
row in the source ends up in exactly one chunk.**

Why one-record-per-file: the platform's batch worker treats each input file
as an independent inference unit, and the C model's GPU pod OOMs when
processing multi-record JSONLs even with batch-size bisection (observed at
2 MB / 196 records, where bisection fell all the way to bs=1 and still
OOMed). Each file = one inference, sized to fit C-model context. Same shape
as the wifi-multi demo's per-query budget — see that repo's "Constraints
driving the design" subsection.

Output layout (file count scales with source size):

    data/per_device_hour/
      dev_home_a__alice_laptop__h00__c0000.jsonl   (single record, ≤10 KB)
      dev_home_a__alice_laptop__h00__c0001.jsonl
      ...
      dev_home_c__home_c_thermostat__h23__c0000.jsonl
    data/manifest_chunked.jsonl    (sidecar — one entry per chunk file)

At 1 GB source CSV: ~37,000 chunk files. At 10 GB: ~370,000. The sidecar
sums to one line per file and tracks (home, human, device, mac, hour,
chunk_index, n_flows, byte size, time range) for downstream joins.

Usage:
    python 1_prepare_data/prepare_per_device_hour_jsonls.py \\
      --input data/wifi_flows_multihome_1gb.csv \\
      --output-dir data/per_device_hour \\
      --manifest data/manifest_chunked.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone

from topology import REPO_DIR, load_topology

DEFAULT_DATE = "2019-10-19"
DEFAULT_INPUT_CSV = os.path.join(REPO_DIR, "data", "wifi_flows_multihome_1gb.csv")
DEFAULT_OUTPUT_DIR = os.path.join(REPO_DIR, "data", "per_device_hour")
DEFAULT_MANIFEST = os.path.join(REPO_DIR, "data", "manifest_chunked.jsonl")
# 10 KB chunk budget — matches the safe 150-flow / ~2.5K-token cap from §8.2's
# binary search. Each file is a single-record JSONL, so this is also the file's
# total inputs[0].data size. The platform OOMs at higher per-record sizes even
# at batch size 1 — keep this tight.
DEFAULT_MAX_CHUNK_BYTES = 10 * 1024
PROGRESS_EVERY = 1_000_000

SYSTEM = (
    "You are a network security analyst reviewing smart-home WiFi traffic. "
    "Flows are bidirectional network conversations between two endpoints (a and b). "
    "Each flow has byte/packet counters for each direction, a transport protocol, "
    "an application-layer protocol, and start/end timestamps."
)

INSTRUCTION = (
    "Analyze the attached flow log slice and describe what this device was doing "
    "during this part of the hour. Cover dominant application protocols, traffic "
    "volume, and anything unusual. Stay focused on this slice — separate downstream "
    "calls will combine slices into per-hour, per-device, per-user, and per-house "
    "narratives."
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


def file_id_for(home_id: str, device_id: str, hour: int, chunk_index: int) -> str:
    return f"dev_{home_id}__{device_id}__h{hour:02d}__c{chunk_index:04d}.jsonl"


class BucketState:
    """In-flight chunk for one (device, hour) bucket. Tracks bytes, line strs,
    min/max ts_start, and the number of chunks already emitted so each new
    chunk gets a unique chunk_index."""

    __slots__ = ("lines", "byte_count", "ts_min", "ts_max", "next_chunk_index")

    def __init__(self):
        self.lines: list[str] = []
        self.byte_count = 0
        self.ts_min: int | None = None
        self.ts_max: int | None = None
        self.next_chunk_index = 0

    def add(self, line: str, ts_start: int) -> None:
        delta = len(line.encode("utf-8")) + (1 if self.lines else 0)
        self.byte_count += delta
        self.lines.append(line)
        if self.ts_min is None or ts_start < self.ts_min:
            self.ts_min = ts_start
        if self.ts_max is None or ts_start > self.ts_max:
            self.ts_max = ts_start

    def n_flows(self) -> int:
        return len(self.lines)

    def is_empty(self) -> bool:
        return not self.lines

    def reset_chunk(self) -> None:
        self.lines = []
        self.byte_count = 0
        self.ts_min = None
        self.ts_max = None


def main():
    parser = argparse.ArgumentParser(description="Per-chunk single-record JSONL prep with dynamic byte-based chunking (section 8).")
    parser.add_argument("--date", default=DEFAULT_DATE, help="Date to summarize, YYYY-MM-DD UTC (default: %(default)s)")
    parser.add_argument("--input", default=DEFAULT_INPUT_CSV, help="Multi-home wlan0 flows CSV (default: %(default)s)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help="Directory to write per-chunk JSONL files (default: %(default)s)")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST,
                        help="Sidecar manifest JSONL — one entry per chunk file (default: %(default)s)")
    parser.add_argument("--max-chunk-bytes", type=int, default=DEFAULT_MAX_CHUNK_BYTES,
                        help=f"Max bytes per chunk's flow-log payload (default: {DEFAULT_MAX_CHUNK_BYTES} = 10 KB ≈ "
                             f"150 GHOST-IoT-formatted flows ≈ 2.5K tokens). The empirical safe ceiling — "
                             f"see §8.2 of the README. Don't raise this without re-running the cap binary search.")
    args = parser.parse_args()

    topo = load_topology()
    mac_to_device = {d.mac: d for d in topo.all_devices}
    print(f"Topology: {len(topo.homes)} homes, {len(topo.all_humans)} humans, {len(topo.all_devices)} devices")
    print(f"Input:    {args.input}")
    print(f"Output:   {args.output_dir}/")
    print(f"Manifest: {args.manifest}")
    print(f"Date:     {args.date}")
    print(f"Cap:      {args.max_chunk_bytes:,} bytes / chunk (one chunk = one file = one inference)")
    print()

    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = int(day.timestamp())
    window_end = int((day + timedelta(days=1)).timestamp())

    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.manifest) or ".", exist_ok=True)

    # In-memory bucket state for every (device, hour) we expect.
    # Memory: 576 buckets × max_chunk_bytes ≈ ~6 MB regardless of source size.
    buckets: dict[tuple[str, int], BucketState] = {}
    bucket_meta: dict[tuple[str, int], tuple] = {}  # (mac, hour) -> (home, device)
    for home in topo.homes:
        for dev in home.all_devices:
            for hour in range(24):
                buckets[(dev.mac, hour)] = BucketState()
                bucket_meta[(dev.mac, hour)] = (home, dev)

    manifest_f = open(args.manifest, "w")
    n_files_written = 0

    def flush_chunk(mac: str, hour: int) -> None:
        """Emit the in-flight chunk for (mac, hour) as a 1-record JSONL file."""
        nonlocal n_files_written
        b = buckets[(mac, hour)]
        if b.is_empty():
            return
        home, dev = bucket_meta[(mac, hour)]
        chunk_index = b.next_chunk_index
        fid = file_id_for(home.home_id, dev.device_id, hour, chunk_index)
        out_path = os.path.join(args.output_dir, fid)

        hour_label = f"{hour:02d}:00-{(hour + 1) % 24:02d}:00 UTC"
        ts_lo = datetime.fromtimestamp(b.ts_min, tz=timezone.utc).strftime("%H:%M:%S")
        ts_hi = datetime.fromtimestamp(b.ts_max, tz=timezone.utc).strftime("%H:%M:%S")
        owner_str = dev.owner if dev.owner is not None else "shared"
        prompt = (
            f"Date: {args.date} UTC. Home: {home.home_id} ({home.label}). "
            f"Owner: {owner_str}. Device: {dev.device_id} (type={dev.type}, mac={dev.mac}). "
            f"Hour: {hour_label}. Chunk slice: {b.n_flows()} flows covering {ts_lo}-{ts_hi} UTC."
        )
        flow_log = FLOW_LOG_PREAMBLE + "\n\n" + "\n".join(b.lines)
        record = {
            "system": SYSTEM,
            "instruction": INSTRUCTION,
            "prompt": prompt,
            "inputs": [{"type": "text", "format": "plain", "data": flow_log}],
        }
        with open(out_path, "w") as f:
            f.write(json.dumps(record) + "\n")

        manifest_f.write(json.dumps({
            "file_id": fid,
            "line_index": 0,
            "chunk_index": chunk_index,
            "home_id": home.home_id,
            "home_label": home.label,
            "gateway_mac": home.gateway_mac,
            "human": dev.owner,
            "device_id": dev.device_id,
            "device_type": dev.type,
            "device_mac": dev.mac,
            "hour_utc": hour,
            "n_flows": b.n_flows(),
            "n_bytes": b.byte_count,
            "ts_start_min": b.ts_min,
            "ts_start_max": b.ts_max,
            "date": args.date,
        }) + "\n")

        b.next_chunk_index += 1
        b.reset_chunk()
        n_files_written += 1

    t0 = time.time()
    total = 0
    matched = 0
    skipped_unknown_mac = 0

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

                mac_a = row.get("mac_a") or ""
                mac_b = row.get("mac_b") or ""
                if not mac_a or not mac_b:
                    continue

                dev = mac_to_device.get(mac_a) or mac_to_device.get(mac_b)
                if dev is None:
                    skipped_unknown_mac += 1
                    continue

                matched += 1
                hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
                key = (dev.mac, hour)
                b = buckets[key]
                line = flow_row_to_line(row)
                line_bytes = len(line.encode("utf-8")) + (1 if b.lines else 0)
                if b.byte_count + line_bytes > args.max_chunk_bytes and not b.is_empty():
                    flush_chunk(dev.mac, hour)
                    b = buckets[key]  # reset_chunk() rebuilds state in-place
                b.add(line, ts)

                if total % PROGRESS_EVERY == 0:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed > 0 else 0
                    print(f"  Scanned {total:,} rows ({matched:,} matched, "
                          f"{n_files_written:,} chunk files emitted)  "
                          f"{rate / 1000:.0f}k rows/s  {elapsed:.1f}s")

        # Flush any in-flight buffers that weren't full at end of scan.
        for (mac, hour), b in list(buckets.items()):
            if not b.is_empty():
                flush_chunk(mac, hour)

        # Empty buckets get one "no flows" file so the 576-bucket grid is
        # uniform and downstream group-by joins are trivial.
        for (mac, hour), b in buckets.items():
            if b.next_chunk_index != 0:
                continue
            home, dev = bucket_meta[(mac, hour)]
            fid = file_id_for(home.home_id, dev.device_id, hour, 0)
            out_path = os.path.join(args.output_dir, fid)
            hour_label = f"{hour:02d}:00-{(hour + 1) % 24:02d}:00 UTC"
            owner_str = dev.owner if dev.owner is not None else "shared"
            prompt = (
                f"Date: {args.date} UTC. Home: {home.home_id} ({home.label}). "
                f"Owner: {owner_str}. Device: {dev.device_id} (type={dev.type}, mac={dev.mac}). "
                f"Hour: {hour_label}. Flow count: 0. (No flows recorded for this device-hour.)"
            )
            flow_log = FLOW_LOG_PREAMBLE + "\n\n(no flows)"
            record = {
                "system": SYSTEM,
                "instruction": INSTRUCTION,
                "prompt": prompt,
                "inputs": [{"type": "text", "format": "plain", "data": flow_log}],
            }
            with open(out_path, "w") as f:
                f.write(json.dumps(record) + "\n")
            manifest_f.write(json.dumps({
                "file_id": fid,
                "line_index": 0,
                "chunk_index": 0,
                "home_id": home.home_id,
                "home_label": home.label,
                "gateway_mac": home.gateway_mac,
                "human": dev.owner,
                "device_id": dev.device_id,
                "device_type": dev.type,
                "device_mac": dev.mac,
                "hour_utc": hour,
                "n_flows": 0,
                "n_bytes": 0,
                "ts_start_min": None,
                "ts_start_max": None,
                "date": args.date,
            }) + "\n")
            n_files_written += 1

    finally:
        manifest_f.close()

    elapsed = time.time() - t0
    chunks_per_bucket = [b.next_chunk_index for b in buckets.values()]
    n_buckets_with_flows = sum(1 for c in chunks_per_bucket if c > 0)
    print()
    print(f"Scan complete: {total:,} rows, {matched:,} matched, "
          f"{skipped_unknown_mac:,} skipped (unknown MACs).  {elapsed:.1f}s")
    print(f"Wrote {n_files_written:,} chunk files (one inference each) to {args.output_dir}/")
    print(f"  Buckets populated: {n_buckets_with_flows}/{len(buckets)}  "
          f"(chunks/bucket: min={min(chunks_per_bucket)}  "
          f"avg={sum(chunks_per_bucket)/len(chunks_per_bucket):.1f}  "
          f"max={max(chunks_per_bucket)})")
    print(f"Wrote sidecar manifest: {args.manifest}  ({n_files_written:,} entries)")


if __name__ == "__main__":
    main()
