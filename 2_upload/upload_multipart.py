#!/usr/bin/env python3
"""
Multipart file upload to Archetype AI using presigned URLs.

Usage:
    python upload_multipart.py data/volve_inference.csv

Flow:
    1. POST /v0.5/files/uploads/initiate  -> get presigned URLs
    2. PUT each part to S3                 -> collect ETags
    3. POST /v0.5/files/uploads/{id}/complete -> finalize
"""

import json
import os
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
with open(ENV_PATH) as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v

API_KEY = os.environ["ATAI_API_KEY"]
API_ENDPOINT = os.environ["ATAI_API_ENDPOINT"]
BASE_URL = f"{API_ENDPOINT}/v0.5"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Platform-supported MIME types (per /files/uploads/initiate validation):
#   image/jpeg, image/png, video/mp4, text/csv, text/plain,
#   application/json, application/x-ndjson
# JSONL is newline-delimited JSON → application/x-ndjson.
EXT_TO_FILE_TYPE = {
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".json":  "application/json",
    ".csv":   "text/csv",
    ".txt":   "text/plain",
}


def detect_file_type(path: str) -> str:
    """Pick a content-type from the file extension. The platform stores this
    on the uploaded file and uses it when later jobs read the file — getting
    it wrong (e.g. uploading a .jsonl as text/csv) makes the batch worker fail
    with `(item count unknown) → Processing failed`."""
    ext = os.path.splitext(path)[1].lower()
    return EXT_TO_FILE_TYPE.get(ext, "application/octet-stream")


def fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.0f} MB"
    return f"{n / 1024:.0f} KB"


def progress_bar(current: int, total: int, width: int = 40) -> str:
    pct = current / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:6.1%}"


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
class FileAlreadyExists(Exception):
    """Raised when /files/uploads/initiate returns HTTP 409 Conflict because
    a file with the same filename has already been uploaded under this org.
    The platform doesn't expose a per-file delete endpoint, so the only
    options are: skip the re-upload (file is already there), rename the local
    file, or use a different filename when calling initiate_upload."""

    def __init__(self, filename: str, response_body: str = ""):
        self.filename = filename
        self.response_body = response_body
        super().__init__(f"File already exists on platform: {filename}")


def initiate_upload(filename: str, file_size: int, file_type: str = "application/octet-stream") -> dict:
    resp = requests.post(
        f"{BASE_URL}/files/uploads/initiate",
        headers={**AUTH, "Content-Type": "application/json"},
        json={"filename": filename, "file_type": file_type, "num_bytes": file_size},
    )
    if resp.status_code == 409:
        raise FileAlreadyExists(filename, resp.text)
    if not resp.ok:
        print(f"  HTTP {resp.status_code} from /files/uploads/initiate")
        print(f"  Response body: {resp.text}")
    resp.raise_for_status()
    return resp.json()


def upload_part(url: str, data: bytes, length: int) -> str:
    """Upload a single part and return its ETag."""
    resp = requests.put(url, data=data, headers={"Content-Length": str(length)})
    resp.raise_for_status()
    return resp.headers.get("ETag", "").strip('"')


def complete_upload(upload_id: str, parts: list) -> dict:
    resp = requests.post(
        f"{BASE_URL}/files/uploads/{upload_id}/complete",
        headers={**AUTH, "Content-Type": "application/json"},
        json={"parts": parts},
    )
    resp.raise_for_status()
    return resp.json()


