"""Cached, throttled access to the UFA stats API (backend.ufastats.com).

A cooperative JSON API — no WAF machinery needed. Every response is cached
on disk under data/raw/ufa/ so re-runs are free and live traffic stays low.
"""

import hashlib
import json
import time
from pathlib import Path

import requests

BASE_URL = "https://www.backend.ufastats.com/api/v1"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "ufa"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
RATE_LIMIT_SECONDS = 0.3

_last_request = 0.0


def get(endpoint: str, params: dict, session=None, refresh: bool = False) -> list:
    """GET /api/v1/<endpoint>, returning the parsed 'data' list (disk-cached)."""
    global _last_request
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    url = f"{BASE_URL}/{endpoint}?{query}"
    path = RAW_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())["data"]
    wait = _last_request + RATE_LIMIT_SECONDS - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    req = session or requests
    resp = req.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if "data" not in payload:
        raise ValueError(f"unexpected response from {url}: {str(payload)[:200]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload["data"]
