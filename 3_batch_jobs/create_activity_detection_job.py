#!/usr/bin/env python3
"""
Create and monitor an Activity Detection batch job on the Archetype AI platform.

Runs Newton's language generation on a JSONL input file to produce natural
language descriptions. For this repo, the two expected inputs are:

    ghost_iot_home_yesterday.jsonl     (1 prompt  — home-level summary)
    ghost_iot_devices_yesterday.jsonl  (N prompts — one per active device)

Usage:
    python create_activity_detection_job.py                                      # default: home
    python create_activity_detection_job.py --file ghost_iot_devices_yesterday.jsonl
    python create_activity_detection_job.py --file ghost_iot_home_yesterday.jsonl --name my-run

Flow:
    1. POST /v0.5/batch/jobs          -> create batch job
    2. GET  /v0.5/batch/jobs/{id}     -> poll status
    3. GET  /v0.5/batch/jobs/{id}/events -> view logs
"""

import argparse
import os
import time

import requests

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

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


def build_payload(file_id: str, name: str) -> dict:
    return {
        "name": name,
        "pipeline_type": "batch",
        "pipeline_key": "activity-detection",
        "inputs": {
            "worker.data": [{"file_id": file_id}],
        },
        "parameters": {
            "worker": {
                "parallelism": 1,
                "config": {
                    "generation": {
                        "do_sample": True,
                        "max_new_tokens": 256,
                        "repetition_penalty": 1,
                        "temperature": 0.7,
                        "top_k": 20,
                        "top_p": 0.8,
                    },
                },
            }
        },
    }


def create_job(payload: dict) -> dict:
    resp = requests.post(
        f"{BASE_URL}/batch/jobs",
        headers={**AUTH, "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def get_job(job_id: str) -> dict:
    resp = requests.get(f"{BASE_URL}/batch/jobs/{job_id}", headers=AUTH)
    resp.raise_for_status()
    return resp.json()


def get_events(job_id: str, limit: int = 100) -> dict:
    resp = requests.get(
        f"{BASE_URL}/batch/jobs/{job_id}/events",
        headers=AUTH,
        params={"limit": limit},
    )
    resp.raise_for_status()
    return resp.json()


def default_job_name(file_id: str) -> str:
    stem = file_id
    if stem.endswith(".jsonl"):
        stem = stem[:-len(".jsonl")]
    stem = stem.replace("_", "-")
    return f"{stem}-activity-detection"


def main():
    parser = argparse.ArgumentParser(description="Create an Activity Detection batch job.")
    parser.add_argument("--file", default="ghost_iot_home_yesterday.jsonl",
                        help="file_id (filename as registered on the platform)")
    parser.add_argument("--name", default=None,
                        help="Job name (default: derived from --file)")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Seconds between status polls (default: 10)")
    args = parser.parse_args()

    name = args.name or default_job_name(args.file)
    payload = build_payload(args.file, name)

    print("=" * 60)
    print(" Archetype AI Activity Detection Job")
    print("=" * 60)
    print(f" file_id: {args.file}")
    print(f" name:    {name}")
    print()

    print("[1/3] Creating activity detection job...")
    job = create_job(payload)
    job_id = job["id"]

    print(f"      job_id:   {job_id}")
    print(f"      name:     {job['name']}")
    print(f"      pipeline: {job['pipeline_key']} v{job.get('pipeline_version', '?')}")
    print(f"      status:   {job['status']}")
    print()

    print("[2/3] Monitoring job status...")
    prev_status = None
    while True:
        job = get_job(job_id)
        status = job["status"]
        if status != prev_status:
            print(f"      [{time.strftime('%H:%M:%S')}] {status}")
            prev_status = status
        if status in TERMINAL_STATUSES:
            break
        time.sleep(args.poll_interval)
    print()

    print("[3/3] Job events:")
    events = get_events(job_id)
    for event in reversed(events.get("events", [])):
        level = event["level"]
        msg = event["message"]
        ts = event["created_at"][11:19]
        marker = "!!" if level == "ERROR" else "  "
        print(f"  {marker} [{ts}] {level:<5} {msg}")

    print()
    print("=" * 60)
    print(f" Job {job_id}")
    print(f" Status: {job['status']}")
    if job.get("completed_at"):
        print(f" Completed: {job['completed_at']}")
    if job.get("failed_at"):
        print(f" Failed:    {job['failed_at']}")
    print("=" * 60)
    print()
    print("Next: download outputs with")
    print(f"  python 4_download_outputs/download_outputs.py {job_id} outputs/{name}")


if __name__ == "__main__":
    main()
