"""Adapt EUCS games and season rosters to the player-Elo replay contract.

EUCS Ranking exposes names but no stable player IDs. European-only IDs are
therefore deterministic, source-scoped negative integers. A European roster
name reuses a USAU player ID only when the exact case/spacing key maps to one
non-ambiguous USAU identity and both sources show that identity in the same
calendar season. Those bridges are reviewable name matches, not provider-ID
matches.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analysis.euf_overlap import exact_name_key

ROOT = Path(__file__).resolve().parent.parent
EUF_DB = ROOT / "data" / "euf.db"


def team_name_key(name: str | None) -> str:
    value = unicodedata.normalize("NFKD", name or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", value).strip()


def european_player_id(source_key: str) -> int:
    """Stable negative ID outside USAU, kept inside JavaScript's safe range."""
    digest = hashlib.sha256(f"eucs-ranking\0{source_key}".encode()).hexdigest()
    return -int(digest[:13], 16)


@dataclass
class EuropeanInputs:
    games: list[dict[str, Any]] = field(default_factory=list)
    rosters: dict[str, list[int]] = field(default_factory=dict)
    clubs: dict[str, str] = field(default_factory=dict)
    player_names: dict[int, str] = field(default_factory=dict)
    latest: dict[int, tuple[str, str, int]] = field(default_factory=dict)
    appearances: list[tuple[int, int, str, str]] = field(default_factory=list)
    event_info: dict[int, tuple[str, str, int, str]] = field(default_factory=dict)
    event_team_event: dict[str, int] = field(default_factory=dict)
    event_roster_rows: list[tuple[int, str, int, str]] = field(default_factory=list)
    team_rosters: dict[
        tuple[int, str],
        tuple[dict[str, list[int]], dict[str, tuple[str, str]], dict[str, str]],
    ] = field(default_factory=dict)
    team_names: dict[str, tuple[str, str]] = field(default_factory=dict)
    bridge_rows: list[dict[str, Any]] = field(default_factory=list)
    covered_scored_games: int = 0
    ghost_scored_games: int = 0


