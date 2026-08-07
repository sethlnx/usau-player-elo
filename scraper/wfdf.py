"""Ingest completed WFDF world grass championships from official results pages.

The WFDF results service is server-rendered HTML, not a documented JSON API.
This adapter caches every public page, preserves source hashes and IDs, and
normalizes teams, games, standings, and event-specific rosters into data/euf.db.

Examples:
    python -m scraper.wfdf wucc-2022 wuc-2024
    python -m scraper.wfdf all
    python -m scraper.wfdf --audit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .euf import (
    EUF_DB,
    audit,
    init_db,
    observe,
    replace_event,
    upsert_event,
    validate_event,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "raw" / "wfdf"
WFDF_SOURCE = "wfdf-results"
BASE_URL = "https://results.wfdf.sport/"
USER_AGENT = "usau-player-elo/1.0 (+https://github.com/sethlnx/usau-player-elo)"


@dataclass(frozen=True)
class EventSpec:
    key: str
    slug: str
    season_id: str
    season: int
    name: str
    competition: str
    city: str
    state: str

    @property
    def url(self) -> str:
        return urljoin(BASE_URL, f"{self.slug}/")


EVENTS = {
    spec.key: spec
    for spec in (
        EventSpec("wucc-2022", "wucc", "WUCC2022", 2022,
                  "WFDF World Ultimate Club Championships 2022", "club",
                  "Lebanon", "Ohio, United States"),
        EventSpec("wmucc-2022", "wmucc-2022", "WMUCC2022", 2022,
                  "WFDF World Masters Ultimate Club Championships 2022", "club",
                  "Limerick", "Ireland"),
        EventSpec("wuc-2024", "wuc", "WUC2024", 2024,
                  "WFDF World Ultimate Championships 2024", "national",
                  "Gold Coast", "Queensland, Australia"),
        EventSpec("wmuc-2024", "wmuc", "WMUC2024", 2024,
                  "WFDF World Masters Ultimate Championships 2024", "national",
                  "Irvine", "California, United States"),
    )
}

DIVISIONS = {
    "Open": "club-men",
    "Women's": "club-women",
    "Mixed": "club-mixed",
    "Master Open": "masters-men",
    "Master Women's": "masters-women",
    "Master Mixed": "masters-mixed",
    "Grand Master Open": "grandmasters-men",
    "Grand Master Women's": "grandmasters-women",
    "Grand Master Mixed": "grandmasters-mixed",
    "Great Grand Master Open": "greatgrandmasters-men",
    "Great Grand Master Women's": "greatgrandmasters-women",
    "Great Grand Master Mixed": "greatgrandmasters-mixed",
}
IGNORED_DIVISIONS = {"Guts Open", "Guts Women's"}
TEAM_ALIASES = {
    "wmucc-2022": {
        "GOLD (Gentle-OLD)": "Goldfingers Ultimate Club",
    },
}


@dataclass(frozen=True)
class FetchedPage:
    text: str
    url: str
    observed_at: str
    payload_hash: str
    from_cache: bool


_thread_local = threading.local()


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        _thread_local.session = session
    return session


def _cache_path(spec: EventSpec, resource: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", resource)
    return CACHE_DIR / spec.key / f"{safe}.html"


def fetch_page(
    spec: EventSpec,
    resource: str,
    params: dict[str, str],
    refresh: bool = False,
) -> FetchedPage:
    path = _cache_path(spec, resource)
    url = spec.url
    if path.exists() and not refresh:
        raw = path.read_bytes()
        observed = datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat()
        return FetchedPage(
            raw.decode("utf-8", errors="replace"),
            requests.Request("GET", url, params=params).prepare().url,
            observed,
            hashlib.sha256(raw).hexdigest(),
            True,
        )
    response = _session().get(url, params=params, timeout=45)
    response.raise_for_status()
    raw = response.content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    observed = datetime.now(timezone.utc).isoformat()
    return FetchedPage(
        raw.decode(response.encoding or "utf-8", errors="replace"),
        response.url,
        observed,
        hashlib.sha256(raw).hexdigest(),
        False,
    )


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _query_id(href: str | None, key: str) -> str | None:
    if not href:
        return None
    values = parse_qs(urlparse(href.replace("&amp;", "&")).query).get(key)
    return values[0] if values else None


def _place(value: str) -> int | None:
    text = _clean(value)
    podium = {"Gold": 1, "Silver": 2, "Bronze": 3}
    if text in podium:
        return podium[text]
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _country(anchor: Any) -> str | None:
    cell = anchor.find_parent("td")
    if cell is None:
        return None
    image = anchor.find_previous("img")
    if image is None or image.find_parent("td") != cell:
        return None
    filename = Path(urlparse(image.get("src", "")).path).stem
    return unquote(filename).replace("_", " ") or None


def parse_team_index(page: FetchedPage) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page.text, "html.parser")
    table = next(
        (
            table for table in soup.find_all("table")
            if table.find("th") is not None
            and _clean(table.find("th").get_text(" ", strip=True)) == "Placement"
        ),
        None,
    )
    if table is None:
        raise ValueError(f"no standings table in {page.url}")
    headers = [_clean(th.get_text(" ", strip=True)) for th in table.find_all("th")]
    published = headers[1:]
    unknown = [
        name for name in published
        if name not in DIVISIONS and name not in IGNORED_DIVISIONS
    ]
    if unknown:
        raise ValueError(f"unknown WFDF divisions in {page.url}: {unknown}")
    teams: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        place = _place(cells[0].get_text(" ", strip=True))
        for index, division_name in enumerate(published, start=1):
            if division_name in IGNORED_DIVISIONS:
                continue
            if index >= len(cells):
                continue
            for anchor in cells[index].find_all("a", href=True):
                team_id = _query_id(anchor.get("href"), "team")
                if team_id is None:
                    continue
                if team_id in seen:
                    raise ValueError(f"team {team_id} appears twice in {page.url}")
                seen.add(team_id)
                teams.append({
                    "team_id": team_id,
                    "name": _clean(anchor.get_text(" ", strip=True)),
                    "country": _country(anchor),
                    "division_name": division_name,
                    "division": DIVISIONS[division_name],
                    "place": place,
                })
    if not teams:
        raise ValueError(f"no teams in {page.url}")
    return teams

def parse_all_teams(page: FetchedPage) -> list[dict[str, Any]]:
    """Parse the complete team list, including unplaced/withdrawn teams."""
    soup = BeautifulSoup(page.text, "html.parser")
    content = soup.select_one("td.tdcontent") or soup
    teams: list[dict[str, Any]] = []
    seen: set[str] = set()
    division_name: str | None = None
    for row in content.find_all("tr"):
        heading = row.find("th")
        if heading is not None:
            candidate = _clean(heading.get_text(" ", strip=True))
            if candidate in DIVISIONS or candidate in IGNORED_DIVISIONS:
                division_name = candidate
            continue
        if division_name is None or division_name in IGNORED_DIVISIONS:
            continue
        anchor = row.find("a", href=re.compile(r"[?&]team="))
        team_id = _query_id(anchor.get("href"), "team") if anchor else None
        if team_id is None:
            continue
        if team_id in seen:
            raise ValueError(f"team {team_id} appears twice in {page.url}")
        seen.add(team_id)
        cells = row.find_all("td", recursive=False)
        country = None
        if len(cells) >= 3:
            country_link = cells[2].find("a", href=re.compile(r"[?&]country="))
            if country_link is not None:
                country = _clean(country_link.get_text(" ", strip=True))
        teams.append({
            "team_id": team_id,
            "name": _clean(anchor.get_text(" ", strip=True)),
            "country": country,
            "division_name": division_name,
            "division": DIVISIONS[division_name],
            "place": None,
        })
    if not teams:
        raise ValueError(f"no complete team list in {page.url}")
    return teams


def merge_team_lists(
    all_teams: list[dict[str, Any]],
    standings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from analysis.euf_ratings import team_name_key
    by_id = {team["team_id"]: team for team in all_teams}
    for placed in standings:
        team = by_id.get(placed["team_id"])
        if team is None:
            raise ValueError(f"placed team {placed['team_id']} missing from full team list")
        if (team["division"], team_name_key(team["name"])) != (
            placed["division"], team_name_key(placed["name"])
        ):
            raise ValueError(f"team-list disagreement for {placed['team_id']}")
        team["place"] = placed["place"]
        team["country"] = team["country"] or placed["country"]
    return all_teams


def _date(text: str) -> str | None:
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if not match:
        return None
    day, month, year = map(int, match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _score(text: str) -> int | None:
    match = re.match(r"\d+", _clean(text))
    return int(match.group()) if match else None


def _stage_division(stage: str) -> tuple[str, str] | None:
    for published in sorted(DIVISIONS, key=len, reverse=True):
        if stage == published or stage.startswith(published + " "):
            return published, DIVISIONS[published]
    if any(stage == name or stage.startswith(name + " ")
           for name in IGNORED_DIVISIONS):
        return None
    raise ValueError(f"unknown WFDF game division: {stage!r}")


def _team_lookup(teams: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    from analysis.euf_ratings import team_name_key

    lookup: dict[tuple[str, str], list[str]] = {}
    for team in teams:
        lookup.setdefault(
            (team["division"], team_name_key(team["name"])), []
        ).append(team["team_id"])
    return lookup


def parse_games(
    page: FetchedPage,
    teams: list[dict[str, Any]],
    aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    from analysis.euf_ratings import team_name_key

    soup = BeautifulSoup(page.text, "html.parser")
    aliases = aliases or {}
    content = soup.select_one("td.tdcontent") or soup
    lookup = _team_lookup(teams)
    current_date: str | None = None
    games: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in content.find_all(["h3", "table"]):
        if element.name == "h3":
            current_date = _date(element.get_text(" ", strip=True)) or current_date
            continue
        links = element.find_all("a", href=re.compile(r"[?&]game="))
        if not links:
            continue
        heading = element.find("th")
        if heading is None:
            continue
        stage = _clean(heading.get_text(" ", strip=True))
        resolved_division = _stage_division(stage)
        if resolved_division is None:
            continue
        published_division, division = resolved_division
        for row in element.find_all("tr"):
            link = row.find("a", href=re.compile(r"[?&]game="))
            if link is None:
                continue
            game_id = _query_id(link.get("href"), "game")
            cells = row.find_all("td", recursive=False)
            if game_id is None or len(cells) < 7:
                continue
            if game_id in seen:
                raise ValueError(f"game {game_id} appears twice in {page.url}")
            seen.add(game_id)
            home_name = _clean(cells[2].get_text(" ", strip=True))
            away_name = _clean(cells[6].get_text(" ", strip=True))

            def resolve(name: str) -> str | None:
                key = team_name_key(aliases.get(name, name))
                candidates = lookup.get((division, key), [])
                if len(candidates) > 1:
                    raise ValueError(
                        f"ambiguous {published_division} team {name!r} in {page.url}"
                    )
                return candidates[0] if candidates else None

            home_id, away_id = resolve(home_name), resolve(away_name)
            home_score, away_score = _score(cells[3].get_text()), _score(cells[5].get_text())
            status_text = _clean(" ".join(
                cell.get_text(" ", strip=True) for cell in cells[7:-1]
            )).casefold()
            if home_score is not None and away_score is not None:
                state = "played"
            elif any(word in status_text for word in ("forfeit", "walkover", "game not")):
                state = "forfeit"
            else:
                state = "scheduled"
            if state == "played" and (home_id is None or away_id is None):
                raise ValueError(
                    f"scored game {game_id} has unresolved teams "
                    f"{home_name!r} / {away_name!r} in {page.url}"
                )
            games.append({
                "game_id": game_id,
                "division_name": published_division,
                "division": division,
                "stage": stage,
                "date": current_date,
                "time": _clean(cells[0].get_text(" ", strip=True)) or None,
                "field": _clean(cells[1].get_text(" ", strip=True)) or None,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_name": home_name,
                "away_name": away_name,
                "home_score": home_score,
                "away_score": away_score,
                "state": state,
            })
    if not games:
        raise ValueError(f"no games in {page.url}")
    if any(game["state"] == "played" and game["date"] is None for game in games):
        raise ValueError(f"scored game without a date in {page.url}")
    return games


def parse_team_card(
    page: FetchedPage,
    team: dict[str, Any],
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page.text, "html.parser")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for anchor in soup.find_all("a", href=re.compile(r"[?&]player=")):
        player_id = _query_id(anchor.get("href"), "player")
        row = anchor.find_parent("tr")
        if player_id is None or row is None:
            continue
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        name = _clean(anchor.get_text(" ", strip=True))
        number = _clean(cells[0].get_text(" ", strip=True)) or None
        key = (player_id, number)
        if not name or key in seen:
            continue
        seen.add(key)
        headers = [
            _clean(cell.get_text(" ", strip=True)).casefold()
            for cell in row.find_parent("table").find_all("th")
        ]
        values = [_clean(cell.get_text(" ", strip=True)) for cell in cells]
        by_header = {
            header: values[index]
            for index, header in enumerate(headers)
            if index < len(values)
        }
        entries.append({
            "player_id": player_id,
            "team_id": team["team_id"],
            "name": name,
            "number": number,
            "games": by_header.get("games"),
            "assists": by_header.get("assists"),
            "points": by_header.get("goals"),
        })
    return entries

def parse_player_index(page: FetchedPage) -> list[str]:
    soup = BeautifulSoup(page.text, "html.parser")
    content = soup.select_one("td.tdcontent") or soup
    player_ids = {
        player_id
        for anchor in content.find_all("a", href=re.compile(r"[?&]player="))
        if (player_id := _query_id(anchor.get("href"), "player")) is not None
    }
    if not player_ids:
        raise ValueError(f"no players in {page.url}")
    return sorted(player_ids, key=lambda value: int(value))


def parse_player_card(page: FetchedPage, player_id: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(page.text, "html.parser")
    content = soup.select_one("td.tdcontent") or soup
    team_anchor = content.find("a", href=re.compile(r"[?&]team="))
    team_id = _query_id(team_anchor.get("href"), "team") if team_anchor else None
    heading = content.find("h1")
    if team_id is None or heading is None:
        return None
    title = _clean(heading.get_text(" ", strip=True))
    numbered = re.match(r"^#(\S+)\s+(.+)$", title)
    number = numbered.group(1) if numbered else None
    name = numbered.group(2) if numbered else title
    if not name:
        return None
    stats: dict[str, str] = {}
    for table in content.find_all("table"):
        headers = [
            _clean(cell.get_text(" ", strip=True)).casefold()
            for cell in table.find_all("th")
        ]
        if not {"games", "assists", "goals"}.issubset(headers):
            continue
        row = table.find("tr")
        values_row = row.find_next_sibling("tr") if row is not None else None
        values = (
            [_clean(cell.get_text(" ", strip=True))
             for cell in values_row.find_all("td")]
            if values_row is not None else []
        )
        stats = {
            header: values[index]
            for index, header in enumerate(headers)
            if index < len(values)
        }
        break
    return {
        "player_id": player_id,
        "team_id": team_id,
        "name": name,
        "number": number,
        "games": stats.get("games"),
        "assists": stats.get("assists"),
        "points": stats.get("goals"),
    }


def _prefix(spec: EventSpec, value: str | None) -> str | None:
    return f"{spec.key}:{value}" if value is not None else None


def _combined_hash(pages: Iterable[FetchedPage]) -> str:
    digest = hashlib.sha256()
    for page in sorted(pages, key=lambda item: item.url):
        digest.update(page.url.encode())
        digest.update(b"\0")
        digest.update(page.payload_hash.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def ingest_world_event(
    con,
    spec: EventSpec,
    refresh: bool = False,
    workers: int = 4,
) -> list[dict[str, Any]]:
    standings_page = fetch_page(
        spec, "standings",
        {"view": "teams", "season": spec.season_id, "list": "bystandings"},
        refresh,
    )
    standings = parse_team_index(standings_page)
    teams_page = fetch_page(
        spec, "teams",
        {"view": "teams", "season": spec.season_id, "list": "allteams"},
        refresh,
    )
    teams = merge_team_lists(parse_all_teams(teams_page), standings)
    games_page = fetch_page(
        spec, "games",
        {"view": "games", "season": spec.season_id,
         "filter": "tournaments", "group": "all"},
        refresh,
    )
    games = parse_games(games_page, teams, TEAM_ALIASES.get(spec.key))

    roster_pages: dict[str, FetchedPage] = {}
    rosters: dict[str, list[dict[str, Any]]] = {}

    def fetch_roster(team: dict[str, Any]):
        try:
            page = fetch_page(
                spec, f"team-{team['team_id']}",
                {"view": "teamcard", "team": team["team_id"]}, refresh,
            )
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status not in (404, 500):
                raise
            url = requests.Request(
                "GET", spec.url,
                params={"view": "teamcard", "team": team["team_id"]},
            ).prepare().url
            raw = f"<!-- roster unavailable: HTTP {status} -->".encode()
            path = _cache_path(spec, f"team-{team['team_id']}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            page = FetchedPage(
                raw.decode(), url, datetime.now(timezone.utc).isoformat(),
                hashlib.sha256(raw).hexdigest(), False,
            )
        return team["team_id"], page, parse_team_card(page, team)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_roster, team) for team in teams]
        for future in as_completed(futures):
            team_id, page, entries = future.result()
            roster_pages[team_id] = page
            rosters[team_id] = entries
    player_index_page: FetchedPage | None = None
    player_pages: dict[str, FetchedPage] = {}
    if sum(len(entries) for entries in rosters.values()) < len(teams) * 5:
        player_index_page = fetch_page(
            spec, "players",
            {"view": "allplayers", "list": "all"}, refresh,
        )
        player_ids = parse_player_index(player_index_page)
        known_teams = {team["team_id"] for team in teams}

        def fetch_player(player_id: str):
            try:
                page = fetch_page(
                    spec, f"player-{player_id}",
                    {"view": "playercard", "series": "0", "player": player_id},
                    refresh,
                )
            except requests.HTTPError as error:
                status = (
                    error.response.status_code
                    if error.response is not None else None
                )
                if status not in (404, 500):
                    raise
                url = requests.Request(
                    "GET", spec.url,
                    params={"view": "playercard", "series": "0",
                            "player": player_id},
                ).prepare().url
                raw = f"<!-- player unavailable: HTTP {status} -->".encode()
                path = _cache_path(spec, f"player-{player_id}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                page = FetchedPage(
                    raw.decode(), url, datetime.now(timezone.utc).isoformat(),
                    hashlib.sha256(raw).hexdigest(), False,
                )
            entry = parse_player_card(page, player_id)
            if entry is not None and entry["team_id"] not in known_teams:
                entry = None
            return player_id, page, entry

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(fetch_player, player_id)
                       for player_id in player_ids]
            for future in as_completed(futures):
                player_id, page, entry = future.result()
                player_pages[player_id] = page
                if entry is not None:
                    entry["_page"] = page
                    existing = {
                        row["player_id"]
                        for row in rosters[entry["team_id"]]
                    }
                    if entry["player_id"] not in existing:
                        rosters[entry["team_id"]].append(entry)

    dates = sorted(game["date"] for game in games if game["date"])
    if not dates:
        raise ValueError(f"{spec.key} has no dated games")
    source_pages = [
        standings_page, teams_page, games_page,
        *roster_pages.values(), *player_pages.values(),
    ]
    if player_index_page is not None:
        source_pages.append(player_index_page)
    event_hash = _combined_hash(source_pages)
    observed_at = max(page.observed_at for page in source_pages)
    summaries: list[dict[str, Any]] = []
    with con:
        observe(
            con, WFDF_SOURCE, "team-index", spec.key, count=len(teams),
            source_url=teams_page.url, observed_at=teams_page.observed_at,
            payload_hash=teams_page.payload_hash, state="ok",
        )
        observe(
            con, WFDF_SOURCE, "standings", spec.key, count=len(standings),
            source_url=standings_page.url, observed_at=standings_page.observed_at,
            payload_hash=standings_page.payload_hash, state="ok",
        )
        observe(
            con, WFDF_SOURCE, "games", spec.key, count=len(games),
            source_url=games_page.url, observed_at=games_page.observed_at,
            payload_hash=games_page.payload_hash, state="ok",
        )
        for team in teams:
            page = roster_pages[team["team_id"]]
            recovered = bool(rosters[team["team_id"]]) and not parse_team_card(
                page, team
            )
            observation_page = (
                player_index_page
                if recovered and player_index_page is not None
                else page
            )
            observe(
                con, WFDF_SOURCE, "roster", _prefix(spec, team["team_id"]),
                count=len(rosters[team["team_id"]]),
                source_url=observation_page.url,
                observed_at=observation_page.observed_at,
                payload_hash=event_hash if recovered else page.payload_hash,
                state="ok" if rosters[team["team_id"]] else "empty",
            )

        for division_name in dict.fromkeys(team["division_name"] for team in teams):
            division = DIVISIONS[division_name]
            selected_teams = [team for team in teams if team["division"] == division]
            selected_games = [game for game in games if game["division"] == division]
            selected_rosters = [
                entry
                for team in selected_teams
                for entry in rosters[team["team_id"]]
            ]
            rostered = {entry["team_id"] for entry in selected_rosters}
            if rostered == {team["team_id"] for team in selected_teams}:
                roster_state = "public"
            elif rostered:
                roster_state = "partial"
            else:
                roster_state = "unavailable"
            event_id = upsert_event(
                con, spec.season, spec.name, spec.url, division,
                f"{spec.key}:{division}", WFDF_SOURCE, spec.season_id,
                spec.city, spec.state, dates[0], dates[-1],
            )
            team_rows = [{
                "source_id": _prefix(spec, team["team_id"]),
                "name": team["name"],
                "country": team["country"],
                "source_url": teams_page.url,
                "observed_at": teams_page.observed_at,
                "payload_hash": teams_page.payload_hash,
            } for team in selected_teams]
            game_rows = [{
                "source_id": _prefix(spec, game["game_id"]),
                "home_source_id": _prefix(spec, game["home_team_id"]),
                "away_source_id": _prefix(spec, game["away_team_id"]),
                "home_name": game["home_name"],
                "away_name": game["away_name"],
                "home_score": game["home_score"],
                "away_score": game["away_score"],
                "state": game["state"],
                "date": game["date"],
                "time": game["time"],
                "field": game["field"],
                "stage": game["stage"],
                "source_url": games_page.url,
                "observed_at": games_page.observed_at,
                "payload_hash": games_page.payload_hash,
            } for game in selected_games]
            standing_rows = [{
                "source_id": f"{spec.key}:{division}:{team['place']}:{team['team_id']}",
                "team_source_id": _prefix(spec, team["team_id"]),
                "division": division,
                "place": team["place"],
                "source_url": standings_page.url,
                "observed_at": standings_page.observed_at,
                "payload_hash": standings_page.payload_hash,
            } for team in selected_teams if team["place"] is not None]
            person_rows = []
            for entry in selected_rosters:
                page = entry.get("_page") or roster_pages[entry["team_id"]]
                person_rows.append({
                    "source_id": _prefix(spec, entry["player_id"]),
                    "team_source_id": _prefix(spec, entry["team_id"]),
                    "name": entry["name"],
                    "number": entry["number"],
                    "points": entry["points"],
                    "assists": entry["assists"],
                    "source_url": page.url,
                    "observed_at": page.observed_at,
                    "payload_hash": page.payload_hash,
                })
            replace_event(
                con, event_id, WFDF_SOURCE, f"{spec.key}:{division}",
                f"{spec.key}:{division_name}", team_rows, game_rows,
                standing_rows, roster_state, spec.url, observed_at, event_hash,
                person_rows,
            )
            stats = validate_event(con, event_id)
            con.execute("UPDATE events SET complete=1 WHERE event_id=?", (event_id,))
            summaries.append({
                "event": spec.key,
                "event_id": event_id,
                "division": division,
                "roster_state": roster_state,
                "roster_entries": len(person_rows),
                **stats,
            })
    return summaries


def _select_events(values: list[str]) -> list[EventSpec]:
    if not values or values == ["all"]:
        return list(EVENTS.values())
    unknown = sorted(set(values) - set(EVENTS))
    if unknown:
        raise ValueError(f"unknown events: {', '.join(unknown)}")
    return [EVENTS[value] for value in values]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", nargs="*", metavar="EVENT")
    parser.add_argument("--refresh", action="store_true", help="ignore cached HTML")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--db", type=Path, default=EUF_DB)
    args = parser.parse_args()
    con = init_db(args.db)
    try:
        if args.audit and not args.events:
            report, blocked = audit(con)
            print(json.dumps(report, indent=2, sort_keys=True))
            raise SystemExit(1 if blocked else 0)
        for spec in _select_events(args.events):
            for summary in ingest_world_event(
                con, spec, refresh=args.refresh, workers=args.workers
            ):
                print(json.dumps(summary, sort_keys=True))
        report, blocked = audit(con)
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1 if blocked else 0)
    finally:
        con.close()


if __name__ == "__main__":
    main()
