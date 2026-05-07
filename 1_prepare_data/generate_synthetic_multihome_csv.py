#!/usr/bin/env python3
"""
Generate a synthetic multi-home WiFi flow CSV from data/topology.json.

For section 9 of the README — the "many small files" batch pattern. Combines:
  - GHOST-IoT seed CSV (real wlan0_ipv4_flows_db.csv) — provides realistic
    protocol/byte/packet distributions
  - data/topology.json — defines 3 homes / 6 humans / 24 devices and their
    gateway MACs
  - Per-device-type profiles — control temporal shape so each device's day
    looks plausible (phones bursty in evenings, watches sparse heartbeat,
    thermostats uniform, etc)

Each output row inherits its protocol/port/byte/packet distribution from a
randomly chosen seed row. Only mac_a, mac_b, ts_start, ts_end (and optionally
jittered byte/packet counts) are rewritten. Rows are written in per-device
order — not globally sorted by timestamp, since the per-device-hour prep
script in step 1b buckets by (device_mac, hour) anyway.

Per-device flow counts are allocated proportionally to each device type's
"weight" so total rows match --target-size-gb / --target-size-mb / --rows.

Usage:
    # 1 GB target — typical demo run, 24 devices x ~24h x deep sampling pool
    python 1_prepare_data/generate_synthetic_multihome_csv.py --target-size-gb 1.0

    # Tiny smoke test
    python 1_prepare_data/generate_synthetic_multihome_csv.py --target-size-mb 1

    # Exact row count
    python 1_prepare_data/generate_synthetic_multihome_csv.py --rows 5800000
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timezone

from topology import REPO_DIR, load_topology

DEFAULT_DATE = "2019-10-19"
DEFAULT_SEED_CSV = os.path.join(REPO_DIR, "data", "wlan0_ipv4_flows_db.csv")
DEFAULT_OUTPUT_CSV = os.path.join(REPO_DIR, "data", "wifi_flows_multihome_1gb.csv")
PROGRESS_EVERY = 500_000
JITTER_BYTES = 0.2
JITTER_PACKETS = 0.2


def _hours(active_ranges, baseline=0.05):
    """24-hour weight vector. Active ranges (inclusive) get 1.0, others get baseline."""
    w = [baseline] * 24
    for lo, hi in active_ranges:
        for h in range(lo, hi + 1):
            w[h] = 1.0
    return w


# Per device type:
#   weight = relative share of total flow count (proportional, not absolute)
#   hour_weights = 24-element relative likelihood per UTC hour
PROFILES = {
    "phone":         {"weight": 1050, "hour_weights": _hours([(7, 9), (12, 13), (17, 23)], baseline=0.1)},
    "laptop":        {"weight": 1400, "hour_weights": _hours([(9, 12), (13, 18), (20, 22)], baseline=0.05)},
    "watch":         {"weight":  350, "hour_weights": _hours([(6, 23)], baseline=0.2)},
    "smart_speaker": {"weight":  850, "hour_weights": _hours([(6, 23)], baseline=0.3)},
    "thermostat":    {"weight":  125, "hour_weights": [1.0] * 24},
}


def jitter_int(value_str: str, pct: float, rng: random.Random) -> str:
    try:
        v = int(value_str)
    except (TypeError, ValueError):
        return value_str
    if v == 0:
        return "0"
    factor = 1.0 + rng.uniform(-pct, pct)
    return str(max(0, int(round(v * factor))))


def pick_hour(rng: random.Random, weights: list[float]) -> int:
    total = sum(weights)
    r = rng.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r < acc:
            return i
    return 23


def fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def parse_args():
    p = argparse.ArgumentParser(description="Generate a multi-home synthetic WiFi flow CSV from topology.json.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--target-size-gb", type=float)
    g.add_argument("--target-size-mb", type=float)
    g.add_argument("--rows", type=int)
    p.add_argument("--seed-csv", default=DEFAULT_SEED_CSV, help="GHOST-IoT seed CSV (default: %(default)s)")
    p.add_argument("--output", default=DEFAULT_OUTPUT_CSV, help="Output CSV path (default: %(default)s)")
    p.add_argument("--date", default=DEFAULT_DATE, help="UTC date for ts_start window (default: %(default)s)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (default: %(default)s)")
    p.add_argument("--no-jitter", action="store_true", help="Disable byte/packet jitter")
    p.add_argument("--keep-empty-macs", action="store_true", help="Keep seed rows with empty mac_a/mac_b")
    return p.parse_args()


def load_seed(seed_csv: str, keep_empty_macs: bool):
    with open(seed_csv) as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = []
        for r in reader:
            if not keep_empty_macs and (not r.get("mac_a") or not r.get("mac_b")):
                continue
            rows.append(r)
    if not rows:
        sys.exit(f"Seed CSV is empty (or all rows filtered): {seed_csv}")
    return fieldnames, rows


def avg_row_bytes(fieldnames, rows, sample_size: int = 1000):
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    header_len = buf.tell()
    buf.seek(0); buf.truncate()
    sample = rows if len(rows) <= sample_size else random.sample(rows, sample_size)
    w.writerows(sample)
    return buf.tell() / max(1, len(sample)), header_len


def main():
    args = parse_args()
    rng = random.Random(args.seed)

    seed_csv = args.seed_csv if os.path.isabs(args.seed_csv) else os.path.join(REPO_DIR, args.seed_csv)
    output = args.output if os.path.isabs(args.output) else os.path.join(REPO_DIR, args.output)

    topo = load_topology()
    print(f"Topology: {len(topo.homes)} homes, {len(topo.all_humans)} humans, {len(topo.all_devices)} devices")
    print(f"Seed CSV: {seed_csv}")
    print(f"Output:   {output}")
    print(f"Date:     {args.date}")
    print()

    fieldnames, seed_rows = load_seed(seed_csv, args.keep_empty_macs)
    print(f"Loaded {len(seed_rows):,} seed rows ({len(fieldnames)} columns).")

    avg_bytes, header_bytes = avg_row_bytes(fieldnames, seed_rows)
    print(f"Avg row size: {avg_bytes:.1f} bytes (header: {header_bytes} bytes).")

    if args.rows is not None:
        target_rows = args.rows
        target_bytes_estimate = header_bytes + int(avg_bytes * target_rows)
    else:
        if args.target_size_gb is not None:
            target_bytes = int(args.target_size_gb * 1024 ** 3)
        else:
            target_bytes = int(args.target_size_mb * 1024 ** 2)
        target_rows = max(1, int((target_bytes - header_bytes) / avg_bytes))
        target_bytes_estimate = target_bytes

    print(f"Target: {target_rows:,} rows (~{fmt_bytes(target_bytes_estimate)}).")

    out_dir = os.path.dirname(output) or "."
    try:
        st = os.statvfs(out_dir)
        free_bytes = st.f_bavail * st.f_frsize
    except (AttributeError, OSError):
        free_bytes = None
    if free_bytes is not None:
        print(f"Free disk at {out_dir}: {fmt_bytes(free_bytes)}")
        if target_bytes_estimate > free_bytes * 0.9:
            sys.exit(
                f"ERROR: target ~{fmt_bytes(target_bytes_estimate)} would exceed 90% of free disk "
                f"({fmt_bytes(free_bytes)})."
            )

    est_seconds = target_rows / 150_000
    if est_seconds >= 60:
        print(f"Estimated time: {est_seconds / 60:.1f} min (at ~150k rows/sec).")
    else:
        print(f"Estimated time: {est_seconds:.0f}s.")
    print()

    # Allocate rows per device proportional to device-type weight.
    plan = []  # list of (device, profile, n_rows)
    devices_with_profile = []
    for home in topo.homes:
        for dev in home.all_devices:
            prof = PROFILES.get(dev.type)
            if prof is None:
                sys.exit(f"No profile for device type: {dev.type}")
            devices_with_profile.append((dev, prof))
    total_weight = sum(prof["weight"] for _, prof in devices_with_profile)

    allocated = 0
    for i, (dev, prof) in enumerate(devices_with_profile):
        if i == len(devices_with_profile) - 1:
            n = target_rows - allocated  # absorb rounding into last device
        else:
            n = int(round(target_rows * prof["weight"] / total_weight))
        plan.append((dev, prof, n))
        allocated += n

    print("Per-device row allocation:")
    for dev, prof, n in plan:
        owner = dev.owner or "(shared)"
        print(f"  {dev.home_id}  {owner:>6}  {dev.type:<14}  {dev.mac}  -> {n:>12,} rows")
    print()

    day_start_ts = int(datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    # Cache seed durations (clamp to 0-60s; keeps ts_end realistic)
    seed_durs = []
    for s in seed_rows:
        try:
            dur = max(0, min(int(s["ts_end"]) - int(s["ts_start"]), 60))
        except (TypeError, ValueError):
            dur = 0
        seed_durs.append(dur)
    n_seed = len(seed_rows)

    os.makedirs(os.path.dirname(output), exist_ok=True)
    print(f"Writing to: {output}")
    print()

    t0 = time.time()
    written = 0
    with open(output, "w", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        for dev, prof, n in plan:
            hw = prof["hour_weights"]
            for _ in range(n):
                idx = rng.randrange(n_seed)
                src = seed_rows[idx]
                hour = pick_hour(rng, hw)
                offset = hour * 3600 + rng.randint(0, 3599)
                ts_start = day_start_ts + offset

                new_row = dict(src)
                new_row["mac_a"] = dev.mac
                new_row["mac_b"] = dev.gateway_mac
                new_row["ts_start"] = str(ts_start)
                new_row["ts_end"] = str(ts_start + seed_durs[idx])
                if not args.no_jitter:
                    new_row["bytes_a"] = jitter_int(src["bytes_a"], JITTER_BYTES, rng)
                    new_row["bytes_b"] = jitter_int(src["bytes_b"], JITTER_BYTES, rng)
                    new_row["packets_a"] = jitter_int(src["packets_a"], JITTER_PACKETS, rng)
                    new_row["packets_b"] = jitter_int(src["packets_b"], JITTER_PACKETS, rng)

                writer.writerow(new_row)
                written += 1

                if written % PROGRESS_EVERY == 0:
                    elapsed = time.time() - t0
                    rate = written / elapsed if elapsed > 0 else 0
                    eta = (target_rows - written) / rate if rate > 0 else 0
                    pct = 100 * written / target_rows
                    cur_bytes = fout.tell()
                    print(
                        f"  [{pct:5.1f}%] {written:>12,}/{target_rows:,}  "
                        f"{fmt_bytes(cur_bytes):>10}  {rate / 1000:6.1f}k/s  ETA {eta:5.0f}s"
                    )

    elapsed = time.time() - t0
    final = os.path.getsize(output)
    print()
    print("=" * 60)
    print(f" Wrote {written:,} rows to {output}")
    print(f" Final size: {fmt_bytes(final)} ({final:,} bytes)")
    print(f" Elapsed:    {elapsed:.1f}s ({written / max(elapsed, 1e-6) / 1000:.1f}k rows/sec)")
    print("=" * 60)


if __name__ == "__main__":
    main()
