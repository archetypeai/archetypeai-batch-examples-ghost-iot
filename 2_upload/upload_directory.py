#!/usr/bin/env python3
"""
Upload every JSONL file in a directory to Archetype AI via the Files API.

For section 8 of the README. The per-chunk prep step writes ~37,000 small
JSONL files at 1 GB scale (one record per file, ~10 KB each). This script
uploads them concurrently and records the resulting filenames + file_uids
so the downstream batch-job script can reference all of them and the
post-download join can map output filenames back to scope metadata.

Concurrency: pass --concurrency N (default 8). Sequential at 1-2 s/upload
would mean 10-20 hours for 37K files; 8 streams takes that down to 1-2 hours.
Bump higher if the platform tolerates it.

Usage:
    python 2_upload/upload_directory.py data/per_device_hour
    python 2_upload/upload_directory.py data/per_device_hour --concurrency 16
    python 2_upload/upload_directory.py data/per_device_hour \\
      --manifest data/uploaded_file_ids.txt --concurrency 8

Idempotency: files whose names already appear in the manifest are skipped, so
you can rerun the script after a partial failure (network blip, ctrl-C) and
pick up where it left off. The manifest is written incrementally — every
successful upload is flushed to disk immediately.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Reuse the existing single-file uploader's HTTP helpers.
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)
from upload_multipart import (  # noqa: E402
    FileAlreadyExists,
    abort_upload,
    complete_upload,
    detect_file_type,
    fmt_bytes,
    initiate_upload,
    upload_part,
)


def upload_one(file_path: str) -> dict:
    """Upload a single file. Returns {filename, file_uid, file_status, size}.

    Quiet (no per-part progress) — the directory loop prints one line per file.
    """
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)

    init = initiate_upload(filename, file_size, file_type=detect_file_type(file_path))
    upload_id = init["upload_id"]
    parts_meta = init["parts"]

    completed_parts = []
    try:
        with open(file_path, "rb") as f:
            for part in parts_meta:
                f.seek(part["offset"])
                data = f.read(part["length"])
                etag = upload_part(part["url"], data, part["length"])
                completed_parts.append({"part_number": part["part_number"], "part_token": etag})
    except Exception:
        abort_upload(upload_id)
        raise

    result = complete_upload(upload_id, completed_parts)
    return {
        "filename": filename,
        "file_uid": result.get("file_uid", init.get("file_uid", "")),
        "file_status": result.get("file_status", "unknown"),
        "size": file_size,
    }


def main():
    parser = argparse.ArgumentParser(description="Upload a directory of JSONL files via the Files API.")
    parser.add_argument("directory", help="Directory containing JSONL files to upload")
    parser.add_argument("--pattern", default="*.jsonl", help="Glob pattern within the directory (default: %(default)s)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Number of concurrent uploads (default: 8). Each worker handles one file at a time. "
                             "37K sequential uploads at 1-2s each = 10-20 hours; 8 streams ≈ 1-2 hours.")
    parser.add_argument("--manifest", default="data/uploaded_file_ids.txt",
                        help="Manifest file recording uploaded filenames, one per line. "
                             "Used for skip-on-rerun and as input to create_activity_detection_job.py --file-list. "
                             "A sibling .jsonl with the same stem is also written, recording filename + file_uid "
                             "(needed to join downloaded outputs back to scope metadata). (default: %(default)s)")
    args = parser.parse_args()

    repo_dir = os.path.dirname(THIS_DIR)
    directory = args.directory if os.path.isabs(args.directory) else os.path.join(repo_dir, args.directory)
    manifest = args.manifest if os.path.isabs(args.manifest) else os.path.join(repo_dir, args.manifest)

    if not os.path.isdir(directory):
        sys.exit(f"Not a directory: {directory}")

    paths = sorted(glob.glob(os.path.join(directory, args.pattern)))
    if not paths:
        sys.exit(f"No files matched {args.pattern} in {directory}")

    already_uploaded: set[str] = set()
    if os.path.exists(manifest):
        with open(manifest) as f:
            for line in f:
                name = line.strip()
                if name:
                    already_uploaded.add(name)

    todo = [p for p in paths if os.path.basename(p) not in already_uploaded]
    skipping = len(paths) - len(todo)
    total_bytes = sum(os.path.getsize(p) for p in todo)

    print("=" * 60)
    print(" Upload Directory to Archetype AI Files API")
    print("=" * 60)
    print(f" Source:      {directory}")
    print(f" Manifest:    {manifest}")
    print(f" Files:       {len(paths)} matched ({skipping} already uploaded, {len(todo)} todo)")
    print(f" Bytes:       {fmt_bytes(total_bytes)} to transfer")
    print(f" Concurrency: {args.concurrency} parallel streams")
    print("=" * 60)
    print()

    if not todo:
        print("Nothing to upload — manifest already covers every matching file.")
        return

    os.makedirs(os.path.dirname(manifest) or ".", exist_ok=True)
    manifest_jsonl = manifest.rsplit(".", 1)[0] + ".jsonl" if "." in os.path.basename(manifest) else manifest + ".jsonl"

    # Locked, append-mode handles so concurrent workers can write incrementally.
    mf = open(manifest, "a")
    mfj = open(manifest_jsonl, "a")
    write_lock = threading.Lock()

    t0 = time.time()
    counters = {"done": 0, "uploaded": 0, "exists": 0, "failed": 0, "bytes": 0}
    failures: list[tuple[str, str]] = []
    counters_lock = threading.Lock()
    print_lock = threading.Lock()

    def handle(path: str) -> None:
        filename = os.path.basename(path)
        t_start = time.time()
        try:
            result = upload_one(path)
            elapsed = time.time() - t_start
            with write_lock:
                mf.write(filename + "\n")
                mf.flush()
                mfj.write(json.dumps({
                    "filename": filename,
                    "file_uid": result["file_uid"],
                    "file_status": result["file_status"],
                    "size": result["size"],
                }) + "\n")
                mfj.flush()
            with counters_lock:
                counters["uploaded"] += 1
                counters["done"] += 1
                counters["bytes"] += result["size"]
                done = counters["done"]
                bytes_done = counters["bytes"]
            overall = time.time() - t0
            rate_kbs = bytes_done / max(overall, 1e-6) / 1024
            with print_lock:
                if done <= 5 or done % 100 == 0 or done == len(todo):
                    print(f"  [{done:>5}/{len(todo)}] {filename:<60}  "
                          f"{fmt_bytes(result['size']):>8}  {elapsed:5.2f}s  "
                          f"({rate_kbs:5.0f} KB/s overall)")
        except FileAlreadyExists:
            with write_lock:
                mf.write(filename + "\n")
                mf.flush()
                mfj.write(json.dumps({
                    "filename": filename,
                    "file_uid": "",
                    "file_status": "exists",
                    "size": os.path.getsize(path),
                }) + "\n")
                mfj.flush()
            with counters_lock:
                counters["exists"] += 1
                counters["done"] += 1
                done = counters["done"]
            with print_lock:
                if done <= 5 or done % 500 == 0 or done == len(todo):
                    print(f"  [{done:>5}/{len(todo)}] {filename:<60}  exists on platform, skipped")
        except Exception as e:
            with counters_lock:
                counters["failed"] += 1
                counters["done"] += 1
                done = counters["done"]
            failures.append((filename, str(e)))
            with print_lock:
                print(f"  [{done:>5}/{len(todo)}] {filename:<60}  FAILED: {e}")

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            list(as_completed([ex.submit(handle, p) for p in todo]))
    except KeyboardInterrupt:
        print("\nInterrupted — already-uploaded files are recorded in the manifest. "
              "Re-run to resume.")
        raise
    finally:
        mf.close()
        mfj.close()

    elapsed = time.time() - t0
    print()
    print("=" * 60)
    print(f" Uploaded {counters['uploaded']}/{len(todo)} files "
          f"({fmt_bytes(counters['bytes'])}) in {elapsed:.1f}s "
          f"({elapsed/60:.1f} min, avg {counters['uploaded']/max(elapsed, 1e-6):.1f} files/sec)")
    if counters["exists"]:
        print(f" {counters['exists']} already existed on platform from a prior session — "
              f"recorded in manifest with file_uid=\"\".")
        print( "   For these files, the post-download join in extract_predictions.py will")
        print( "   not be able to match outputs to file_ids. Either rename + re-upload, or")
        print( "   try `python cleanup.py --remote` first.")
    if counters["failed"]:
        print(f" {counters['failed']} failure(s):")
        for fn, err in failures[:10]:
            print(f"   - {fn}: {err}")
        print(" Re-run this command to retry the failures (already-uploaded files are skipped).")
    print(f" Manifest: {manifest}")
    print(f"           {manifest_jsonl}")
    print("=" * 60)


if __name__ == "__main__":
    main()
