"""Serial, cache-first reader for the public EUCS Schedule pages."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://eucs-schedule.ultimatefederation.eu/"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "eucs"
PLACEHOLDER_RE = re.compile(
    r"^(?:winner|loser)\s+of\b|\b(?:winner|loser)\b.*\b(?:pool|game|quarter|semi|final)",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
GAME_ID_RE = re.compile(r"(?:[?&])game=([^&#]+)")
_LAST_FETCH = 0.0


class EUCSFetchError(RuntimeError):
    pass


class EUCSParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchedDocument:
    content: str
    source_url: str
    observed_at: str
    payload_hash: str
    from_cache: bool


@dataclass(frozen=True)
class SeasonRef:
    code: str
    name: str
    source_url: str


@dataclass(frozen=True)
class Schedule:
    season_code: str
    name: str
    divisions: list[str]
    games: list[dict[str, Any]]
    source_url: str
    observed_at: str
    payload_hash: str


def _cache_key(kind: str, code: str) -> str:
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}"


def _read_cache(cache_dir: Path, key: str) -> FetchedDocument | None:
    body = cache_dir / f"{key}.txt"
    meta = cache_dir / f"{key}.json"
    if not body.exists() or not meta.exists():
        return None
    info = json.loads(meta.read_text())
    raw = body.read_bytes()
    content = raw.decode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != info["payload_hash"]:
        raise EUCSFetchError(f"cache hash mismatch for {body}")
    return FetchedDocument(
        content=content,
        source_url=info["source_url"],
        observed_at=info["observed_at"],
        payload_hash=digest,
        from_cache=True,
    )


def _write_cache(cache_dir: Path, key: str, document: FetchedDocument) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.txt").write_bytes(document.content.encode("utf-8"))
    (cache_dir / f"{key}.json").write_text(json.dumps({
        "source_url": document.source_url,
        "observed_at": document.observed_at,
        "payload_hash": document.payload_hash,
    }, sort_keys=True, separators=(",", ":")))


def _fetch(
    url: str,
    session: requests.Session,
    cache_dir: Path,
    key: str,
    refresh: bool = False,
    timeout: float = 30.0,
    crawl_delay: float = 10.0,
) -> FetchedDocument:
    global _LAST_FETCH
    if not refresh:
        cached = _read_cache(cache_dir, key)
        if cached is not None:
            return cached
    wait = crawl_delay - (time.monotonic() - _LAST_FETCH)
    if wait > 0:
        time.sleep(wait)
    try:
        response = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise EUCSFetchError(f"failed to fetch {url}: {exc}") from exc
    finally:
        _LAST_FETCH = time.monotonic()
    if response.status_code != 200:
        raise EUCSFetchError(f"HTTP {response.status_code} from {response.url}")
    response.encoding = response.encoding or "utf-8"
    content = response.text
    document = FetchedDocument(
        content=content,
        source_url=response.url,
        observed_at=datetime.now(timezone.utc).isoformat(),
        payload_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        from_cache=False,
    )
    _write_cache(cache_dir, key, document)
    return document


def _schedule_url(season: str) -> str:
    return BASE_URL + "?" + urlencode({
        "view": "games",
        "season": season,
        "filter": "tournaments",
        "group": "all",
    })


def discover_seasons(
    session: requests.Session,
    cache_dir: Path = CACHE_DIR,
    refresh: bool = False,
    crawl_delay: float = 10.0,
) -> list[SeasonRef]:
    document = _fetch(
        BASE_URL,
        session,
        cache_dir,
        _cache_key("seasons", "index"),
        refresh=refresh,
        crawl_delay=crawl_delay,
    )
    soup = BeautifulSoup(document.content, "html.parser")
    select = soup.select_one("select[name=selseason]")
    if select is None:
        raise EUCSParseError("season selector not found")
    refs = []
    for option in select.find_all("option"):
        code = option.get("value")
        if code:
            refs.append(SeasonRef(str(code), option.get_text(" ", strip=True),
                                  _schedule_url(str(code))))
    if not refs:
        raise EUCSParseError("season selector contained no seasons")
    return refs


def fetch_schedule(
    season: str,
    session: requests.Session,
    cache_dir: Path = CACHE_DIR,
    refresh: bool = False,
    crawl_delay: float = 10.0,
) -> FetchedDocument:
    return _fetch(
        _schedule_url(season),
        session,
        cache_dir,
        _cache_key("schedule", season),
        refresh=refresh,
        crawl_delay=crawl_delay,
    )


def fetch_ical(
    season: str,
    session: requests.Session,
    cache_dir: Path = CACHE_DIR,
    refresh: bool = False,
    crawl_delay: float = 10.0,
) -> FetchedDocument:
    url = BASE_URL + "?" + urlencode({
        "view": "ical", "season": season, "time": "all", "order": "tournaments"
    })
    return _fetch(
        url,
        session,
        cache_dir,
        _cache_key("ical", season),
        refresh=refresh,
        crawl_delay=crawl_delay,
    )


def _score(cell: Any) -> int | None:
    # The first span is the game score. Later muted spans are spirit scores.
    span = cell.find("span")
    text = span.get_text(" ", strip=True) if span else cell.get_text(" ", strip=True)
    return int(text) if re.fullmatch(r"\d+", text) else None


def _division(stage: str) -> str:
    first = stage.split(None, 1)[0].rstrip(":") if stage else ""
    aliases = {"mixed": "Mixed", "open": "Open", "women": "Women",
               "women's": "Women", "womens": "Women"}
    return aliases.get(first.lower(), first or "Unknown")


def _date_before(table: Any) -> str | None:
    heading = table.find_previous("h3")
    if heading is None:
        return None
    match = DATE_RE.search(heading.get_text(" ", strip=True))
    if not match:
        return None
    day, month, year = (int(x) for x in match.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError as exc:
        raise EUCSParseError(f"invalid schedule date {match.group(0)}") from exc


def _game_id(row: Any, identity: str) -> tuple[str, str | None]:
    for link in row.find_all("a", href=True):
        match = GAME_ID_RE.search(link["href"])
        if match:
            return match.group(1), match.group(1)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"slot:{digest}", None


def parse_schedule(document: FetchedDocument, season: str) -> Schedule:
    soup = BeautifulSoup(document.content, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else season
    games: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for table in soup.find_all("table"):
        header = table.find("th")
        if header is None:
            continue
        stage = header.get_text(" ", strip=True)
        date = _date_before(table)
        for ordinal, row in enumerate(table.find_all("tr", recursive=False)):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 7:
                continue
            time_text = cells[0].get_text(" ", strip=True)
            field = cells[1].get_text(" ", strip=True)
            home_label = cells[2].get_text(" ", strip=True)
            away_label = cells[6].get_text(" ", strip=True)
            if not time_text or not home_label or not away_label:
                continue
            home_score, away_score = _score(cells[3]), _score(cells[5])
            home_placeholder = bool(PLACEHOLDER_RE.search(home_label))
            away_placeholder = bool(PLACEHOLDER_RE.search(away_label))
            row_text = row.get_text(" ", strip=True).lower()
            forfeit = any(word in row_text for word in ("forfeit", "walkover", "walk-over"))
            if forfeit:
                state = "forfeit"
            elif home_placeholder or away_placeholder:
                state = "placeholder"
            elif home_score is not None and away_score is not None:
                state = "played"
            else:
                state = "scheduled"
            identity = "|".join((season, date or "", time_text, field, stage,
                                  home_label, away_label, str(ordinal)))
            source_id, numeric_id = _game_id(row, identity)
            if source_id in seen_ids:
                # A numeric game can have a second navigation link, but not a
                # second fixture. Synthetic slot identities include ordinal.
                continue
            seen_ids.add(source_id)
            games.append({
                "season_code": str(season),
                "source_game_id": str(source_id),
                "numeric_game_id": numeric_id,
                "division": _division(stage),
                "stage": stage,
                "date": date,
                "time": time_text,
                "timezone": None,
                "field": field,
                "home": None if home_placeholder else home_label,
                "away": None if away_placeholder else away_label,
                "home_label": home_label,
                "away_label": away_label,
                "home_score": home_score,
                "away_score": away_score,
                "state": state,
            })
    if not games:
        raise EUCSParseError(f"no schedule games found for {season}")
    divisions = sorted({game["division"] for game in games})
    return Schedule(
        season_code=str(season),
        name=title.removeprefix("Schedule ").strip(),
        divisions=divisions,
        games=games,
        source_url=document.source_url,
        observed_at=document.observed_at,
        payload_hash=document.payload_hash,
    )

def parse_ical(document: FetchedDocument) -> list[dict[str, str | None]]:
    """Parse schedule facts only; iCalendar is never a score source."""
    unfolded: list[str] = []
    for line in document.content.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    events, current = [], None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        raw_key, value = line.split(":", 1)
        key = raw_key.split(";", 1)[0]
        if key in {"SUMMARY", "DESCRIPTION", "LOCATION", "DTSTART", "DTEND", "UID"}:
            current[key.lower()] = value.replace("\\,", ",").replace("\\n", "\n")
    return events


def cross_check_ical(schedule: Schedule, document: FetchedDocument) -> dict[str, int]:
    """Cross-check date/time/field/pairing coverage without importing scores."""
    def slot(date_value: str | None, time_value: str | None, field: str | None):
        number = re.search(r"\d+", field or "")
        return date_value or "", (time_value or "")[:5], number.group(0) if number else ""

    schedule_slots: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for game in schedule.games:
        schedule_slots.setdefault(
            slot(game["date"], game["time"], game["field"]), []
        ).append(game)
    matched_slots = matched_pairings = 0
    events = parse_ical(document)
    for event in events:
        start = event.get("dtstart") or ""
        match = re.search(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})", start)
        if not match:
            continue
        year, month, day, hour, minute = match.groups()
        key = slot(f"{year}-{month}-{day}", f"{hour}:{minute}", event.get("location"))
        candidates = schedule_slots.get(key, [])
        if not candidates:
            continue
        matched_slots += 1
        summary = (event.get("summary") or "").casefold()
        if any((game["home_label"].casefold() in summary and
                game["away_label"].casefold() in summary) for game in candidates):
            matched_pairings += 1
    return {
        "ical_events": len(events),
        "matched_slots": matched_slots,
        "matched_pairings": matched_pairings,
        "unmatched_slots": len(events) - matched_slots,
    }
