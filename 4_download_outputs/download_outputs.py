#!/usr/bin/env python3
"""
Download batch job output artifacts from the Archetype AI platform.

Usage:
    python download_outputs.py <job_id> [output_dir]
    python download_outputs.py job_6pgect4qqc8h0sd6v3rva23y8g outputs/
"""

import os
import sys
import time


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from atai_api import fetch_ref, list_all  # noqa: E402


def get_outputs(job_id: str) -> list:
    """Fetch all output metadata (cursor-paginated)."""
    return list_all(f"/batch/jobs/{job_id}/outputs")


def download_file(ref: str, dest: str) -> int:
    """Download one output (data.ref is an /api/files/.../content path)."""
    content = fetch_ref(ref)
    with open(dest, "wb") as f:
        f.write(content)
    return len(content)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <job_id> [output_dir]")
        sys.exit(1)

    job_id = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else f"outputs/{job_id}"
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print(" Download Batch Job Outputs")
    print("=" * 60)
    print(f"  Job:    {job_id}")
    print(f"  Saving: {output_dir}/")
    print()

    # Step 1: Fetch output metadata
    print("[1/2] Fetching output list...")
    outputs = get_outputs(job_id)
    print(f"  Total: {len(outputs)} files")
    print()

    # Step 2: Download files
    print(f"[2/2] Downloading {len(outputs)} files...")
    t0 = time.time()
    total_bytes = 0

    for i, out in enumerate(outputs):
        ref = out["data"]["ref"]
        filename = out["data"]["filename"]
        dest = os.path.join(output_dir, filename)

        nbytes = download_file(ref, dest)
        total_bytes += nbytes

        if (i + 1) % 50 == 0 or i == 0 or i == len(outputs) - 1:
            elapsed = time.time() - t0
            pct = (i + 1) / len(outputs) * 100
            speed = total_bytes / elapsed / 1024 / 1024 if elapsed > 0 else 0
            print(f"  [{pct:5.1f}%] {i + 1}/{len(outputs)}  {filename}  ({speed:.1f} MB/s)")

    elapsed = time.time() - t0
    print()
    print(f"  Done! {len(outputs)} files, {total_bytes / 1024 / 1024:.1f} MB in {elapsed:.1f}s")
    print(f"  Saved to: {output_dir}/")


if __name__ == "__main__":
    main()
