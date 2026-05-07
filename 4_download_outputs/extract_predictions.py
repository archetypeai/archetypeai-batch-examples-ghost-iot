#!/usr/bin/env python3
"""
Join a directory of downloaded batch-job outputs back to their input file_ids.

After download_outputs.py drops a bunch of `inp_<HASH>_output.jsonl` files into
an outputs/<job_name>/ directory, this script produces a single keyed JSONL:

    {"file_id": "dev_home_a__alice_phone__h13.jsonl",
     "prediction": "...",
     "line_index": 0}

so the section-9 reduce-stage prep scripts can group predictions by scope
without caring about how the platform names its output artifacts.

Strategy:
  Each output file is named `inp_<HASH>_output.jsonl`, where <HASH> is the
  platform's `file_uid` for the input it was generated from. The upload step
  (2_upload/upload_directory.py) records (filename, file_uid) pairs in a
  sibling `.jsonl` manifest. We extract <HASH> from each output filename, look
  it up in the upload manifest to get the original filename, and pair it with
  the prediction text inside the file.

If the output filename hash does not exactly match a recorded `file_uid`, we
fall back to substring matching (sometimes the platform emits a truncated or
prefixed form). Failures are reported but don't abort — partial joins are
still useful.

Usage:
    python 4_download_outputs/extract_predictions.py \\
      outputs/multihome-per-device-hour \\
      --upload-manifest data/uploaded_file_ids.jsonl \\
      --output data/predictions_per_device_hour.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


HASH_RE = re.compile(r"^inp_([A-Za-z0-9]+)_output\.jsonl$")


def load_upload_manifest(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    if not out:
        sys.exit(f"Upload manifest is empty: {path}")
    return out


def find_filename_for_hash(uid_hash: str, manifest: list[dict]) -> str | None:
    # 1) exact match on file_uid
    for entry in manifest:
        if entry.get("file_uid") == uid_hash:
            return entry["filename"]
    # 2) substring (file_uid contains hash)
    for entry in manifest:
        fu = entry.get("file_uid") or ""
        if uid_hash and uid_hash in fu:
            return entry["filename"]
    # 3) reverse substring (hash contains file_uid)
    for entry in manifest:
        fu = entry.get("file_uid") or ""
        if fu and fu in uid_hash:
            return entry["filename"]
    return None


def main():
    parser = argparse.ArgumentParser(description="Join downloaded batch outputs to input file_ids.")
    parser.add_argument("predictions_dir", help="Directory containing inp_<HASH>_output.jsonl files")
    parser.add_argument("--upload-manifest", required=True,
                        help="data/uploaded_file_ids.jsonl with {filename, file_uid} per line")
    parser.add_argument("--output", required=True, help="Where to write the joined predictions JSONL")
    args = parser.parse_args()

    if not os.path.isdir(args.predictions_dir):
        sys.exit(f"Not a directory: {args.predictions_dir}")

    manifest = load_upload_manifest(args.upload_manifest)
    print(f"Loaded {len(manifest)} entries from {args.upload_manifest}")

    output_files = sorted(glob.glob(os.path.join(args.predictions_dir, "inp_*_output.jsonl")))
    print(f"Found {len(output_files)} output files in {args.predictions_dir}/")
    print()

    if not output_files:
        sys.exit("No output files matched 'inp_*_output.jsonl' — wrong directory?")

    matched = 0
    unmatched: list[str] = []
    n_predictions = 0

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as out_f:
        for path in output_files:
            name = os.path.basename(path)
            m = HASH_RE.match(name)
            if not m:
                unmatched.append(name)
                continue
            uid_hash = m.group(1)
            file_id = find_filename_for_hash(uid_hash, manifest)
            if file_id is None:
                unmatched.append(name)
                continue

            matched += 1
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    out_rec = {
                        "file_id": file_id,
                        "line_index": rec.get("line_index", 0),
                        "prediction": rec.get("prediction", ""),
                    }
                    out_f.write(json.dumps(out_rec) + "\n")
                    n_predictions += 1

    print(f"Matched {matched}/{len(output_files)} output files to file_ids.")
    print(f"Wrote {n_predictions} prediction records to {args.output}")
    if unmatched:
        print()
        print(f"WARNING: {len(unmatched)} output files could not be matched to a file_uid.")
        print("First few unmatched:")
        for n in unmatched[:5]:
            print(f"  {n}")
        print("Check that --upload-manifest covers every file submitted to this job.")


if __name__ == "__main__":
    main()
