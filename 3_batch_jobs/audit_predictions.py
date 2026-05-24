#!/usr/bin/env python3
"""
Quick audit of any predictions JSONL: spot cliff-style garbage, length anomalies,
and stylistic patterns. Designed to be the first thing you run after §8.5 (chunks)
and after every subsequent reduce stage (§8.6 A, §8.7 A₂, §8.8 B, §8.9 device-day,
§8.10 user-day/house-day).

Usage:
    python 3_batch_jobs/audit_predictions.py data/predictions_chunks.jsonl
    python 3_batch_jobs/audit_predictions.py data/predictions_chunks_c25_8b.jsonl \\
        --baseline data/predictions_chunks_4k.jsonl \\
        --baseline-label "c2.4.0-7b" --label "c2.5.0-8b"

Cliff signatures detected (silent quality failures §10.6 / §8.2):
    - Predictions < 100 chars                  (cliff-1: empty/fragment)
    - Predictions starting with pipe-row       (cliff-2: model regurgitates input as table)
    - Predictions containing the flow preamble (cliff-3: model auto-completes preamble)
    - Sub-500-char predictions                 (cliff-4: degraded but not fully cliffed)

If ANY of cliff-1/2/3 fire on more than 1% of records, exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from typing import Iterable


# Cliff signature: prediction starts with "<int>|<PROTO>|<int>|..." — model
# regurgitated raw pipe-CSV rows from the input.
_CLIFF_PIPE_RE = re.compile(r"^\s*\d+\|[A-Za-z_]+\|\d+\|")
_FLOW_PREAMBLE = "time_utc|mac_a|mac_b"


def load_predictions(path: str) -> list[str]:
    """Return list of prediction strings, indexed by line order in the file."""
    out: list[str] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out.append(r.get("prediction", ""))
    return out


def percentile(sorted_vals: list[int], frac: float) -> int:
    if not sorted_vals:
        return 0
    idx = max(0, min(len(sorted_vals) - 1, int(frac * len(sorted_vals))))
    return sorted_vals[idx]


def categorize_ending(text: str) -> str:
    """Stylistic ending bucket (helps distinguish max_new_tokens truncation from markdown)."""
    tail = text.rstrip()
    if not tail:
        return "empty"
    last = tail[-1]
    if last in ".!?":
        return "sentence_period"
    if last == "*":
        return "markdown_bold"
    if last in "`)]\"'":
        return "url_or_code"
    if last == "|":
        return "table_pipe"
    if last in ",;:-—":
        return "list_item_no_punct"
    # tail ends on a bare word
    if re.search(r"\s\w+$", tail):
        return "mid_word"
    return "other_no_term"


def audit(path: str, label: str) -> dict:
    preds = load_predictions(path)
    n = len(preds)
    lens = [len(p) for p in preds]
    sorted_lens = sorted(lens)

    cliff_sub_100 = [(i, p) for i, p in enumerate(preds) if len(p) < 100]
    cliff_sub_500_only = [(i, p) for i, p in enumerate(preds) if 100 <= len(p) < 500]
    cliff_pipe = [(i, p) for i, p in enumerate(preds) if _CLIFF_PIPE_RE.match(p)]
    cliff_preamble = [(i, p) for i, p in enumerate(preds) if _FLOW_PREAMBLE in p]

    # Pre-populate all categories so missing buckets render as 0 (not absent).
    endings: dict[str, int] = {k: 0 for k in (
        "sentence_period", "markdown_bold", "url_or_code", "list_item_no_punct",
        "table_pipe", "mid_word", "other_no_term", "empty",
    )}
    for p in preds:
        endings[categorize_ending(p)] += 1

    # Length-by-ending: real budget truncation should cluster at high lengths.
    lens_by_normal_end = [len(p) for p in preds
                          if categorize_ending(p) in ("sentence_period", "markdown_bold", "url_or_code")]
    lens_by_other_end = [len(p) for p in preds
                         if categorize_ending(p) in ("mid_word", "other_no_term", "list_item_no_punct", "table_pipe")]

    return {
        "label": label,
        "path": path,
        "n": n,
        "lens": {
            "min": sorted_lens[0] if sorted_lens else 0,
            "p1":  percentile(sorted_lens, 0.01),
            "p5":  percentile(sorted_lens, 0.05),
            "p25": percentile(sorted_lens, 0.25),
            "p50": sorted_lens[len(sorted_lens)//2] if sorted_lens else 0,
            "p75": percentile(sorted_lens, 0.75),
            "p95": percentile(sorted_lens, 0.95),
            "p99": percentile(sorted_lens, 0.99),
            "max": sorted_lens[-1] if sorted_lens else 0,
            "mean": int(statistics.mean(lens)) if lens else 0,
        },
        "cliff": {
            "sub_100": len(cliff_sub_100),
            "sub_500_only": len(cliff_sub_500_only),
            "pipe_start": len(cliff_pipe),
            "flow_preamble": len(cliff_preamble),
            "samples_sub_100": cliff_sub_100[:3],
            "samples_pipe_start": cliff_pipe[:3],
        },
        "endings": dict(endings),
        "mean_len_normal_ending": int(statistics.mean(lens_by_normal_end)) if lens_by_normal_end else 0,
        "mean_len_other_ending":  int(statistics.mean(lens_by_other_end))  if lens_by_other_end  else 0,
    }


def print_report(a: dict, b: dict | None = None) -> None:
    def col(d, key_path):
        cur = d
        for k in key_path.split("."):
            cur = cur.get(k, "?")
            if not isinstance(cur, dict):
                return cur
        return cur

    def row(label, key_path, formatter=str):
        va = col(a, key_path)
        line = f"  {label:<34}  {formatter(va):>12}"
        if b is not None:
            vb = col(b, key_path)
            line += f"  {formatter(vb):>12}"
        print(line)

    def row_pct(label, key_path):
        a_val = col(a, key_path); a_n = a["n"]
        line = f"  {label:<34}  {a_val:>5} ({100*a_val/max(1,a_n):>5.1f}%)"
        if b is not None:
            b_val = col(b, key_path); b_n = b["n"]
            line += f"  {b_val:>5} ({100*b_val/max(1,b_n):>5.1f}%)"
        print(line)

    head = f"  {'metric':<34}  {a['label']:>12}"
    if b is not None:
        head += f"  {b['label']:>12}"
    print()
    print("=" * (len(head)+2))
    print(head)
    print("=" * (len(head)+2))

    row("records",                "n", lambda v: f"{v:,}")
    print()
    print("  length (chars):")
    for p in ["min", "p1", "p5", "p25", "p50", "p75", "p95", "p99", "max", "mean"]:
        row(f"    {p}",            f"lens.{p}", lambda v: f"{v:,}")
    print()
    print("  cliff signatures:")
    row_pct("    < 100 chars (empty/fragment)",   "cliff.sub_100")
    row_pct("    100 ≤ len < 500 (degraded)",     "cliff.sub_500_only")
    row_pct("    starts with pipe-row",           "cliff.pipe_start")
    row_pct("    contains flow-log preamble",     "cliff.flow_preamble")
    print()
    print("  ending style:")
    for k in ["sentence_period", "markdown_bold", "url_or_code", "list_item_no_punct",
              "table_pipe", "mid_word", "other_no_term", "empty"]:
        row_pct(f"    {k:<26}", f"endings.{k}")
    print()
    print("  mean len by ending type (truncation tell — much higher 'other' = budget hit):")
    row(f"    mean when . ! ? * ` ) ] etc.",     "mean_len_normal_ending", lambda v: f"{v:,}")
    row(f"    mean when bare-word/no-term",      "mean_len_other_ending",  lambda v: f"{v:,}")

    # Surface concerning samples
    print()
    if a["cliff"]["samples_sub_100"]:
        print(f"  --- {a['label']}: sub-100-char samples ---")
        for i, p in a["cliff"]["samples_sub_100"]:
            print(f"    [#{i}, len={len(p)}] {p!r}")
    if a["cliff"]["samples_pipe_start"]:
        print(f"  --- {a['label']}: pipe-start cliff samples ---")
        for i, p in a["cliff"]["samples_pipe_start"]:
            print(f"    [#{i}] {p[:200]!r}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Audit a predictions JSONL for cliff garbage and length anomalies.")
    parser.add_argument("input", help="Path to predictions JSONL.")
    parser.add_argument("--label", default=None, help="Display label for the input (default: filename).")
    parser.add_argument("--baseline", default=None, help="Optional comparison predictions JSONL.")
    parser.add_argument("--baseline-label", default=None, help="Display label for the baseline (default: filename).")
    parser.add_argument("--cliff-fail-pct", type=float, default=1.0,
                        help="Exit non-zero if any of cliff-1/2/3 exceeds this %% of records (default: 1.0).")
    args = parser.parse_args()

    a = audit(args.input, args.label or args.input.rsplit("/",1)[-1])
    b = audit(args.baseline, args.baseline_label or args.baseline.rsplit("/",1)[-1]) if args.baseline else None

    print_report(a, b)

    # Hard fail signals
    cliff_pct = max(
        100 * a["cliff"]["sub_100"]    / max(1, a["n"]),
        100 * a["cliff"]["pipe_start"] / max(1, a["n"]),
        100 * a["cliff"]["flow_preamble"] / max(1, a["n"]),
    )
    if cliff_pct > args.cliff_fail_pct:
        print(f"  ✗ FAIL: cliff signatures fire on {cliff_pct:.2f}% of records (> {args.cliff_fail_pct}% threshold).")
        sys.exit(1)
    print(f"  ✓ OK: no cliff signatures above the {args.cliff_fail_pct}% threshold.")


if __name__ == "__main__":
    main()
