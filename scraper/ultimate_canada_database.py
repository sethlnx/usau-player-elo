"""Ingest Canadian Ultimate tournament data into the normalized EUF database.

The public Canadian Ultimate Database is linked from Ultimate Canada tournament
pages and exposes tournament metadata, schedules, teams, standings, and public
roster/stat rows as one JSON document.  It is kept as a distinct source with
its own provenance; rows are not presented as Ultimate Central data.

Examples:
    python -m scraper.ultimate_canada_database --list --db data/euf.db
    python -m scraper.ultimate_canada_database 2026-cuc-jr --db data/euf.db
    python -m scraper.ultimate_canada_database --all --year 2026 --db data/euf.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

from .euf import EUF_DB, init_db, observe, replace_event, upsert_event, validate_event

API_BASE_URL = "https://ulti-db-api-7k4iapmnxa-nn.a.run.app"
SOURCE = "ultimate-canada:database"
WEB_BASE_URL = "https://canadian-ultimate-database.web.app/tournament"


class UltimateCanadaError(RuntimeError):
    """Base class for Canadian tournament API failures."""


class UltimateCanadaClient:
    """Small injectable client for the public Canadian tournament API."""

    def __init__(
        self,
        session: requests.Session,
        base_url: str = API_BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str) -> tuple[dict[str, Any], str, str, str]:
        response = self.session.get(self.base_url + path, timeout=self.timeout)
        if response.status_code >= 400:
            raise UltimateCanadaError(
                f"HTTP {response.status_code} from {response.url}"
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise UltimateCanadaError(f"non-JSON response from {response.url}") from exc
        if not isinstance(payload, dict):
            raise UltimateCanadaError(f"response from {response.url} is not an object")
        observed_at = datetime.now(timezone.utc).isoformat()
        return (
            payload,
            response.url,
            observed_at,
            hashlib.sha256(response.content).hexdigest(),
        )

    def list_tournaments(self) -> tuple[list[dict[str, Any]], str, str, str]:
        payload, url, observed_at, payload_hash = self._get_json("/tournaments")
        rows = payload.get("tournaments")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise UltimateCanadaError("tournament listing has an invalid shape")
        return rows, url, observed_at, payload_hash

    def get_tournament(
        self, tournament_key: str
    ) -> tuple[dict[str, Any], str, str, str]:
        key = str(tournament_key).strip()
        if not key:
            raise ValueError("tournament key must not be empty")
        payload, url, observed_at, payload_hash = self._get_json(
            f"/tournaments/{key}/data"
        )
        if payload.get("ok") is not True:
            raise UltimateCanadaError(f"Canadian tournament {key} is unavailable")
        return payload, url, observed_at, payload_hash


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _division_key(value: Any) -> str:
    text = str(value or "").strip()
    return text or "unknown"

def _division_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return "canada-" + (slug or "unknown")

def _source_id(*parts: Any) -> str:
    return "|".join(str(part) for part in parts)


def _game_state(game: dict[str, Any]) -> str:
    score1 = _int_or_none(game.get("score1"))
    score2 = _int_or_none(game.get("score2"))
    if score1 is not None and score2 is not None:
        return "has_outcome"
    if score1 is not None or score2 is not None:
        return "incomplete"
    return "scheduled"


def _pool_rows(payload: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for pool in payload.get("pools") or []:
        if not isinstance(pool, dict):
            continue
        division = _division_key(pool.get("division"))
        yield division, pool


def _teams_by_division(payload: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    teams: dict[str, dict[str, dict[str, Any]]] = {}
    for team in payload.get("teams") or []:
        if not isinstance(team, dict) or team.get("team_id") in (None, ""):
            continue
        division = _division_key(team.get("division"))
        teams.setdefault(division, {})[str(team["team_id"])] = team
    for division, pool in _pool_rows(payload):
        bucket = teams.setdefault(division, {})
        for participant in pool.get("participants") or []:
            if not isinstance(participant, dict) or not participant.get("teamId"):
                continue
            sid = str(participant["teamId"])
            bucket.setdefault(sid, {
                "team_id": sid,
                "name": participant.get("label") or sid,
                "division": division,
            })
    return teams


def _standings(pool: dict[str, Any], division: str, slug: str,
               source_url: str, observed_at: str, payload_hash: str) -> list[dict[str, Any]]:
    rows = []
    for row in pool.get("results") or []:
        if not isinstance(row, dict) or not row.get("teamId"):
            continue
        place = _int_or_none(row.get("rank"))
        if place is None:
            continue
        team_sid = str(row["teamId"])
        pool_id = str(pool.get("poolId") or pool.get("name") or "pool")
        standing_division = f"{division}|{pool_id}"
        rows.append({
            "source_id": _source_id(slug, division, pool_id, place, team_sid),
            "team_source_id": team_sid,
            "division": standing_division,
            "place": place,
            "source_url": source_url,
            "observed_at": observed_at,
            "payload_hash": payload_hash,
        })
    return rows


def _normalize_division(
    payload: dict[str, Any],
    slug: str,
    division: str,
    source_url: str,
    observed_at: str,
    payload_hash: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    teams_by_division = _teams_by_division(payload)
    team_rows = teams_by_division.get(division, {})
    teams = []
    for sid, team in team_rows.items():
        teams.append({
            "source_id": sid,
            "name": str(team.get("name") or sid),
            "city": team.get("region"),
            "country": "CA",
            "source_url": source_url,
            "observed_at": observed_at,
            "payload_hash": payload_hash,
        })

    games: list[dict[str, Any]] = []
    standings: list[dict[str, Any]] = []
    for pool_division, pool in _pool_rows(payload):
        if pool_division != division:
            continue
        pool_id = str(pool.get("poolId") or pool.get("name") or "pool")
        standings.extend(_standings(pool, division, slug, source_url, observed_at,
                                    payload_hash))
        for game in pool.get("games") or []:
            if not isinstance(game, dict) or not game.get("gameId"):
                continue
            game_id = _source_id(slug, division, pool_id, game["gameId"])
            games.append({
                "source_id": game_id,
                "field": game.get("field"),
                "stage": pool.get("name"),
                "date": game.get("startDateISO"),
                "time": game.get("startTime"),
                "home_source_id": game.get("team1Id") or None,
                "away_source_id": game.get("team2Id") or None,
                "home_score": _int_or_none(game.get("score1")),
                "away_score": _int_or_none(game.get("score2")),
                "home_name": game.get("team1Display"),
                "away_name": game.get("team2Display"),
                "state": _game_state(game),
                "source_url": source_url,
                "observed_at": observed_at,
                "payload_hash": payload_hash,
            })

    roster_entries = []
    for person in payload.get("teamRosters") or []:
        if not isinstance(person, dict) or _division_key(person.get("division")) != division:
            continue
        if not person.get("team_id") or not person.get("player_id"):
            continue
        first = str(person.get("first_name") or "").strip()
        last = str(person.get("last_name") or "").strip()
        name = " ".join(part for part in (first, last) if part)
        if not name:
            continue
        roster_entries.append({
            "team_source_id": str(person["team_id"]),
            "source_id": str(person["player_id"]),
            "number": person.get("player_number"),
            "name": name,
            "points": person.get("goals"),
            "assists": person.get("assists"),
            "turns": person.get("turnovers"),
            "ds": person.get("blocks"),
            "source_url": source_url,
            "observed_at": observed_at,
            "payload_hash": payload_hash,
        })
    return teams, games, standings, roster_entries


def ingest_tournament(
    con: sqlite3.Connection,
    client: UltimateCanadaClient,
    tournament_key: str,
) -> list[dict[str, Any]]:
    payload, source_url, observed_at, payload_hash = client.get_tournament(tournament_key)
    key = str(tournament_key)
    tournament = payload.get("tournament")
    if not isinstance(tournament, dict):
        raise UltimateCanadaError(f"Canadian tournament {key} has no metadata")
    start = tournament.get("startDate")
    end = tournament.get("endDate")
    year_match = re.match(r"(\d{4})", str(start or ""))
    if not year_match:
        raise ValueError(f"Canadian tournament {key} has no source year")
    season = int(year_match.group(1))
    divisions = sorted(set(_division_key(value) for value in payload.get("divisions") or [])
                       | set(_division_key(division) for division, _ in _pool_rows(payload)))
    if not divisions:
        raise ValueError(f"Canadian tournament {key} has no divisions")
    roster_state = "public" if "teamRosters" in payload else "unavailable"
    event_url = f"{WEB_BASE_URL}/{key}"
    summaries = []
    with con:
        observe(con, SOURCE, "tournament", key, source_url=source_url,
                observed_at=observed_at, payload_hash=payload_hash,
                count=len(payload.get("pools") or []))
        for division in divisions:
            teams, games, standings, roster_entries = _normalize_division(
                payload, key, division, source_url, observed_at, payload_hash
            )
            event_id = upsert_event(
                con, season, str(tournament.get("name") or key), event_url,
                _division_slug(division), key, SOURCE,
                key, tournament.get("location"), None, start, end,
            )
            replace_event(
                con, event_id, SOURCE, f"{key}|{division}", f"{key}|{division}",
                teams, games, standings, roster_state, source_url, observed_at,
                payload_hash, roster_entries,
            )
            stats = validate_event(con, event_id)
            con.execute("UPDATE events SET complete=1 WHERE event_id=?", (event_id,))
            summaries.append({"event_id": event_id, "division": division,
                              **stats, "standings": len(standings),
                              "roster_entries": len(roster_entries)})
    return summaries


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tournament", nargs="?")
    parser.add_argument("--db", type=Path, default=EUF_DB)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--year", type=int)
    return parser


def main(argv: list[str] | None = None,
         session_factory: Callable[[], requests.Session] = requests.Session) -> int:
    args = _parser().parse_args(argv)
    if not args.list and not args.all and not args.tournament:
        raise SystemExit("choose a tournament key, --list, or --all")
    session = session_factory()
    client = UltimateCanadaClient(session)
    try:
        if args.list:
            rows, _, _, _ = client.list_tournaments()
            if args.year is not None:
                rows = [row for row in rows if str(row.get("start_date", "")).startswith(str(args.year))]
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        keys = [args.tournament] if args.tournament else []
        if args.all:
            rows, _, _, _ = client.list_tournaments()
            keys = [str(row["tournament_key"]) for row in rows
                    if row.get("tournament_key") and
                    (args.year is None or str(row.get("start_date", "")).startswith(str(args.year)))]
        con = init_db(args.db)
        try:
            summaries = []
            for key in keys:
                summaries.append({"tournament": key,
                                  "result": ingest_tournament(con, client, key)})
            print(json.dumps(summaries, indent=2, sort_keys=True))
        finally:
            con.close()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
