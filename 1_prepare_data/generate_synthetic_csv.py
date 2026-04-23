#!/usr/bin/env python3
"""
Generate a synthetic wlan0 WiFi flow CSV for stress-testing the pipeline.

Strategy: sample-and-jitter. Rows are sampled with replacement from the real
GHOST-IoT wlan0 flows CSV, then:
  - ts_start is replaced with a random timestamp in the chosen UTC date window
  - ts_end is set to ts_start + original duration (capped at the window end)
  - byte/packet counts are optionally jittered to avoid exact duplicates
Everything else (MACs, IPs, ports, protocols) is preserved from the template
row, so the synthetic data keeps realistic distributions and device pairings.

Usage:
    # Size target (recommended)
    python generate_synthetic_csv.py --target-size-gb 1.0
    python generate_synthetic_csv.py --target-size-mb 100

    # Exact row count
    python generate_synthetic_csv.py --rows 5000000

    # Different date / source / output
    python generate_synthetic_csv.py --date 2019-10-19 --source data/wlan0_ipv4_flows_db.csv --output data/wlan0_ipv4_flows_large.csv

Output: a CSV with the same schema as the source. Default output path is
data/wlan0_ipv4_flows_large.csv (gitignored by default — see .gitignore).
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

DEFAULT_DATE = "2019-10-19"
DEFAULT_SOURCE = "data/wlan0_ipv4_flows_db.csv"
DEFAULT_OUTPUT = "data/wlan0_ipv4_flows_large.csv"
PROGRESS_EVERY = 500_000  # rows
JITTER_BYTES = 0.2         # ±20% on byte counts
JITTER_PACKETS = 0.2       # ±20% on packet counts


def parse_args():
    p = argparse.ArgumentParser(description="Synthesize a larger WiFi flow CSV for stress testing.")
    size_group = p.add_mutually_exclusive_group(required=True)
    size_group.add_argument("--target-size-gb", type=float, help="Target output size in gigabytes (1 GB = 1024^3 bytes)")
    size_group.add_argument("--target-size-mb", type=float, help="Target output size in megabytes (1 MB = 1024^2 bytes)")
    size_group.add_argument("--rows", type=int, help="Exact number of rows to write")
    p.add_argument("--source", default=DEFAULT_SOURCE, help="Source CSV to sample from (default: %(default)s)")
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="Output CSV path (default: %(default)s)")
    p.add_argument("--date", default=DEFAULT_DATE, help="UTC date for ts_start window, YYYY-MM-DD (default: %(default)s)")
    p.add_argument("--seed", type=int, default=42, help="PRNG seed (default: %(default)s)")
    p.add_argument("--no-jitter", action="store_true", help="Disable byte/packet jitter (exact template values)")
    p.add_argument("--keep-empty-macs", action="store_true", help="Keep template rows with empty mac_a/mac_b (default: drop)")
    return p.parse_args()


def load_template_rows(path: str, keep_empty_macs: bool) -> tuple[list[str], list[dict]]:
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise SystemExit(f"Could not read header from {path}")
        rows = []
        for r in reader:
            if not keep_empty_macs and (not r.get("mac_a") or not r.get("mac_b")):
                continue
            rows.append(r)
    return fieldnames, rows


def avg_row_bytes(fieldnames: list[str], rows: list[dict], sample_size: int = 1000) -> float:
    """Estimate average bytes per CSV row by writing a sample to a buffer."""
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    header_len = buf.tell()
    buf.seek(0); buf.truncate()
    sample = rows if len(rows) <= sample_size else random.sample(rows, sample_size)
    w.writerows(sample)
    total = buf.tell()
    return total / max(1, len(sample)), header_len


def jitter_int(value_str: str, pct: float, rng: random.Random) -> str:
    """Multiply value by a random factor in [1-pct, 1+pct]. Returns int string."""
    try:
        v = int(value_str)
    except (TypeError, ValueError):
        return value_str
    if v == 0:
        return "0"
    factor = 1.0 + rng.uniform(-pct, pct)
    return str(max(0, int(round(v * factor))))


def main():
    args = parse_args()
    rng = random.Random(args.seed)

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = args.source if os.path.isabs(args.source) else os.path.join(repo_dir, args.source)
    output = args.output if os.path.isabs(args.output) else os.path.join(repo_dir, args.output)

    # --- Load template rows ------------------------------------------------
    print(f"Source: {source}")
    fieldnames, rows = load_template_rows(source, args.keep_empty_macs)
    print(f"Loaded {len(rows):,} template rows ({len(fieldnames)} columns).")

    if not rows:
        raise SystemExit("No template rows to sample from.")

    # --- Determine target row count ----------------------------------------
    avg_bytes, header_bytes = avg_row_bytes(fieldnames, rows)
    print(f"Estimated average row size: {avg_bytes:.1f} bytes (header: {header_bytes} bytes).")

    if args.rows is not None:
        target_rows = args.rows
        target_bytes_estimate = header_bytes + int(avg_bytes * target_rows)
    else:
        if args.target_size_gb is not None:
            target_bytes = int(args.target_size_gb * (1024 ** 3))
        else:
            target_bytes = int(args.target_size_mb * (1024 ** 2))
        target_rows = max(1, int((target_bytes - header_bytes) / avg_bytes))
        target_bytes_estimate = target_bytes

    def fmt_bytes(n: int) -> str:
        if n >= 1024 ** 3:
            return f"{n / 1024**3:.2f} GB"
        if n >= 1024 ** 2:
            return f"{n / 1024**2:.1f} MB"
        return f"{n / 1024:.0f} KB"

    print(f"Target: {target_rows:,} rows (~{fmt_bytes(target_bytes_estimate)}).")

    # --- Pre-flight checks -------------------------------------------------
    out_dir = os.path.dirname(output) or "."
    try:
        stat = os.statvfs(out_dir)
        free_bytes = stat.f_bavail * stat.f_frsize
    except (AttributeError, OSError):
        free_bytes = None
    if free_bytes is not None:
        print(f"Free disk space at {out_dir}: {fmt_bytes(free_bytes)}")
        if target_bytes_estimate > free_bytes * 0.9:
            raise SystemExit(
                f"ERROR: target ~{fmt_bytes(target_bytes_estimate)} would exceed 90% of free "
                f"disk ({fmt_bytes(free_bytes)}). Free some space or pick a smaller target."
            )

    # Rough time estimate — assume ~150k rows/sec sustained on a typical SSD.
    est_seconds = target_rows / 150_000
    if est_seconds >= 60:
        mins = est_seconds / 60
        print(f"Estimated generation time: {mins:.1f} minutes (at ~150k rows/sec).")
    else:
        print(f"Estimated generation time: {est_seconds:.0f} seconds.")
    print()

    # --- Set up timestamp window -------------------------------------------
    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = int(day.timestamp())
    window_end = int((day + timedelta(days=1)).timestamp()) - 1  # inclusive upper bound
    print(f"Timestamp window: {args.date} UTC  [{window_start}, {window_end}]")

    # Precompute original flow durations per template row (cap to 0 for safety)
    durations = []
    for r in rows:
        try:
            dur = max(0, int(r["ts_end"]) - int(r["ts_start"]))
        except (TypeError, ValueError):
            dur = 0
        durations.append(dur)

    # --- Write ---------------------------------------------------------------
    os.makedirs(os.path.dirname(output), exist_ok=True)
    n_templates = len(rows)
    t0 = time.time()
    written = 0

    print(f"Writing to: {output}")
    print()

    with open(output, "w", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        while written < target_rows:
            idx = rng.randrange(n_templates)
            template = rows[idx]
            ts_start = rng.randint(window_start, window_end)
            ts_end = min(window_end, ts_start + durations[idx])

            new_row = dict(template)
            new_row["ts_start"] = str(ts_start)
            new_row["ts_end"] = str(ts_end)
            if not args.no_jitter:
                for col in ("bytes_a", "bytes_b"):
                    new_row[col] = jitter_int(template[col], JITTER_BYTES, rng)
                for col in ("packets_a", "packets_b"):
                    new_row[col] = jitter_int(template[col], JITTER_PACKETS, rng)

            writer.writerow(new_row)
            written += 1

            if written % PROGRESS_EVERY == 0:
                elapsed = time.time() - t0
                rate = written / elapsed if elapsed > 0 else 0
                eta = (target_rows - written) / rate if rate > 0 else 0
                cur_bytes = fout.tell()
                pct = 100 * written / target_rows
                print(
                    f"  [{pct:5.1f}%] {written:>12,}/{target_rows:,} rows  "
                    f"{fmt_bytes(cur_bytes):>10}  {rate/1000:6.1f}k rows/s  ETA {eta:5.0f}s"
                )

    elapsed = time.time() - t0
    final_bytes = os.path.getsize(output)
    print()
    print("=" * 60)
    print(f" Wrote {written:,} rows to {output}")
    print(f" Final size: {fmt_bytes(final_bytes)} ({final_bytes:,} bytes)")
    print(f" Elapsed:    {elapsed:.1f}s ({written/elapsed/1000:.1f}k rows/sec)")
    print("=" * 60)


if __name__ == "__main__":
    main()
