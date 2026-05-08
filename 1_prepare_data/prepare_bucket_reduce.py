#!/usr/bin/env python3
"""
REDUCE stage 1 (two-pass): chunk narratives -> 1 narrative per (device, hour) bucket.

The dynamic-chunking prep step (prepare_per_device_hour_jsonls.py) preserves
every flow but produces variable chunk counts per bucket (avg ~40-67 at 1 GB
depending on --max-chunk-bytes). Folding all of a bucket's chunk-narratives
into one device-hour reduce record blows the C model's ~16-20 KB CSV-heavy
quality cliff (§8.2, §10.7), so the bucket reduce runs in TWO PASSES:

  --stage a   intra-bucket pre-reduce
              For each bucket with N chunks, group chunks into packs of up to
              --group-size (default 4 — sized to keep pack ≤ 13 KB given
              ~3 KB chunk-narratives from max_new_tokens=1024). Emit ONE
              reduce record per group. Output: bucket_reduce_a.jsonl +
              manifest_bucket_reduce_a.jsonl.

  --stage b   bucket finalization
              For each bucket, fold its Stage-A partial narratives into ONE
              final device-hour narrative. Output: bucket_reduce_b.jsonl +
              manifest_per_device_hour.jsonl — same shape as section 8's
              original sidecar, so the downstream device-day reduce script
              works unchanged.

Usage:

    # Stage A — run after the chunk batch's predictions are extracted.
    python 1_prepare_data/prepare_bucket_reduce.py --stage a \\
      --predictions data/predictions_chunked.jsonl \\
      --manifest data/manifest_chunked.jsonl \\
      --output data/bucket_reduce_a.jsonl \\
      --output-manifest data/manifest_bucket_reduce_a.jsonl

    # Stage B — run after Stage A's predictions are extracted.
    python 1_prepare_data/prepare_bucket_reduce.py --stage b \\
      --predictions data/predictions_bucket_reduce_a.jsonl \\
      --manifest data/manifest_bucket_reduce_a.jsonl \\
      --output data/bucket_reduce_b.jsonl \\
      --output-manifest data/manifest_per_device_hour.jsonl

For low-volume buckets (N == 1 chunk in the source), Stage A produces a
single one-chunk pack — effectively a passthrough that re-narrates a single
slice — and Stage B then consumes that one partial. The two-pass overhead is
small (~576 extra inferences in Stage B) and keeps the pipeline uniform.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

from topology import REPO_DIR, load_topology

# Stage A packs N chunk-narratives per record. Each chunk-narrative is bounded
# by max_new_tokens (default 1024 ≈ ~2.6-3 KB of text — actual narrative length
# is generation-bounded, NOT chunk-input-bounded). Pack size = group_size ×
# narrative_size + envelope.
#
# The CSV-heavy quality cliff at ~16-20 KB applies even to narrative-heavy
# packs (the model can still degenerate when input is too long). With
# group_size=4 and ~3 KB narratives: pack ≈ 13 KB, comfortably under cliff.
# Don't raise without re-validating with a 5-record probe at the new size.
DEFAULT_GROUP_SIZE = 4
SOFT_WARN_BYTES = 16 * 1024  # narrative-pack cliff observed at ~16-20 KB (§8.2, §10.7)

SYSTEM_INTRA = (
    "You are a network security analyst. The text below is a sequence of "
    "narratives covering several sequential slices from one device's traffic "
    "during a single hour. Stay grounded in those slice narratives — do not "
    "invent traffic that isn't stated. Synthesize patterns across the slices."
)

INSTRUCTION_INTRA = (
    "Read the slice narratives above and write a single paragraph describing "
    "what this device was doing across the slices covered. Cover dominant "
    "protocols, traffic intensity, and anything unusual. 3-6 sentences. "
    "Another call will combine your output with other partial summaries to "
    "produce the full hour narrative."
)

SYSTEM_FINAL = (
    "You are a network security analyst writing a per-hour summary for one "
    "device. The text below contains partial narratives that each cover a "
    "consecutive portion of this device's hour. Stay grounded in those "
    "partials — do not invent traffic that isn't stated."
)

INSTRUCTION_FINAL = (
    "Read the partial narratives above and write a single paragraph "
    "describing what this device did during this UTC hour. Cover dominant "
    "protocols, when activity peaked within the hour, and anything unusual. "
    "4-7 sentences."
)


def load_manifest(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    if not out:
        sys.exit(f"Manifest is empty: {path}")
    return out


def load_predictions(path: str) -> dict:
    """Returns dict[(file_id, line_index)] -> prediction_text."""
    out: dict[tuple[str, int], str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            key = (r.get("file_id"), r.get("line_index", 0))
            out[key] = r.get("prediction", "")
    if not out:
        sys.exit(f"Predictions empty: {path}")
    return out


def build_pack(narratives: list[tuple[str, str]], header: str, footer: str) -> str:
    """narratives = list of (label, text). Wrap with header/footer + separators."""
    parts = [header]
    for label, text in narratives:
        parts.append("")
        parts.append(f"--- {label} ---")
        parts.append(text.strip() if text else "(no narrative)")
    parts.append("")
    parts.append(footer)
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Bucket reduce stages — chunks → device-hour narratives.")
    parser.add_argument("--stage", choices=["a", "b"], required=True,
                        help="a = intra-bucket grouping (chunks → partials); "
                             "b = bucket finalization (partials → 1 narrative per bucket)")
    parser.add_argument("--predictions", required=True,
                        help="Joined predictions JSONL from extract_predictions.py "
                             "(stage a reads chunk predictions, stage b reads stage-a predictions).")
    parser.add_argument("--manifest", required=True,
                        help="Sidecar manifest JSONL matching --predictions.")
    parser.add_argument("--output", required=True, help="Reduce JSONL to write.")
    parser.add_argument("--output-manifest", required=True, help="New sidecar manifest to write.")
    parser.add_argument("--group-size", type=int, default=DEFAULT_GROUP_SIZE,
                        help=f"Stage A only — max chunks per partial group (default: {DEFAULT_GROUP_SIZE}).")
    parser.add_argument("--date", default=None, help="Override date label (default: read from manifest).")
    args = parser.parse_args()

    topo = load_topology()
    manifest = load_manifest(args.manifest)
    preds = load_predictions(args.predictions)
    date = args.date or manifest[0].get("date") or "unknown"

    if args.stage == "a":
        do_stage_a(args, manifest, preds, topo, date)
    else:
        do_stage_b(args, manifest, preds, topo, date)


def do_stage_a(args, manifest, preds, topo, date):
    """Group chunks by (home, device, hour); split into groups of ≤group_size."""
    # Group manifest entries by bucket key, preserving chunk_index order.
    buckets: dict[tuple[str, str, int], list[dict]] = {}
    for entry in manifest:
        key = (entry["home_id"], entry["device_id"], entry["hour_utc"])
        buckets.setdefault(key, []).append(entry)
    for k in buckets:
        buckets[k].sort(key=lambda e: e.get("chunk_index", 0))

    print(f"Stage A: {len(buckets)} buckets, {sum(len(v) for v in buckets.values())} chunks total.")
    print(f"Group size: up to {args.group_size} chunks per partial.")
    print()

    n = 0
    n_warn = 0
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf:
        # Iterate in topology order for deterministic line_index.
        for home in topo.homes:
            for dev in home.all_devices:
                for hour in range(24):
                    key = (home.home_id, dev.device_id, hour)
                    chunks = buckets.get(key, [])
                    if not chunks:
                        continue
                    n_chunks = len(chunks)
                    n_groups = math.ceil(n_chunks / args.group_size)

                    for g in range(n_groups):
                        slice_start = g * args.group_size
                        slice_end = min((g + 1) * args.group_size, n_chunks)
                        group_chunks = chunks[slice_start:slice_end]

                        narratives = []
                        for c in group_chunks:
                            pred = preds.get((c["file_id"], c.get("line_index", c.get("chunk_index", 0))), "")
                            ts_min = c.get("ts_start_min")
                            ts_max = c.get("ts_start_max")
                            if ts_min is not None:
                                from datetime import datetime, timezone
                                lo = datetime.fromtimestamp(ts_min, tz=timezone.utc).strftime("%H:%M:%S")
                                hi = datetime.fromtimestamp(ts_max, tz=timezone.utc).strftime("%H:%M:%S")
                                label = f"chunk {c.get('chunk_index', 0)}: {c.get('n_flows', 0)} flows, {lo}-{hi}"
                            else:
                                label = f"chunk {c.get('chunk_index', 0)} (no flows)"
                            narratives.append((label, pred))

                        owner_str = dev.owner if dev.owner is not None else "shared"
                        hour_label = f"{hour:02d}:00-{(hour + 1) % 24:02d}:00 UTC"
                        header = (
                            f"=== CHUNK SLICE NARRATIVES ({len(narratives)}) FOR "
                            f"{dev.device_id} IN {home.label} ON {date} UTC, "
                            f"HOUR {hour_label}, GROUP {g + 1} OF {n_groups} ==="
                        )
                        footer = "=== END OF CHUNK SLICE NARRATIVES ==="
                        pack = build_pack(narratives, header, footer)

                        prompt = (
                            f"Date: {date} UTC. Home: {home.home_id} ({home.label}). "
                            f"Owner: {owner_str}. Device: {dev.device_id} (type={dev.type}, mac={dev.mac}). "
                            f"Hour: {hour_label}. Partial group {g + 1} of {n_groups} "
                            f"covering {len(narratives)} chunk slices."
                        )
                        data_field = pack + "\n\n" + INSTRUCTION_INTRA
                        size = len(data_field.encode("utf-8"))
                        if size > SOFT_WARN_BYTES:
                            n_warn += 1
                            if n_warn <= 5:
                                print(f"  WARN  {dev.device_id} h{hour} g{g + 1}: {size:,} B > {SOFT_WARN_BYTES:,} "
                                      f"— consider lowering --group-size")

                        record = {
                            "system": SYSTEM_INTRA,
                            "instruction": INSTRUCTION_INTRA,
                            "prompt": prompt,
                            "inputs": [{"type": "text", "format": "plain", "data": data_field}],
                        }
                        out_f.write(json.dumps(record) + "\n")

                        mf.write(json.dumps({
                            "line_index": n,
                            "home_id": home.home_id,
                            "home_label": home.label,
                            "gateway_mac": home.gateway_mac,
                            "human": dev.owner,
                            "device_id": dev.device_id,
                            "device_type": dev.type,
                            "device_mac": dev.mac,
                            "hour_utc": hour,
                            "group_index": g,
                            "group_count": n_groups,
                            "n_chunks_in_group": len(narratives),
                            "input_pack_bytes": size,
                            "date": date,
                        }) + "\n")
                        n += 1

    print()
    print(f"Wrote {n} partial reduce records to {args.output}")
    print(f"Wrote sidecar manifest: {args.output_manifest}")
    if n_warn:
        print(f"WARN: {n_warn} groups exceeded the {SOFT_WARN_BYTES:,}-byte soft cap. "
              f"Lower --group-size if predictions degrade.")


def do_stage_b(args, manifest, preds, topo, date):
    """Fold Stage A partials into 1 narrative per bucket."""
    # Stage-A's reduce JSONL was uploaded as a single file, so all predictions
    # share one file_id and we index purely by line_index.
    pred_by_line: dict[int, str] = {}
    for (file_id, line_index), p in preds.items():
        pred_by_line[line_index] = p

    # Group Stage-A entries by (home, device, hour); collect partial narratives in group order.
    by_bucket: dict[tuple[str, str, int], list[tuple[int, str]]] = {}
    for entry in manifest:
        key = (entry["home_id"], entry["device_id"], entry["hour_utc"])
        pred = pred_by_line.get(entry["line_index"], "")
        by_bucket.setdefault(key, []).append((entry.get("group_index", 0), pred))
    for k in by_bucket:
        by_bucket[k].sort(key=lambda x: x[0])

    print(f"Stage B: building 1 final narrative per bucket from {len(manifest)} partial(s).")
    print()

    n = 0
    n_warn = 0
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf:
        for home in topo.homes:
            for dev in home.all_devices:
                for hour in range(24):
                    key = (home.home_id, dev.device_id, hour)
                    partials = by_bucket.get(key, [])
                    n_partials = len(partials)

                    owner_str = dev.owner if dev.owner is not None else "shared"
                    hour_label = f"{hour:02d}:00-{(hour + 1) % 24:02d}:00 UTC"

                    if n_partials == 0:
                        # Empty bucket: emit a "no flows" record so the 576-row grid stays uniform.
                        prompt = (
                            f"Date: {date} UTC. Home: {home.home_id} ({home.label}). "
                            f"Owner: {owner_str}. Device: {dev.device_id} (type={dev.type}, mac={dev.mac}). "
                            f"Hour: {hour_label}. Flow count: 0. (No flows recorded.)"
                        )
                        data_field = "(no flows)\n\n" + INSTRUCTION_FINAL
                    else:
                        narratives = [(f"partial {gi + 1} of {n_partials}", p) for gi, p in partials]
                        header = (
                            f"=== PARTIAL HOUR NARRATIVES ({n_partials}) FOR "
                            f"{dev.device_id} IN {home.label} ON {date} UTC, "
                            f"HOUR {hour_label} ==="
                        )
                        footer = "=== END OF PARTIAL HOUR NARRATIVES ==="
                        pack = build_pack(narratives, header, footer)

                        prompt = (
                            f"Date: {date} UTC. Home: {home.home_id} ({home.label}). "
                            f"Owner: {owner_str}. Device: {dev.device_id} (type={dev.type}, mac={dev.mac}). "
                            f"Hour: {hour_label}. Synthesizing from {n_partials} partial(s)."
                        )
                        data_field = pack + "\n\n" + INSTRUCTION_FINAL

                    size = len(data_field.encode("utf-8"))
                    if size > SOFT_WARN_BYTES:
                        n_warn += 1

                    record = {
                        "system": SYSTEM_FINAL,
                        "instruction": INSTRUCTION_FINAL,
                        "prompt": prompt,
                        "inputs": [{"type": "text", "format": "plain", "data": data_field}],
                    }
                    out_f.write(json.dumps(record) + "\n")

                    mf.write(json.dumps({
                        "line_index": n,
                        "home_id": home.home_id,
                        "home_label": home.label,
                        "gateway_mac": home.gateway_mac,
                        "human": dev.owner,
                        "device_id": dev.device_id,
                        "device_type": dev.type,
                        "device_mac": dev.mac,
                        "hour_utc": hour,
                        "n_partials_consumed": n_partials,
                        "input_pack_bytes": size,
                        "date": date,
                    }) + "\n")
                    n += 1

    print()
    print(f"Wrote {n} final bucket records to {args.output}")
    print(f"Wrote sidecar manifest: {args.output_manifest}")
    if n_warn:
        print(f"WARN: {n_warn} bucket-final records exceeded the {SOFT_WARN_BYTES:,}-byte soft cap.")


if __name__ == "__main__":
    main()
