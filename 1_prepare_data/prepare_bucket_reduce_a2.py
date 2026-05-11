#!/usr/bin/env python3
"""
Stage A2 — hierarchical re-fold of Stage A partials before Stage B.

Stage B's straight concatenation of all per-bucket partials overflows the
16 KB / ~4K-token quality cliff for busy buckets (~17% at 1 GB). This script
inserts one more grouping pass between Stage A and Stage B: within each
(device, hour) bucket, partials are sorted by group_index and packed into
super-groups of --group-size (default 4). Each super-group becomes one
inference record; the model summarizes the super-group into a "super-partial"
narrative. Stage B then folds the super-partials per bucket, which stays
comfortably under the cliff.

Usage:
    python 1_prepare_data/prepare_bucket_reduce_a2.py \\
      --predictions data/predictions_bucket_reduce_a_4k.jsonl \\
      --manifest    data/manifest_bucket_reduce_a_4k.jsonl \\
      --output      data/bucket_reduce_a2_4k.jsonl \\
      --output-manifest data/manifest_bucket_reduce_a2_4k.jsonl \\
      --group-size 4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict


SYSTEM = (
    "You are a network security analyst. The text below is a sequence of "
    "partial summaries covering different chunks/slices of one device's traffic "
    "during a single hour. Stay grounded in those partial summaries — do not "
    "invent traffic that isn't stated. Synthesize patterns across the partials."
)

INSTRUCTION = (
    "Read the partial summaries above and write a single paragraph describing "
    "what this device was doing across the partials covered. Cover dominant "
    "protocols, traffic intensity, and anything unusual. 3-6 sentences. "
    "Another call will combine your output with other super-partial summaries "
    "to produce the full hour narrative."
)

SOFT_CAP_BYTES = 16_384


def load_predictions(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main():
    parser = argparse.ArgumentParser(description="Stage A2 — re-fold Stage A partials before Stage B.")
    parser.add_argument("--predictions", required=True,
                        help="Stage A predictions JSONL (one {line_index, prediction} per partial).")
    parser.add_argument("--manifest", required=True,
                        help="Stage A manifest JSONL (one entry per partial, with home_id/device_id/hour_utc/group_index).")
    parser.add_argument("--output", required=True, help="Stage A2 input JSONL to write.")
    parser.add_argument("--output-manifest", required=True, help="Stage A2 sidecar manifest to write.")
    parser.add_argument("--group-size", type=int, default=4,
                        help="Max Stage A partials per Stage A2 super-group (default: 4).")
    args = parser.parse_args()

    predictions = load_predictions(args.predictions)
    manifest = load_predictions(args.manifest)  # same shape: one JSON per line
    if len(predictions) != len(manifest):
        sys.exit(f"Length mismatch: predictions={len(predictions)}, manifest={len(manifest)}")
    print(f"Loaded {len(predictions)} Stage A partials.")

    # Join positionally; group by bucket
    by_bucket: dict[tuple, list[tuple[int, dict, str]]] = defaultdict(list)
    for mf, pr in zip(manifest, predictions):
        key = (mf["home_id"], mf["device_id"], mf["hour_utc"])
        by_bucket[key].append((mf["group_index"], mf, pr.get("prediction", "")))

    print(f"Buckets: {len(by_bucket)} (expected 576).")

    # Sort within bucket by group_index; split into super-groups
    n_records = 0
    over_cap = 0
    with open(args.output, "w") as out_f, open(args.output_manifest, "w") as mf_f:
        for key in sorted(by_bucket.keys()):
            home_id, device_id, hour_utc = key
            partials = sorted(by_bucket[key], key=lambda t: t[0])
            n_partials = len(partials)
            n_super_groups = math.ceil(n_partials / args.group_size)

            # Pull bucket-level metadata from the first partial's manifest
            first_mf = partials[0][1]
            home_label = first_mf["home_label"]
            human = first_mf.get("human")
            device_type = first_mf["device_type"]
            device_mac = first_mf["device_mac"]
            date = first_mf["date"]

            for sg_idx in range(n_super_groups):
                lo = sg_idx * args.group_size
                hi = min(lo + args.group_size, n_partials)
                slice_partials = partials[lo:hi]
                n_in_super = hi - lo

                owner_str = human if human else "shared"
                hour_label = f"{hour_utc:02d}:00-{(hour_utc + 1) % 24:02d}:00 UTC"
                prompt = (
                    f"Date: {date} UTC. Home: {home_id} ({home_label}). "
                    f"Owner: {owner_str}. Device: {device_id} (type={device_type}, mac={device_mac}). "
                    f"Hour: {hour_label}. Super-group {sg_idx + 1} of {n_super_groups} "
                    f"covering {n_in_super} partial summaries."
                )

                # Build the inputs[0].data envelope
                preamble = (
                    f"=== PARTIAL NARRATIVES ({n_in_super}) FOR {device_id} IN {home_label} "
                    f"ON {date} UTC, HOUR {hour_label}, SUPER-GROUP {sg_idx + 1} OF {n_super_groups} ==="
                )
                pieces = [preamble, ""]
                for (gi, mf, pred) in slice_partials:
                    pieces.append(f"--- partial group {gi + 1}/{mf['group_count']} ({mf['n_chunks_in_group']} chunks, {mf['input_pack_bytes']} B) ---")
                    pieces.append(pred.strip())
                    pieces.append("")
                data = "\n".join(pieces)
                input_pack_bytes = len(data.encode("utf-8"))
                if input_pack_bytes > SOFT_CAP_BYTES:
                    over_cap += 1
                    print(f"  WARN  {device_id} h{hour_utc:02d} super-group {sg_idx}: "
                          f"{input_pack_bytes:,} B > {SOFT_CAP_BYTES:,} — consider lowering --group-size")

                record = {
                    "system": SYSTEM,
                    "instruction": INSTRUCTION,
                    "prompt": prompt,
                    "inputs": [{"type": "text", "format": "plain", "data": data}],
                }
                out_f.write(json.dumps(record) + "\n")

                mf_entry = {
                    "line_index": n_records,
                    "home_id": home_id,
                    "home_label": home_label,
                    "gateway_mac": first_mf["gateway_mac"],
                    "human": human,
                    "device_id": device_id,
                    "device_type": device_type,
                    "device_mac": device_mac,
                    "hour_utc": hour_utc,
                    "super_group_index": sg_idx,
                    "super_group_count": n_super_groups,
                    "n_partials_in_super_group": n_in_super,
                    "input_pack_bytes": input_pack_bytes,
                    "date": date,
                }
                mf_f.write(json.dumps(mf_entry) + "\n")
                n_records += 1

    print(f"\nWrote {n_records} Stage A2 super-group records to {args.output}")
    print(f"Wrote sidecar manifest: {args.output_manifest}")
    if over_cap:
        print(f"WARN: {over_cap} super-group records exceeded the {SOFT_CAP_BYTES:,}-byte soft cap. "
              f"Lower --group-size if predictions degrade.")


if __name__ == "__main__":
    main()
