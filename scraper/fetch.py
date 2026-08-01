"""Cached, rate-limited HTTP access to play.usaultimate.org.

All raw HTML is cached on disk under data/raw/ so parsing is repeatable
without re-fetching. POST results (WebForms postbacks) are cached keyed on
a stable digest of the form data.
"""

import hashlib
import json
import os
import random
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup


class SiteBlocked(Exception):
    """Raised when the WAF is 5xx-ing everything — time to switch VPN and resume."""


class NotFound(Exception):
    """Raised on a 404, including ones replayed from the negative cache."""


class UrlUnavailable(Exception):
    """One URL keeps failing while the site as a whole is answering fine.

    Some event pages reset the connection on every request — a server-side
    fault, not a block. `Denver-Round-Robin-2019` does it consistently while
    every neighbouring event returns 200. Conflating that with a WAF block
    halts an entire backfill on one bad row, so it is raised separately and
    build_db skips the event and carries on.
    """


BASE_URL = "https://play.usaultimate.org"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# The site WAF hard-blocks (500s on everything) if hit too fast for too long.
# Base delay is tunable via USAU_RATE_LIMIT; a random 0..jitter is added on top.
# Faster pace = more throughput but earlier blocks (resumable, so VPN-switch and rerun).
RATE_LIMIT_SECONDS = float(os.environ.get("USAU_RATE_LIMIT", "1.5"))
JITTER_SECONDS = float(os.environ.get("USAU_JITTER", "0.75"))
# Optional file holding a single float: the current base delay. Re-read (on
# mtime change) before every request, so a long backfill can be ramped up or
# backed off WHILE it runs instead of only between attempts. Absent or
# unreadable, RATE_LIMIT_SECONDS stands.
RATE_FILE = os.environ.get("USAU_RATE_FILE")
# Probes to confirm a block is sustained (not a one-off 500), then bail to the
# caller so a human can switch VPN. Default stays short (no grinding through
# long sleeps), but observed WAF behavior is a count budget that refills over
# time, so USAU_BLOCK_PROBES can opt into a longer escalating schedule.
BLOCK_PROBE_SECONDS = tuple(
    int(s) for s in os.environ.get("USAU_BLOCK_PROBES", "5,15,30").split(","))
# Optional egress proxy, e.g. a Mullvad per-server SOCKS5
# (socks5h://us-sea-wg-socks5-001.relays.mullvad.net:1080). Lets a backfill
# rotate exit IPs per run without reconnecting the tunnel (which stalls every
# other connection on the machine). Applies to ALL requests INCLUDING the
# control probe: the block classifier must look through the same exit the data
# requests use, or a blocked exit reads as "site fine" and healthy events get
# wrongly skipped as UrlUnavailable.
_PROXY = os.environ.get("USAU_PROXY")
PROXIES = {"http": _PROXY, "https": _PROXY} if _PROXY else None

_last_request_time = 0.0
_live_request_count = 0  # network requests this process (cache hits excluded)
_run_started = time.monotonic()
_rate_cache = (0.0, RATE_LIMIT_SECONDS)   # (mtime, delay)


def _current_delay() -> float:
    global _rate_cache
    if not RATE_FILE:
        return RATE_LIMIT_SECONDS
    try:
        mtime = os.stat(RATE_FILE).st_mtime
        if mtime != _rate_cache[0]:
            with open(RATE_FILE) as f:
                _rate_cache = (mtime, max(0.0, float(f.read().strip())))
        return _rate_cache[1]
    except (OSError, ValueError):
        return RATE_LIMIT_SECONDS


def _throttle():
    global _last_request_time
    base = _current_delay()
    wait = _last_request_time + base + random.uniform(0, JITTER_SECONDS) - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


CONTROL_URL = BASE_URL + "/events/tournament/"


def _site_reachable() -> bool:
    """Is the site answering at all? Used to tell a bad URL from a WAF block."""
    try:
        r = requests.get(CONTROL_URL, headers={"User-Agent": USER_AGENT},
                         timeout=30, proxies=PROXIES)
        return r.status_code < 500
    except (requests.Timeout, requests.ConnectionError):
        return False


