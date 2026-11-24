"""Shared config and HTTP helpers for the Archetype AI platform API (2.0).

Every endpoint lives under {root}/api: files, batch jobs, events, outputs.
ATAI_API_ENDPOINT may be the bare root (https://platform.u1.archetypeai.app)
or end in /api; a legacy /v0.5 suffix is stripped.

Settings come from the shell first, then from the repo's .env. When both set
a variable to different values the shell wins and a warning names it, so a
stale `export ATAI_API_ENDPOINT=...` can't silently send requests elsewhere.
"""

from __future__ import annotations

import os
import re
import sys

import requests

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(REPO_DIR, ".env")


def load_env(path: str = ENV_PATH) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("\"'")
            if k in os.environ and os.environ[k] != v:
                print(f"warning: {k} from your shell overrides .env (unset it to use .env)",
                      file=sys.stderr)
            os.environ.setdefault(k, v)


def api_base(endpoint: str) -> str:
    e = endpoint.strip().rstrip("/")
    e = re.sub(r"/v[0-9]+(\.[0-9]+)*$", "", e)
    e = re.sub(r"/api$", "", e)
    return e + "/api"


load_env()
API_KEY = os.environ.get("ATAI_API_KEY", "")
API_ENDPOINT = os.environ.get("ATAI_API_ENDPOINT", "")
if not API_KEY or not API_ENDPOINT:
    sys.exit("Set ATAI_API_KEY and ATAI_API_ENDPOINT (see .env.example).")
BASE_URL = api_base(API_ENDPOINT)
AUTH = {"Authorization": f"Bearer {API_KEY}"}


def list_all(path: str, params: dict | None = None, limit: int = 100) -> list:
    """Every item of a cursor-paginated list, oldest first.

    2.0 lists return {data, has_more, next_cursor}, newest first; the next
    (older) page is ?after=<next_cursor>. There is no offset or total.
    """
    items: list = []
    after = None
    while True:
        q = {**(params or {}), "limit": limit, **({"after": after} if after else {})}
        resp = requests.get(f"{BASE_URL}{path}", headers=AUTH, params=q, timeout=60)
        resp.raise_for_status()
        page = resp.json()
        items.extend(page.get("data") or [])
        after = page.get("next_cursor")
        if not page.get("has_more") or not after:
            break
    return list(reversed(items))


def fetch_ref(ref: str) -> bytes:
    """Download a batch output. `ref` is a root path such as
    /api/files/fil_.../content (needs the API key); absolute URLs are presigned."""
    if ref.startswith("/"):
        url = BASE_URL + re.sub(r"^/(api|v[0-9]+(\.[0-9]+)*)(?=/)", "", ref)
        resp = requests.get(url, headers=AUTH, timeout=300)
    else:
        resp = requests.get(ref, timeout=300)
    resp.raise_for_status()
    return resp.content
