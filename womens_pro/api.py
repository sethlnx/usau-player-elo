"""Cached access to the PUL Stats Hub JSON API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE_URL = "https://pul-stats-hub.pages.dev"
MANIFEST_URL = f"{BASE_URL}/api/v1/index.json"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "womens_pro" / "pul"
USER_AGENT = "usau-player-elo/1.0 (women's professional ultimate stats importer)"


def endpoint_url(path: str) -> str:
    """Resolve a manifest path while refusing off-site endpoints."""
    url = urljoin(f"{BASE_URL}/", path)
    parsed = urlparse(url)
    base = urlparse(BASE_URL)
    if parsed.scheme != "https" or parsed.netloc != base.netloc:
        raise ValueError(f"PUL manifest endpoint is outside {BASE_URL}: {path}")
    return url


def get_json(
    url: str,
    session=None,
    *,
    refresh: bool = False,
    raw_dir: Path = RAW_DIR,
) -> dict:
    """Return a JSON object from the PUL API, cached by absolute URL."""
    path = raw_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.json"
    if path.exists() and not refresh:
        payload = json.loads(path.read_text())
    else:
        client = session or requests
        response = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, separators=(",", ":")))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object from {url}")
    return payload