def _request_with_backoff(send) -> requests.Response:
    """Run send(); retry a lone failure, then decide WHY it failed.

    The WAF blocks two ways: 500s on everything, or hanging connections until
    they time out. But an individual event page can also reset the connection
    forever while the rest of the site is perfectly healthy. Those need
    opposite responses — stop and switch exit, versus skip this row and carry
    on — so before entering the probe ladder we ask a control URL which case
    this is. Without that check one broken event halts a whole backfill, and
    the operator goes hunting for a block that was never there.
    """
    global _live_request_count, _last_request_time
    _throttle()
    _live_request_count += 1

    def attempt():
        try:
            return send(), None
        except (requests.Timeout, requests.ConnectionError) as e:
            return None, e

    resp, err = attempt()
    if resp is not None and resp.status_code < 500:
        resp.raise_for_status()
        return resp
    what = str(resp.status_code) if resp is not None else type(err).__name__
    elapsed = time.monotonic() - _run_started
    print(f"    [fetch] first {what} at live request #{_live_request_count}, "
          f"{elapsed:.0f}s into run", flush=True)

    if _site_reachable():
        time.sleep(2)                      # one more go, in case it was a blip
        _last_request_time = time.monotonic()
        resp, err = attempt()
        if resp is not None and resp.status_code < 500:
            resp.raise_for_status()
            return resp
        what2 = str(resp.status_code) if resp is not None else type(err).__name__
        print(f"    [fetch] {what2} again, but {CONTROL_URL} is fine "
              f"— treating as a bad URL, not a block", flush=True)
        raise UrlUnavailable(f"{what2} on this URL while the site is up")

    # confirm the block is real with escalating probes
    for probe in BLOCK_PROBE_SECONDS:
        print(f"    [fetch] {what} — probing again in {probe}s", flush=True)
        time.sleep(probe)
        _last_request_time = time.monotonic()
        resp, err = attempt()
        if resp is not None and resp.status_code < 500:
            resp.raise_for_status()
            return resp
        what = str(resp.status_code) if resp is not None else type(err).__name__
    raise SiteBlocked(
        f"WAF still failing ({what}) after {len(BLOCK_PROBE_SECONDS)} probes — "
        "switch VPN location and re-run the backfill to resume from cache.")


def _cache_path(url: str, data: dict | None = None) -> Path:
    key = url if data is None else url + "\x00" + json.dumps(data, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return RAW_DIR / "cache" / f"{digest}.html"


def cached_date(url: str) -> str | None:
    """ISO date the cached copy of url was written, or None if not cached."""
    path = _cache_path(url)
    return date.fromtimestamp(path.stat().st_mtime).isoformat() if path.exists() else None


def get(url: str, session: requests.Session | None = None, refresh: bool = False) -> str:
    """GET a URL, serving from the on-disk cache unless refresh=True.

    404s are cached too (as a .404 sentinel) and replayed as NotFound, so
    resume runs don't spend live requests re-confirming missing pages.
    """
    path = _cache_path(url)
    miss = path.with_suffix(".404")
    if not refresh:
        if path.exists():
            return path.read_text()
        if miss.exists():
            raise NotFound(url)
    req = session or requests
    try:
        resp = _request_with_backoff(
            lambda: req.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=60, proxies=PROXIES))
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            miss.parent.mkdir(parents=True, exist_ok=True)
            miss.write_text("")
            raise NotFound(url) from e
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resp.text)
    miss.unlink(missing_ok=True)
    return resp.text


def post(url: str, data: dict, session: requests.Session, refresh: bool = False) -> str:
    """POST form data (WebForms postback), cached on url+data digest.

    VIEWSTATE fields are excluded from the cache key — they vary per fetch
    but don't change what the query means. (__EVENTTARGET does carry meaning,
    e.g. which pager page, so it stays in the key.)
    """
    _noise = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "__LASTFOCUS")
    key_data = {k: v for k, v in data.items() if k not in _noise}
    path = _cache_path(url, key_data)
    if path.exists() and not refresh:
        return path.read_text()
    resp = _request_with_backoff(
        lambda: session.post(url, data=data, headers={"User-Agent": USER_AGENT},
                             timeout=60, proxies=PROXIES))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resp.text)
    return resp.text


def webforms_state(html: str) -> dict:
    """Extract the hidden ASP.NET state fields needed to submit a postback."""
    soup = BeautifulSoup(html, "lxml")
    state = {}
    for field in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        el = soup.find("input", id=field)
        if el is not None:
            state[field] = el.get("value", "")
    return state


def form_defaults(html: str) -> dict:
    """All form fields with their current values, as a browser would submit them.

    Buttons are excluded — the caller adds the one being 'clicked' (or sets
    __EVENTTARGET). WebForms EVENTVALIDATION rejects submissions with missing
    or unexpected fields, so faithfully replaying the whole form matters.
    """
    soup = BeautifulSoup(html, "lxml")
    data: dict = {}
    for el in soup.find_all("input"):
        name = el.get("name")
        if not name or el.get("type") in ("submit", "button", "image"):
            continue
        if el.get("type") in ("checkbox", "radio") and not el.has_attr("checked"):
            continue
        data[name] = el.get("value", "")
    for el in soup.find_all("select"):
        name = el.get("name")
        if not name:
            continue
        opt = el.find("option", selected=True) or el.find("option")
        data[name] = opt.get("value", "") if opt else ""
    return data