def _usa_bridge_candidates(
    con: sqlite3.Connection,
    eu_names: dict[str, set[str]],
    eu_seasons: dict[str, set[int]],
    colliding: set[str],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    usa: dict[str, list[tuple[int, str, bool]]] = defaultdict(list)
    for player_id, name, ambiguous in con.execute(
        "SELECT player_id,display_name,ambiguous FROM players"
    ):
        usa[exact_name_key(name)].append((int(player_id), name, bool(ambiguous)))
    usa_seasons: dict[int, set[int]] = defaultdict(set)
    for player_id, season in con.execute(
        """SELECT DISTINCT rp.player_id,e.season
           FROM roster_players rp
           JOIN event_teams et USING(event_team_id)
           JOIN events e USING(event_id)"""
    ):
        usa_seasons[int(player_id)].add(int(season))

    bridges: dict[str, int] = {}
    audit = []
    for key in sorted(eu_names):
        candidates = usa.get(key, [])
        if (
            key in colliding
            or len(eu_names[key]) != 1
            or len(candidates) != 1
            or candidates[0][2]
        ):
            continue
        player_id, usa_name, _ = candidates[0]
        shared = sorted(eu_seasons[key] & usa_seasons[player_id])
        if not shared:
            continue
        bridges[key] = player_id
        audit.append({
            "name_key": key,
            "eu_name": next(iter(eu_names[key])),
            "usau_player_id": player_id,
            "usau_name": usa_name,
            "shared_seasons": shared,
        })
    return bridges, audit


def load_european_inputs(
    usa_con: sqlite3.Connection,
    euf_path: Path = EUF_DB,
) -> EuropeanInputs:
    out = EuropeanInputs()
    con = sqlite3.connect(f"file:{euf_path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        observations = con.execute(
            """SELECT source,roster_id,season,division,team_name,observed_at,
                      record_count
               FROM ranking_roster_observations
               ORDER BY season,division,team_name"""
        ).fetchall()
        entries = con.execute(
            """SELECT r.roster_id,r.ordinal,r.player_name,r.name_key,
                      o.season,o.division,o.team_name
               FROM ranking_roster_entries r
               JOIN ranking_roster_observations o
                 ON o.source=r.source AND o.roster_id=r.roster_id
               ORDER BY o.season,o.division,o.team_name,r.ordinal"""
        ).fetchall()

        eu_names: dict[str, set[str]] = defaultdict(set)
        eu_seasons: dict[str, set[int]] = defaultdict(set)
        teams_by_name_season: dict[tuple[str, int, str], set[str]] = defaultdict(set)
        for row in entries:
            key = row["name_key"]
            eu_names[key].add(row["player_name"])
            eu_seasons[key].add(int(row["season"]))
            teams_by_name_season[(key, int(row["season"]), row["division"])].add(
                team_name_key(row["team_name"])
            )
        colliding = {
            key for (key, _season, _division), teams in teams_by_name_season.items()
            if len(teams) > 1
        }
        bridges, out.bridge_rows = _usa_bridge_candidates(
            usa_con, eu_names, eu_seasons, colliding
        )

        entry_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in entries:
            entry_rows[row["roster_id"]].append(row)
        roster_lookup: dict[tuple[int, str, str], tuple[list[int], list[tuple[int, str]]]] = {}
        appearance_seen: set[tuple[int, int, str, str]] = set()
        latest_order: dict[int, tuple[int, str]] = {}
        for observation in observations:
            season = int(observation["season"])
            division = observation["division"]
            team_name = observation["team_name"]
            team_key = team_name_key(team_name)
            club_key = f"euf:{division}:{team_key}"
            pids: list[int] = []
            people: list[tuple[int, str]] = []
            seen: set[int] = set()
            for row in entry_rows.get(observation["roster_id"], []):
                name_key = row["name_key"]
                source_key = (
                    f"{name_key}|{division}|{team_key}"
                    if name_key in colliding else name_key
                )
                player_id = bridges.get(name_key, european_player_id(source_key))
                if player_id in seen:
                    continue
                seen.add(player_id)
                pids.append(player_id)
                people.append((player_id, row["player_name"]))
                out.player_names[player_id] = row["player_name"]
                appearance = (player_id, season, division, str(season))
                if appearance not in appearance_seen:
                    out.appearances.append(appearance)
                    appearance_seen.add(appearance)
                order = (season, observation["observed_at"])
                if player_id not in latest_order or order >= latest_order[player_id]:
                    latest_order[player_id] = order
                    out.latest[player_id] = (row["player_name"], team_name, season)
            roster_lookup[(season, division, team_key)] = (pids, people)
            by_club, source, display = out.team_rosters.setdefault(
                (season, division), ({}, {}, {})
            )
            by_club[club_key] = pids
            source[club_key] = (
                f"EUCS Ranking {season} season roster",
                observation["observed_at"][:10],
            )
            display[club_key] = team_name
            out.team_names[club_key] = (observation["observed_at"][:10], team_name)

        event_rows = con.execute(
            """SELECT e.event_id,e.name,e.season,e.division,e.start_date,e.end_date,
                      et.event_team_id,et.display_name,et.full_name
               FROM event_teams et JOIN events e USING(event_id)
               WHERE e.season IN (
                 SELECT DISTINCT season FROM ranking_roster_observations
               )"""
        ).fetchall()
        source_event_team: dict[str, str] = {}
        source_event: dict[int, int] = {}
        for row in event_rows:
            season, division = int(row["season"]), row["division"]
            candidates = [row["full_name"], row["display_name"]]
            match = None
            matched_team_key = None
            for name in candidates:
                key = team_name_key(name)
                candidate = roster_lookup.get((season, division, key))
                if candidate is not None:
                    match = candidate
                    matched_team_key = key
                    break
            if match is None:
                raise ValueError(
                    f"no ranking roster mapping for EU event team "
                    f"{season} {division} {row['display_name']!r}"
                )
            pids, people = match
            event_id = -int(row["event_id"])
            event_team_id = f"euf:{row['event_team_id']}"
            source_event[int(row["event_id"])] = event_id
            source_event_team[row["event_team_id"]] = event_team_id
            display_name = row["full_name"] or row["display_name"]
            club_key = f"euf:{division}:{matched_team_key}"
            out.rosters[event_team_id] = pids
            out.clubs[event_team_id] = club_key
            out.event_team_event[event_team_id] = event_id
            out.event_info[event_id] = (
                row["name"], row["start_date"] or row["end_date"] or str(season),
                season, division,
            )
            out.team_names[club_key] = (
                row["start_date"] or "", display_name
            )
            for player_id, player_name in people:
                out.event_roster_rows.append(
                    (event_id, event_team_id, player_id, player_name)
                )

        for row in con.execute(
            """SELECT g.event_id,g.game_key,g.date,g.time,g.home_id,g.away_id,
                      g.home_score,g.away_score,g.stage,e.season,e.division,
                      e.start_date,e.end_date
               FROM games g JOIN events e USING(event_id)
               WHERE e.season IN (
                 SELECT DISTINCT season FROM ranking_roster_observations
               )
                 AND g.home_id IS NOT NULL AND g.away_id IS NOT NULL
                 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                 AND g.status IN ('played','has_outcome')
                 AND g.home_score + g.away_score >= 4"""
        ):
            home = source_event_team.get(row["home_id"])
            away = source_event_team.get(row["away_id"])
            if home is None or away is None:
                raise ValueError(
                    f"EU game {row['event_id']}|{row['game_key']} has no team mapping"
                )
            effective = row["date"] or row["start_date"] or f"{row['season']}-01-01"
            if row["start_date"]:
                effective = min(
                    max(effective, row["start_date"]),
                    row["end_date"] or row["start_date"],
                )
            event_id = source_event[int(row["event_id"])]
            out.games.append({
                "sort": (effective, row["time"] or "23:59", event_id, row["game_key"]),
                "date": effective,
                "season": int(row["season"]),
                "division": row["division"],
                "event_id": event_id,
                "game_key": f"euf:{row['game_key']}",
                "stage": row["stage"],
                "home_id": home,
                "away_id": away,
                "home_score": int(row["home_score"]),
                "away_score": int(row["away_score"]),
            })
            out.covered_scored_games += 1
            if not out.rosters.get(home) or not out.rosters.get(away):
                out.ghost_scored_games += 1
        out.games.sort(key=lambda game: game["sort"])
    finally:
        con.close()
    return out
