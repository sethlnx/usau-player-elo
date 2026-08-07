"""Ingest European Ultimate sources into a provenance-preserving SQLite DB.

Examples:
    python -m scraper.euf --init-db
    python -m scraper.euf --probe ultimate-central
    python -m scraper.euf --probe eucs --season eucf24
    python -m scraper.euf 2024 --event eucf24
    python -m scraper.euf --audit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from .build_db import SCHEMA, _ensure_columns, connect
from .eucs_schedule import (
    CACHE_DIR,
    Schedule,
    SeasonRef,
    cross_check_ical,
    discover_seasons,
    fetch_ical,
    fetch_schedule,
    parse_schedule,
)
from .ultimate_central import (
    APIResponse,
    CollectionResult,
    UltimateCentralClient,
)

ROOT = Path(__file__).resolve().parent.parent
EUF_DB = Path(os.environ.get("EUF_DB", ROOT / "data" / "euf.db"))
UC_SOURCE = "ultimate-central:euf"
EUCS_SOURCE = "eucs-schedule"

EUF_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_people (
    person_id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS euf_event_details (
    event_id INTEGER PRIMARY KEY REFERENCES events(event_id),
    event_code TEXT NOT NULL,
    source_owner TEXT NOT NULL,
    provider_event_id TEXT,
    roster_state TEXT NOT NULL DEFAULT 'unavailable',
    UNIQUE (source_owner, event_code, event_id)
);
CREATE INDEX IF NOT EXISTS euf_event_code ON euf_event_details(event_code);
CREATE TABLE IF NOT EXISTS standings (
    event_id INTEGER NOT NULL REFERENCES events(event_id),
    division TEXT NOT NULL,
    place INTEGER NOT NULL,
    event_team_id TEXT NOT NULL REFERENCES event_teams(event_team_id),
    source TEXT NOT NULL,
    PRIMARY KEY (event_id, division, place, event_team_id)
);
CREATE TABLE IF NOT EXISTS roster_availability (
    event_id INTEGER NOT NULL REFERENCES events(event_id),
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (event_id, source)
);
CREATE TABLE IF NOT EXISTS ranking_roster_observations (
    source TEXT NOT NULL,
    roster_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    division TEXT NOT NULL,
    team_id TEXT NOT NULL REFERENCES canonical_teams(team_id),
    team_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    PRIMARY KEY (source, roster_id)
);
CREATE INDEX IF NOT EXISTS ranking_roster_team
ON ranking_roster_observations(team_id, season);
CREATE TABLE IF NOT EXISTS ranking_roster_entries (
    source TEXT NOT NULL,
    roster_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    PRIMARY KEY (source, roster_id, ordinal),
    FOREIGN KEY (source, roster_id)
      REFERENCES ranking_roster_observations(source, roster_id)
      ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ranking_roster_name
ON ranking_roster_entries(name_key);
CREATE TABLE IF NOT EXISTS source_observations (
    source TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    PRIMARY KEY (source, resource_type, source_id, source_url, payload_hash)
);
CREATE TABLE IF NOT EXISTS source_mapping_candidates (
    source_a TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    source_id_a TEXT NOT NULL,
    source_b TEXT NOT NULL,
    source_id_b TEXT NOT NULL,
    reason TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_a, entity_type, source_id_a, source_b, source_id_b)
);
CREATE TABLE IF NOT EXISTS audit_findings (
    finding_id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(event_id),
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail TEXT NOT NULL,
    source_a TEXT,
    source_b TEXT,
    blocking INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, code, detail, source_a, source_b)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_standings_key(con: sqlite3.Connection) -> None:
    key = [row[1] for row in con.execute("PRAGMA table_info(standings)")
           if row[5]]
    if key != ["event_id", "division", "place"]:
        return
    con.execute("ALTER TABLE standings RENAME TO standings_old")
    con.execute(
        """CREATE TABLE standings (
             event_id INTEGER NOT NULL REFERENCES events(event_id),
             division TEXT NOT NULL,
             place INTEGER NOT NULL,
             event_team_id TEXT NOT NULL REFERENCES event_teams(event_team_id),
             source TEXT NOT NULL,
             PRIMARY KEY (event_id,division,place,event_team_id)
           )"""
    )
    con.execute(
        """INSERT INTO standings
           SELECT event_id,division,place,event_team_id,source FROM standings_old"""
    )
    con.execute("DROP TABLE standings_old")


def _migrate_euf_divisions(con: sqlite3.Connection) -> None:
    con.execute("UPDATE events SET division='euf-open' WHERE division='euf-men'")


def init_db(path: Path = EUF_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _ensure_columns(con)
    con.executescript(EUF_SCHEMA)
    _migrate_standings_key(con)
    _migrate_euf_divisions(con)
    con.commit()
    return con


def canonical(value: Any) -> str:
    if value is None:
        raise ValueError("local and source keys cannot be null")
    return str(value)


def stable_key(namespace: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}\0{source_id}".encode()).hexdigest()[:24]
    return f"{namespace}:{digest}"


def link_sources(
    con: sqlite3.Connection,
    source: str,
    entity_type: str,
    source_id: Any,
    local_key: Any,
    source_url: str,
    observed_at: str,
    payload_hash: str,
) -> None:
    source_id, local_key = canonical(source_id), canonical(local_key)
    prior = con.execute(
        """SELECT local_key FROM source_entities
           WHERE source=? AND entity_type=? AND source_id=?""",
        (source, entity_type, source_id),
    ).fetchone()
    if prior is not None and prior[0] != local_key:
        raise ValueError(
            f"{source} {entity_type} {source_id} already maps to {prior[0]}, "
            f"not {local_key}"
        )
    con.execute(
        """INSERT INTO source_entities
           (source, entity_type, source_id, local_key, source_url, observed_at,
            payload_hash) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(source, entity_type, source_id) DO UPDATE SET
             source_url=excluded.source_url,
             observed_at=excluded.observed_at,
             payload_hash=excluded.payload_hash""",
        (source, entity_type, source_id, local_key, source_url, observed_at,
         payload_hash),
    )


def observe(
    con: sqlite3.Connection,
    source: str,
    resource_type: str,
    source_id: str,
    response: APIResponse | None = None,
    *,
    source_url: str | None = None,
    observed_at: str | None = None,
    payload_hash: str | None = None,
    state: str = "ok",
    count: int = 0,
) -> None:
    if response is not None:
        source_url = response.source_url
        observed_at = response.observed_at
        payload_hash = response.payload_hash
        state = response.state
        count = response.count
    assert source_url is not None and observed_at is not None and payload_hash is not None
    con.execute(
        """INSERT INTO source_observations
           (source, resource_type, source_id, source_url, observed_at,
            payload_hash, state, record_count) VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(source, resource_type, source_id, source_url, payload_hash)
           DO UPDATE SET observed_at=excluded.observed_at,
                         state=excluded.state,
                         record_count=excluded.record_count""",
        (source, resource_type, canonical(source_id), source_url, observed_at,
         payload_hash, state, count),
    )


def upsert_event(
    con: sqlite3.Connection,
    season: int,
    name: str,
    url: str,
    division: str,
    event_code: str,
    source_owner: str,
    provider_event_id: str | None = None,
    city: str | None = None,
    state: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    con.execute(
        """INSERT INTO events
           (season, name, url, city, state, start_date, end_date, division,
            has_schedule, complete)
           VALUES (?,?,?,?,?,?,?,?,1,0)
           ON CONFLICT(url, division) DO UPDATE SET
             season=excluded.season, name=excluded.name, city=excluded.city,
             state=excluded.state, start_date=excluded.start_date,
             end_date=excluded.end_date, has_schedule=1""",
        (season, name, url, city, state, start_date, end_date, division),
    )
    event_id = con.execute(
        "SELECT event_id FROM events WHERE url=? AND division=?", (url, division)
    ).fetchone()[0]
    con.execute(
        """INSERT INTO euf_event_details
           (event_id, event_code, source_owner, provider_event_id)
           VALUES (?,?,?,?)
           ON CONFLICT(event_id) DO UPDATE SET
             event_code=excluded.event_code,
             source_owner=excluded.source_owner,
             provider_event_id=excluded.provider_event_id""",
        (event_id, canonical(event_code), source_owner,
         canonical(provider_event_id) if provider_event_id is not None else None),
    )
    return event_id


def _clear_event(con: sqlite3.Connection, event_id: int) -> None:
    team_ids = [row[0] for row in con.execute(
        "SELECT event_team_id FROM event_teams WHERE event_id=?", (event_id,)
    )]
    game_keys = [f"{event_id}|{row[0]}" for row in con.execute(
        "SELECT game_key FROM games WHERE event_id=?", (event_id,)
    )]
    con.execute(
        """DELETE FROM source_entities
           WHERE entity_type='standing' AND local_key LIKE ?""",
        (f"{event_id}|%",),
    )
    local_keys = [str(event_id), *game_keys]
    if local_keys:
        con.executemany(
            "DELETE FROM source_entities WHERE local_key=? AND entity_type!='team'",
            [(key,) for key in local_keys],
        )
    if team_ids:
        con.executemany(
            "DELETE FROM roster_entries WHERE event_team_id=?",
            [(team_id,) for team_id in team_ids],
        )
    con.execute("DELETE FROM standings WHERE event_id=?", (event_id,))
    con.execute("DELETE FROM games WHERE event_id=?", (event_id,))
    con.execute("DELETE FROM event_teams WHERE event_id=?", (event_id,))
    con.execute("DELETE FROM roster_availability WHERE event_id=?", (event_id,))
    con.execute("DELETE FROM audit_findings WHERE event_id=?", (event_id,))


def _upsert_canonical_team(
    con: sqlite3.Connection,
    source: str,
    source_team_id: str,
    name: str,
    source_url: str,
    observed_at: str,
    payload_hash: str,
    country: str | None = None,
) -> str:
    team_id = stable_key(source, source_team_id)
    con.execute(
        """INSERT INTO canonical_teams(team_id,name,country,created_at)
           VALUES (?,?,?,?)
           ON CONFLICT(team_id) DO UPDATE SET
             name=excluded.name, country=COALESCE(excluded.country,country)""",
        (team_id, name, country, observed_at),
    )
    link_sources(con, source, "team", source_team_id, team_id,
                 source_url, observed_at, payload_hash)
    return team_id


def _score_conflicts(
    con: sqlite3.Connection,
    event_id: int,
    source: str,
    games: list[dict[str, Any]],
) -> list[tuple[str, str, str, str, str]]:
    old = con.execute(
        """SELECT sx.source,sx.source_id,g.date,g.time,
                  lower(h.display_name),lower(a.display_name),
                  g.home_score,g.away_score
           FROM games g
           JOIN source_entities sx ON sx.entity_type='game'
             AND sx.local_key=CAST(g.event_id AS TEXT)||'|'||g.game_key
           LEFT JOIN event_teams h ON h.event_team_id=g.home_id
           LEFT JOIN event_teams a ON a.event_team_id=g.away_id
           WHERE g.event_id=? AND g.home_score IS NOT NULL
             AND g.away_score IS NOT NULL AND sx.source!=?""",
        (event_id, source),
    ).fetchall()
    by_id = {canonical(row[1]): row for row in old}
    by_slot = {
        (row[2], (row[3] or "")[:5], row[4] or "", row[5] or ""): row
        for row in old
    }
    findings = []
    for game in games:
        if game.get("home_score") is None or game.get("away_score") is None:
            continue
        key = canonical(game.get("mapping_source_id", game["source_id"]))
        slot = (
            game.get("date"), (game.get("time") or "")[:5],
            (game.get("home_name") or "").casefold(),
            (game.get("away_name") or "").casefold(),
        )
        prior = by_id.get(key) or by_slot.get(slot)
        if prior is None or (
            int(prior[6]) == int(game["home_score"]) and
            int(prior[7]) == int(game["away_score"])
        ):
            continue
        details = json.dumps({
            "source_a": prior[0],
            "source_a_score": [prior[6], prior[7]],
            "source_b": source,
            "source_b_score": [game["home_score"], game["away_score"]],
            "source_game_id": key,
        }, sort_keys=True, separators=(",", ":"))
        findings.append(("score_conflict", "blocking", details, prior[0], source))
    return findings


def replace_event(
    con: sqlite3.Connection,
    event_id: int,
    source: str,
    source_event_id: str,
    division_source_id: str,
    teams: list[dict[str, Any]],
    games: list[dict[str, Any]],
    standings: list[dict[str, Any]],
    roster_state: str,
    source_url: str,
    observed_at: str,
    payload_hash: str,
    roster_entries: list[dict[str, Any]] | None = None,
) -> None:
    rostered_teams = (
        {
            canonical(person["team_source_id"])
            for person in roster_entries
            if person.get("team_source_id") is not None and person.get("name")
        }
        if roster_entries is not None else None
    )
    score_conflicts = _score_conflicts(con, event_id, source, games)
    _clear_event(con, event_id)
    local_team: dict[str, str] = {}
    event_team: dict[str, str] = {}
    for team in teams:
        sid = canonical(team["source_id"])
        canonical_id = _upsert_canonical_team(
            con, source, sid, team["name"], team.get("source_url", source_url),
            team.get("observed_at", observed_at),
            team.get("payload_hash", payload_hash), team.get("country"),
        )
        event_team_id = stable_key(f"euf-event-{event_id}", sid)
        con.execute(
            """INSERT INTO event_teams
               (event_team_id,event_id,display_name,full_name,city,
                roster_fetched,canonical_team_id) VALUES (?,?,?,?,?,?,?)""",
            (event_team_id, event_id, team["name"], team["name"],
             team.get("city"),
             int(sid in rostered_teams) if rostered_teams is not None
             else int(roster_state == "public"),
             canonical_id),
        )
        local_team[sid] = canonical_id
        event_team[sid] = event_team_id

    for game in games:
        home = event_team.get(canonical(game["home_source_id"])) \
            if game.get("home_source_id") is not None else None
        away = event_team.get(canonical(game["away_source_id"])) \
            if game.get("away_source_id") is not None else None
        con.execute(
            """INSERT INTO games
               (event_id,game_key,slot,stage,date,time,home_id,away_id,
                home_score,away_score,status,stage_pub)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, canonical(game["source_id"]), game.get("field"),
             game.get("stage"), game.get("date"), game.get("time"), home, away,
             game.get("home_score"), game.get("away_score"), game["state"],
             game.get("stage_pub")),
        )
        link_sources(
            con, source, "game", game.get("mapping_source_id", game["source_id"]),
            f"{event_id}|{game['source_id']}", game.get("source_url", source_url),
            game.get("observed_at", observed_at),
            game.get("payload_hash", payload_hash),
        )
    con.executemany(
        """INSERT INTO audit_findings
           (event_id,code,severity,detail,source_a,source_b,blocking)
           VALUES (?,?,?,?,?,?,1)""",
        [(event_id, code, severity, detail, source_a, source_b)
         for code, severity, detail, source_a, source_b in score_conflicts],
    )

    for standing in standings:
        sid = canonical(standing["team_source_id"])
        if sid not in event_team:
            raise ValueError(f"standing references unknown team {sid}")
        place = int(standing["place"])
        con.execute(
            """INSERT INTO standings
               (event_id,division,place,event_team_id,source) VALUES (?,?,?,?,?)""",
            (event_id, standing["division"], place, event_team[sid], source),
        )
        link_sources(
            con, source, "standing", standing["source_id"],
            f"{event_id}|{standing['division']}|{place}|{event_team[sid]}",
            standing.get("source_url", source_url),
            standing.get("observed_at", observed_at),
            standing.get("payload_hash", payload_hash),
        )

    for person in roster_entries or []:
        team_sid = canonical(person["team_source_id"])
        if team_sid not in event_team or not person.get("name"):
            continue
        person_sid = canonical(person["source_id"])
        person_id = stable_key(source + ":person", person_sid)
        con.execute(
            """INSERT INTO canonical_people(person_id,display_name,created_at)
               VALUES (?,?,?) ON CONFLICT(person_id) DO UPDATE SET
               display_name=excluded.display_name""",
            (person_id, person["name"], person.get("observed_at", observed_at)),
        )
        link_sources(
            con, source, "person", person_sid, person_id,
            person.get("source_url", source_url),
            person.get("observed_at", observed_at),
            person.get("payload_hash", payload_hash),
        )
        con.execute(
            """INSERT OR REPLACE INTO roster_entries
               (event_team_id,number,name,pronouns,position,height,points,
                assists,ds,turns) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (event_team[team_sid], person.get("number"), person["name"],
             person.get("pronouns"), person.get("position"), person.get("height"),
             person.get("points"), person.get("assists"), person.get("ds"),
             person.get("turns")),
        )

    con.execute(
        """INSERT INTO roster_availability
           (event_id,source,state,detail,observed_at) VALUES (?,?,?,?,?)""",
        (event_id, source, roster_state,
         "Public roster rows only; restricted data was not inferred as empty.",
         observed_at),
    )
    con.execute(
        "UPDATE euf_event_details SET roster_state=? WHERE event_id=?",
        (roster_state, event_id),
    )
    link_sources(con, source, "event", source_event_id, event_id,
                 source_url, observed_at, payload_hash)
    link_sources(con, source, "division", division_source_id, event_id,
                 source_url, observed_at, payload_hash)


def validate_event(con: sqlite3.Connection, event_id: int) -> dict[str, int]:
    invalid = con.execute(
        """SELECT COUNT(*) FROM games WHERE event_id=?
           AND status IN ('played','has_outcome')
           AND (home_id IS NULL OR away_id IS NULL OR home_score IS NULL
                OR away_score IS NULL)""",
        (event_id,),
    ).fetchone()[0]
    duplicate_mappings = con.execute(
        """SELECT COUNT(*) FROM (
             SELECT source,entity_type,source_id,COUNT(DISTINCT local_key) n
             FROM source_entities GROUP BY 1,2,3 HAVING n>1
           )"""
    ).fetchone()[0]
    if invalid:
        raise ValueError(f"event {event_id} has {invalid} incomplete played games")
    if duplicate_mappings:
        raise ValueError(f"database has {duplicate_mappings} duplicate source mappings")
    return {
        "teams": con.execute(
            "SELECT COUNT(*) FROM event_teams WHERE event_id=?", (event_id,)
        ).fetchone()[0],
        "games": con.execute(
            "SELECT COUNT(*) FROM games WHERE event_id=?", (event_id,)
        ).fetchone()[0],
        "played": con.execute(
            """SELECT COUNT(*) FROM games WHERE event_id=?
               AND status IN ('played','has_outcome')""", (event_id,)
        ).fetchone()[0],
    }


def _euf_division(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = {"men": "open", "mens": "open", "women-s": "women"}.get(slug, slug)
    return "euf-" + (slug or "unknown")


def _schedule_teams(schedule: Schedule, division: str) -> list[dict[str, Any]]:
    names = sorted({name for game in schedule.games if game["division"] == division
                    for name in (game["home"], game["away"]) if name})
    return [{
        "source_id": f"{schedule.season_code}|{division}|{name}",
        "name": name,
    } for name in names]


def ingest_event(
    con: sqlite3.Connection,
    season: int,
    schedule: Schedule,
) -> list[dict[str, Any]]:
    """Transactionally replace every division in one EUCS schedule event."""
    summaries = []
    with con:
        observe(
            con, EUCS_SOURCE, "schedule", schedule.season_code,
            source_url=schedule.source_url, observed_at=schedule.observed_at,
            payload_hash=schedule.payload_hash, count=len(schedule.games),
        )
        for division in schedule.divisions:
            local_division = _euf_division(division)
            selected = [game for game in schedule.games
                        if game["division"] == division]
            teams = _schedule_teams(schedule, division)
            source_for_name = {team["name"]: team["source_id"] for team in teams}
            games = [{
                "source_id": game["source_game_id"],
                "mapping_source_id": f"{schedule.season_code}|{game['source_game_id']}",
                "field": game["field"],
                "stage": game["stage"],
                "date": game["date"],
                "time": game["time"],
                "home_source_id": source_for_name.get(game["home"]),
                "away_source_id": source_for_name.get(game["away"]),
                "home_score": game["home_score"],
                "away_score": game["away_score"],
                "state": game["state"],
                "home_name": game["home"],
                "away_name": game["away"],
            } for game in selected]
            event_id = upsert_event(
                con, season, schedule.name, schedule.source_url, local_division,
                schedule.season_code, EUCS_SOURCE,
                start_date=min((g["date"] for g in selected if g["date"]), default=None),
                end_date=max((g["date"] for g in selected if g["date"]), default=None),
            )
            replace_event(
                con, event_id, EUCS_SOURCE,
                f"{schedule.season_code}|{division}",
                f"{schedule.season_code}|{division}",
                teams, games, [], "unavailable", schedule.source_url,
                schedule.observed_at, schedule.payload_hash,
            )
            stats = validate_event(con, event_id)
            con.execute(
                "UPDATE events SET complete=? WHERE event_id=?",
                (0 if any(g["state"] in ("scheduled", "placeholder")
                          for g in selected) else 1, event_id),
            )
            summaries.append({"event_id": event_id, "division": division, **stats})
        _record_candidates(con, season, schedule.name)
    return summaries


def _page_for_item(result: CollectionResult, item: dict[str, Any]) -> APIResponse:
    for page in result.pages:
        if item in page.result:
            return page
    return result.pages[0]


def _flatten_standings(response: APIResponse) -> list[dict[str, Any]]:
    rows = []
    for bundle in response.result:
        if not isinstance(bundle, dict):
            continue
        for stage_id, stage in bundle.items():
            if not isinstance(stage, dict):
                continue
            if stage.get("is_game_report"):
                continue
            division = stage.get("subtitle") or stage.get("title") or "Unknown"
            teams = stage.get("teams")
            if not isinstance(teams, list):
                continue
            for team in teams:
                if not isinstance(team, dict):
                    continue
                if team.get("id") is None or team.get("place") is None:
                    continue
                rows.append({
                    "source_id": f"{stage_id}|{team['id']}",
                    "team_source_id": str(team["id"]),
                    "team": team,
                    "division": division,
                    "place": int(team["place"]),
                })
    return rows


def _public_person_rows(result: CollectionResult) -> list[dict[str, Any]]:
    if result.state != "ok":
        return []
    rows = []
    for person in result.items:
        # Intentionally retain only public identity and team membership fields.
        name = person.get("name") or " ".join(
            x for x in (person.get("first_name"), person.get("last_name")) if x
        ).strip()
        team_id = person.get("team_id")
        if person.get("id") is not None and team_id is not None and name:
            page = _page_for_item(result, person)
            rows.append({
                "source_id": str(person["id"]),
                "team_source_id": str(team_id),
                "name": name,
                "source_url": page.source_url,
                "observed_at": page.observed_at,
                "payload_hash": page.payload_hash,
            })
    return rows


def ingest_ultimate_central_event(
    con: sqlite3.Connection,
    client: UltimateCentralClient,
    provider_event_id: str | int,
) -> list[dict[str, Any]]:
    provider_event_id = str(provider_event_id)
    event_result = client.list_events(id=provider_event_id)
    if event_result.state != "ok" or len(event_result.items) != 1:
        raise ValueError(f"Ultimate Central event {provider_event_id} is unavailable")
    event = event_result.items[0]
    event_page = _page_for_item(event_result, event)
    teams_result = client.list_teams(provider_event_id)
    games_result = client.list_games(provider_event_id)
    standings_response = client.final_standings(provider_event_id)
    persons_result = client.list_public_persons(provider_event_id)
    for kind, result in (("events", event_result), ("teams", teams_result),
                         ("games", games_result), ("persons", persons_result)):
        for page in result.pages:
            observe(con, UC_SOURCE, kind, provider_event_id, page)
    observe(con, UC_SOURCE, "standings", provider_event_id, standings_response)

    standings = _flatten_standings(standings_response)
    divisions = sorted({str(x.get("division_name")) for x in teams_result.items
                        if x.get("division_name")}
                       | {str(x.get("division_name")) for x in games_result.items
                          if x.get("division_name")}
                       | {row["division"] for row in standings})
    start = event.get("start") or event.get("start_date")
    end = event.get("end") or event.get("end_date")
    year_match = re.match(r"(\d{4})", str(start or ""))
    if not year_match:
        raise ValueError(f"Ultimate Central event {provider_event_id} has no source year")
    season = int(year_match.group(1))
    slug = event.get("slug") or provider_event_id
    human_url = f"https://euf.ultimatecentral.com/e/{slug}"
    roster_state = persons_result.state
    if roster_state == "ok":
        roster_state = "public"
    summaries = []
    with con:
        for division in divisions:
            local_division = _euf_division(division)
            selected_teams: dict[str, dict[str, Any]] = {}
            selected_team_pages: dict[str, APIResponse] = {}
            for team in teams_result.items:
                if team.get("division_name") == division and team.get("id") is not None:
                    sid = str(team["id"])
                    selected_teams[sid] = team
                    selected_team_pages[sid] = _page_for_item(teams_result, team)
            selected_games = [game for game in games_result.items
                              if game.get("division_name") == division]
            selected_standings = [row for row in standings if row["division"] == division]
            for game in selected_games:
                game_page = _page_for_item(games_result, game)
                for key in ("HomeTeam", "AwayTeam"):
                    team = game.get(key) or {}
                    if team.get("id") is not None:
                        sid = str(team["id"])
                        selected_teams.setdefault(sid, team)
                        selected_team_pages.setdefault(sid, game_page)
            for row in selected_standings:
                team = row["team"]
                sid = str(team["id"])
                selected_teams.setdefault(sid, team)
                selected_team_pages.setdefault(sid, standings_response)
            teams = []
            for sid, team in selected_teams.items():
                page = selected_team_pages[sid]
                teams.append({
                    "source_id": sid,
                    "name": team.get("name") or team.get("slug") or sid,
                    "country": team.get("country"),
                    "city": team.get("locality"),
                    "source_url": page.source_url,
                    "observed_at": page.observed_at,
                    "payload_hash": page.payload_hash,
                })
            games = []
            for game in selected_games:
                page = _page_for_item(games_result, game)
                is_forfeit = bool(game.get("is_forfeit"))
                scored = (game.get("home_score") is not None and
                          game.get("away_score") is not None)
                if is_forfeit:
                    state = "forfeit"
                elif scored:
                    state = "has_outcome"
                elif game.get("is_played"):
                    state = "incomplete"
                else:
                    source_state = game.get("status") or "scheduled"
                    state = "incomplete" if source_state == "has_outcome" else source_state
                games.append({
                    "source_id": str(game["id"]),
                    "field": str(game.get("field_name") or game.get("field_number") or ""),
                    "stage": game.get("stage_name"),
                    "date": game.get("start_date"),
                    "time": game.get("start_time"),
                    "home_source_id": game.get("home_team_id"),
                    "away_source_id": game.get("away_team_id"),
                    "home_score": game.get("home_score"),
                    "away_score": game.get("away_score"),
                    "home_name": (
                        selected_teams.get(str(game.get("home_team_id")), {}).get("name")
                    ),
                    "away_name": (
                        selected_teams.get(str(game.get("away_team_id")), {}).get("name")
                    ),
                    "state": state,
                    "source_url": page.source_url,
                    "observed_at": page.observed_at,
                    "payload_hash": page.payload_hash,
                })
            standing_rows = [{
                **row,
                "source_url": standings_response.source_url,
                "observed_at": standings_response.observed_at,
                "payload_hash": standings_response.payload_hash,
            } for row in selected_standings]
            event_id = upsert_event(
                con, season, event.get("name") or f"Ultimate Central {provider_event_id}",
                human_url, local_division, f"uc:{provider_event_id}", UC_SOURCE,
                provider_event_id, event.get("locality"), None, start, end,
            )
            replace_event(
                con, event_id, UC_SOURCE,
                f"{provider_event_id}|{division}",
                f"{provider_event_id}|{division}",
                teams, games, standing_rows, roster_state,
                event_page.source_url, event_page.observed_at,
                event_page.payload_hash, _public_person_rows(persons_result),
            )
            stats = validate_event(con, event_id)
            con.execute("UPDATE events SET complete=1 WHERE event_id=?", (event_id,))
            summaries.append({"event_id": event_id, "division": division, **stats,
                              "standings": len(standing_rows)})
        _record_candidates(con, season, event.get("name") or "")
    return summaries


def _record_candidates(con: sqlite3.Connection, season: int, name: str) -> None:
    norm = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not norm:
        return
    rows = list(con.execute(
        """SELECT d.event_id,d.event_code,d.source_owner,e.name,e.division
           FROM euf_event_details d JOIN events e USING(event_id)
           WHERE e.season=?""", (season,)
    ))
    for a in rows:
        for b in rows:
            if a[0] >= b[0] or a[2] == b[2] or a[4] != b[4]:
                continue
            if re.sub(r"[^a-z0-9]+", "", a[3].lower()) != \
                    re.sub(r"[^a-z0-9]+", "", b[3].lower()):
                continue
            con.execute(
                """INSERT OR IGNORE INTO source_mapping_candidates
                   (source_a,entity_type,source_id_a,source_b,source_id_b,reason)
                   VALUES (?,?,?,?,?,?)""",
                (a[2], "event", a[1], b[2], b[1],
                 "same normalized name, season, and division; requires acceptance"),
            )


def discover_events(
    session: requests.Session,
    years: Iterable[int] | None = None,
    refresh: bool = False,
) -> list[SeasonRef]:
    refs = discover_seasons(session, refresh=refresh)
    if years is None:
        return refs
    wanted = {int(year) for year in years}
    return [ref for ref in refs if any(str(year) in ref.name for year in wanted)]


def _probe_uc(client: UltimateCentralClient) -> dict[str, Any]:
    help_response = client.get_help()
    events = client.list_events(per_page=100)
    return {
        "source": UC_SOURCE,
        "help_status": help_response.status,
        "endpoint_count": help_response.count,
        "events_state": events.state,
        "event_count": events.count,
        "public_rosters": "event-specific; probe /api/persons before ingestion",
        "requests": client.requests_made,
    }


def _probe_eucs(session: requests.Session, season: str, refresh: bool) -> dict[str, Any]:
    document = fetch_schedule(season, session, refresh=refresh)
    schedule = parse_schedule(document, season)
    played = [game for game in schedule.games if game["state"] == "played"]
    unresolved = [game for game in played
                  if game["home"] is None or game["away"] is None]
    calendar = fetch_ical(season, session, refresh=refresh)
    calendar_check = cross_check_ical(schedule, calendar)
    return {
        "source": EUCS_SOURCE,
        "season": schedule.season_code,
        "divisions": schedule.divisions,
        "games": len(schedule.games),
        "played": len(played),
        "played_without_team": len(unresolved),
        "payload_hash": schedule.payload_hash,
        "from_cache": document.from_cache,
        "calendar_cross_check": calendar_check,
    }


def audit(con: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
    by_source_year = [dict(source=row[0], year=row[1], events=row[2]) for row in
                      con.execute("""
        SELECT s.source,e.season,COUNT(DISTINCT e.event_id)
        FROM source_entities s JOIN events e ON s.entity_type='event'
          AND s.local_key=CAST(e.event_id AS TEXT)
        GROUP BY 1,2 ORDER BY 1,2""")]
    roster_states = dict(con.execute(
        "SELECT state,COUNT(*) FROM roster_availability GROUP BY state"
    ))
    invalid_played = con.execute("""
        SELECT COUNT(*) FROM games WHERE status IN ('played','has_outcome')
          AND (home_id IS NULL OR away_id IS NULL OR home_score IS NULL
               OR away_score IS NULL)""").fetchone()[0]
    duplicate_mappings = con.execute("""
        SELECT COUNT(*) FROM (
          SELECT source,entity_type,source_id,COUNT(DISTINCT local_key) n
          FROM source_entities GROUP BY 1,2,3 HAVING n>1)""").fetchone()[0]
    conflicts = con.execute(
        "SELECT COUNT(*) FROM audit_findings WHERE code='score_conflict'"
    ).fetchone()[0]
    unresolved = con.execute(
        "SELECT COUNT(*) FROM source_mapping_candidates WHERE accepted=0"
    ).fetchone()[0]
    blocking_findings = con.execute(
        "SELECT COUNT(*) FROM audit_findings WHERE blocking=1"
    ).fetchone()[0]
    provenance_gaps = {
        "events": con.execute(
            """SELECT COUNT(*) FROM events e WHERE EXISTS (
                 SELECT 1 FROM euf_event_details d WHERE d.event_id=e.event_id)
               AND NOT EXISTS (
                 SELECT 1 FROM source_entities s WHERE s.entity_type='event'
                   AND s.local_key=CAST(e.event_id AS TEXT))"""
        ).fetchone()[0],
        "games": con.execute(
            """SELECT COUNT(*) FROM games g WHERE NOT EXISTS (
                 SELECT 1 FROM source_entities s WHERE s.entity_type='game'
                   AND s.local_key=CAST(g.event_id AS TEXT)||'|'||g.game_key)"""
        ).fetchone()[0],
        "teams": con.execute(
            """SELECT COUNT(*) FROM canonical_teams t WHERE NOT EXISTS (
                 SELECT 1 FROM source_entities s WHERE s.entity_type='team'
                   AND s.local_key=t.team_id)"""
        ).fetchone()[0],
        "standings": max(
            0,
            con.execute("SELECT COUNT(*) FROM standings").fetchone()[0] -
            con.execute(
                "SELECT COUNT(*) FROM source_entities WHERE entity_type='standing'"
            ).fetchone()[0],
        ),
    }
    dates = con.execute("SELECT MIN(start_date),MAX(end_date) FROM events").fetchone()
    report = {
        "events_by_source_year": by_source_year,
        "divisions": dict(con.execute(
            "SELECT division,COUNT(*) FROM events GROUP BY division ORDER BY division"
        )),
        "teams": con.execute("SELECT COUNT(*) FROM canonical_teams").fetchone()[0],
        "event_teams": con.execute("SELECT COUNT(*) FROM event_teams").fetchone()[0],
        "scheduled_games": con.execute(
            "SELECT COUNT(*) FROM games WHERE status IN ('scheduled','placeholder','teams_not_set')"
        ).fetchone()[0],
        "scored_games": con.execute(
            "SELECT COUNT(*) FROM games WHERE home_score IS NOT NULL AND away_score IS NOT NULL"
        ).fetchone()[0],
        "public_roster_entries": con.execute(
            "SELECT COUNT(*) FROM roster_entries"
        ).fetchone()[0],
        "ranking_rosters": con.execute(
            "SELECT COUNT(*) FROM ranking_roster_observations"
        ).fetchone()[0],
        "ranking_roster_memberships": con.execute(
            "SELECT COUNT(*) FROM ranking_roster_entries"
        ).fetchone()[0],
        "ranking_roster_display_names": con.execute(
            "SELECT COUNT(DISTINCT player_name) FROM ranking_roster_entries"
        ).fetchone()[0],
        "ranking_roster_empty": con.execute(
            "SELECT COUNT(*) FROM ranking_roster_observations WHERE record_count=0"
        ).fetchone()[0],
        "roster_states": roster_states,
        "game_states": dict(con.execute(
            "SELECT status,COUNT(*) FROM games GROUP BY status ORDER BY status"
        )),
        "standings": con.execute("SELECT COUNT(*) FROM standings").fetchone()[0],
        "provenance_gaps": provenance_gaps,
        "unresolved_mappings": unresolved,
        "duplicate_source_mappings": duplicate_mappings,
        "score_conflicts": conflicts,
        "invalid_played_games": invalid_played,
        "blocking_findings": blocking_findings,
        "first_date": dates[0],
        "last_date": dates[1],
        "source_observation_states": dict(con.execute(
            "SELECT state,COUNT(*) FROM source_observations GROUP BY state"
        )),
    }
    blocking = bool(
        duplicate_mappings or invalid_played or blocking_findings or
        any(provenance_gaps.values())
    )
    report["publication_blocked"] = blocking
    return report, blocking


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("years", nargs="*", type=int)
    parser.add_argument("--db", type=Path, default=EUF_DB)
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--probe", choices=("ultimate-central", "eucs"))
    parser.add_argument("--season", dest="season_code")
    parser.add_argument("--event")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--request-budget", type=int, default=500)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    session = requests.Session()
    client = UltimateCentralClient(
        session, request_budget=args.request_budget, backoff=1.0
    )
    try:
        if args.probe == "ultimate-central":
            print(json.dumps(_probe_uc(client), indent=2, sort_keys=True))
            return 0
        if args.probe == "eucs":
            if not args.season_code:
                raise SystemExit("--probe eucs requires --season")
            print(json.dumps(_probe_eucs(session, args.season_code, args.refresh),
                             indent=2, sort_keys=True))
            return 0

        con = init_db(args.db)
        try:
            if args.audit:
                report, blocked = audit(con)
                print(json.dumps(report, indent=2, sort_keys=True))
                return 2 if blocked else 0
            if args.init_db and not (args.event or args.backfill or args.years):
                print(f"initialized {args.db}")
                return 0
            if args.event:
                if args.event.startswith("uc:"):
                    summaries = ingest_ultimate_central_event(
                        con, client, args.event.split(":", 1)[1]
                    )
                else:
                    if len(args.years) != 1:
                        raise SystemExit("EUCS --event requires exactly one explicit year")
                    document = fetch_schedule(args.event, session, refresh=args.refresh)
                    summaries = ingest_event(
                        con, args.years[0], parse_schedule(document, args.event)
                    )
                print(json.dumps(summaries, indent=2, sort_keys=True))
                return 0
            if args.backfill:
                failures = []
                events = client.list_events(per_page=100, order_by="date_asc")
                for event in events.items:
                    try:
                        result = ingest_ultimate_central_event(con, client, event["id"])
                        print(json.dumps({"ultimate_central": event["id"], "result": result}))
                    except Exception as exc:  # preserve progress and exact gap
                        failures.append(f"ultimate-central:{event.get('id')}: {exc}")
                for ref in discover_events(session, refresh=args.refresh):
                    match = re.search(r"\b(20\d{2})\b", ref.name)
                    if not match:
                        failures.append(f"eucs:{ref.code}: no source year in {ref.name!r}")
                        continue
                    try:
                        document = fetch_schedule(ref.code, session, refresh=args.refresh)
                        result = ingest_event(
                            con, int(match.group(1)), parse_schedule(document, ref.code)
                        )
                        print(json.dumps({"eucs": ref.code, "result": result}))
                    except Exception as exc:
                        failures.append(f"eucs:{ref.code}: {exc}")
                for failure in failures:
                    print(f"UNAVAILABLE {failure}", file=sys.stderr)
                report, blocked = audit(con)
                print(json.dumps(report, indent=2, sort_keys=True))
                return 2 if blocked else 0
            if args.years:
                summaries = []
                for ref in discover_events(session, args.years, args.refresh):
                    match = re.search(r"\b(20\d{2})\b", ref.name)
                    if not match:
                        continue
                    document = fetch_schedule(ref.code, session, refresh=args.refresh)
                    summaries.extend(ingest_event(
                        con, int(match.group(1)), parse_schedule(document, ref.code)
                    ))
                print(json.dumps(summaries, indent=2, sort_keys=True))
                return 0
            raise SystemExit("choose --init-db, --probe, --event, --backfill, or --audit")
        finally:
            con.close()
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