def abort_upload(upload_id: str):
    requests.post(f"{BASE_URL}/files/uploads/{upload_id}/abort", headers=AUTH)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multipart upload to Archetype AI Files API.")
    parser.add_argument("file_path", help="Local file to upload")
    parser.add_argument("--file-type", default=None,
                        help="Override content-type sent to /files/uploads/initiate. "
                             "By default it's auto-detected from the file extension "
                             "(.jsonl/.ndjson → application/x-ndjson, .json → "
                             "application/json, .csv → text/csv, .txt → text/plain). "
                             "Platform-supported types: image/jpeg, image/png, "
                             "video/mp4, text/csv, text/plain, application/json, "
                             "application/x-ndjson. Getting this wrong is the most "
                             "common cause of `(item count unknown) → Processing "
                             "failed` later — see README §10.1.")
    args = parser.parse_args()

    file_path = args.file_path
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    file_type = args.file_type or detect_file_type(file_path)

    print(f"{'='*60}")
    print(f" Archetype AI Multipart Upload")
    print(f"{'='*60}")
    print(f" File:      {filename}")
    print(f" Size:      {fmt_bytes(file_size)} ({file_size:,} bytes)")
    print(f" Type:      {file_type}")
    print(f" Endpoint:  {BASE_URL}")
    print(f"{'='*60}")
    print()

    # --- Step 1: Initiate ---------------------------------------------------
    print("[1/3] Initiating upload...")
    try:
        init = initiate_upload(filename, file_size, file_type=file_type)
    except FileAlreadyExists as exc:
        print()
        print(f"  File '{exc.filename}' already exists on the platform.")
        print( "  Skipping upload — you can reference it directly in batch jobs:")
        print(f"      python 3_batch_jobs/create_activity_detection_job.py --file {exc.filename}")
        print()
        print( "  If you need to upload a fresh copy, rename your local file first")
        print( "  (the platform does not expose a per-file delete endpoint).")
        sys.exit(0)

    required = ("upload_id", "file_uid", "num_parts", "parts")
    missing = [k for k in required if k not in init]
    if missing:
        print()
        print(f"  Unexpected response shape from /files/uploads/initiate — missing field(s): {missing}")
        print(f"  Full response body:")
        print(f"    {json.dumps(init, indent=2)}")
        print()
        print( "  Likely causes: the file is in a partial-upload state on the platform")
        print( "  (e.g. an earlier session uploaded but didn't complete), or the API has")
        print( "  changed shape. If the file is partially uploaded, try aborting via")
        print( "  POST /v0.5/files/uploads/{upload_id}/abort or use a different filename.")
        sys.exit(1)

    upload_id = init["upload_id"]
    file_uid = init["file_uid"]
    strategy = init.get("strategy", "unknown")
    num_parts = init["num_parts"]
    part_size = init.get("part_size", file_size)
    parts = init["parts"]

    print(f"      upload_id : {upload_id}")
    print(f"      file_uid  : {file_uid}")
    print(f"      strategy  : {strategy}")
    print(f"      parts     : {num_parts} x {fmt_bytes(part_size)}")
    print(f"      expires_at: {init.get('expires_at', 'N/A')}")
    print()

    # --- Step 2: Upload parts ------------------------------------------------
    print(f"[2/3] Uploading {num_parts} parts to S3...")
    print()

    completed_parts = []
    bytes_uploaded = 0
    upload_start = time.time()

    try:
        with open(file_path, "rb") as f:
            for part in parts:
                part_num = part["part_number"]
                offset = part["offset"]
                length = part["length"]

                f.seek(offset)
                data = f.read(length)

                part_start = time.time()
                etag = upload_part(part["url"], data, length)
                part_elapsed = time.time() - part_start

                bytes_uploaded += length
                part_speed = length / part_elapsed / 1024 / 1024
                overall_elapsed = time.time() - upload_start
                overall_speed = bytes_uploaded / overall_elapsed / 1024 / 1024
                eta = (file_size - bytes_uploaded) / (bytes_uploaded / overall_elapsed) if bytes_uploaded else 0

                print(f"  Part {part_num:>2}/{num_parts}  "
                      f"{progress_bar(bytes_uploaded, file_size)}  "
                      f"{fmt_bytes(bytes_uploaded):>8}/{fmt_bytes(file_size)}  "
                      f"{part_speed:5.1f} MB/s  "
                      f"ETA {eta:5.0f}s")

                completed_parts.append({"part_number": part_num, "part_token": etag})

    except Exception as e:
        print(f"\n  Upload FAILED at part {part_num}: {e}")
        print(f"  Aborting upload {upload_id}...")
        abort_upload(upload_id)
        sys.exit(1)

    total_time = time.time() - upload_start
    avg_speed = file_size / total_time / 1024 / 1024
    print()
    print(f"      All parts uploaded in {total_time:.1f}s (avg {avg_speed:.1f} MB/s)")
    print()

    # --- Step 3: Complete ----------------------------------------------------
    print("[3/3] Completing upload...")
    result = complete_upload(upload_id, completed_parts)
    print(f"      {json.dumps(result, indent=6)}")
    print()
    print(f"{'='*60}")
    print(f" DONE  file_uid: {result.get('file_uid', file_uid)}")
    print(f"       status:   {result.get('file_status', 'unknown')}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
