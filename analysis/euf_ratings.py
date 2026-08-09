"""Adapt EUCS games and season rosters to the player-Elo replay contract.

EUCS Ranking exposes names but no stable player IDs. European-only IDs are
therefore deterministic, source-scoped negative integers. A European roster
name reuses a USAU player ID only when its accent/punctuation/spacing-insensitive
key maps to one non-ambiguous identity, is not shared by two European teams in
the same season/division, and both sources show it in the same calendar season.
Those bridges are reviewable name matches, not provider-ID matches.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from analysis.euf_overlap import exact_name_key

ROOT = Path(__file__).resolve().parent.parent
EUF_DB = ROOT / "data" / "euf.db"


def team_name_key(name: str | None) -> str:
    value = unicodedata.normalize("NFKD", name or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", value).strip()

def compact_name_key(name: str | None) -> str:
    """Fold harmless display variation without guessing at different names."""
    value = unicodedata.normalize("NFKD", name or "").casefold()
    return "".join(
        char for char in value
        if char.isalnum() and not unicodedata.combining(char)
    )


def european_player_id(source_key: str) -> int:
    """Stable negative ID outside USAU, kept inside JavaScript's safe range."""
    digest = hashlib.sha256(f"eucs-ranking\0{source_key}".encode()).hexdigest()
    return -int(digest[:13], 16)


class Appearance(NamedTuple):
    """The newest roster sighting known for one player.

    Ordered on ``(season, date)``, so a dated real event always outranks a
    season-long registration that carries no play date. ``club`` is the name
    a human reads, never a model club key — this feeds the "last club"
    column directly. ``is_club`` is False for national-team entries, which
    are not clubs and must not answer "what club do they play for".
    """

    name: str
    club: str
    season: int
    date: str = ""
    is_club: bool = True

    @property
    def order(self) -> tuple[int, str]:
        return self.season, self.date


@dataclass
class EuropeanInputs:
    games: list[dict[str, Any]] = field(default_factory=list)
    rosters: dict[str, list[int]] = field(default_factory=dict)
    clubs: dict[str, str] = field(default_factory=dict)
    player_names: dict[int, str] = field(default_factory=dict)
    latest: dict[int, Appearance] = field(default_factory=dict)
    latest_club: dict[int, Appearance] = field(default_factory=dict)
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
    identity_rows: list[dict[str, Any]] = field(default_factory=list)
    covered_scored_games: int = 0
    ghost_scored_games: int = 0


def merge_inputs(*inputs: EuropeanInputs) -> EuropeanInputs:
    """Combine independent external corpora without hiding key collisions."""
    out = EuropeanInputs()
    appearances: set[tuple[int, int, str, str]] = set()

    def merge_map(target: dict, source: dict, label: str) -> None:
        for key, value in source.items():
            if key in target and target[key] != value:
                raise ValueError(f"conflicting {label} for {key!r}")
            target[key] = value

    for current in inputs:
        out.games.extend(current.games)
        merge_map(out.rosters, current.rosters, "roster")
        merge_map(out.clubs, current.clubs, "club")
        for player_id, name in current.player_names.items():
            out.player_names.setdefault(player_id, name)
        for player_id, value in current.latest.items():
            if player_id not in out.latest or value.order >= out.latest[player_id].order:
                out.latest[player_id] = value
        for player_id, value in current.latest_club.items():
            if (player_id not in out.latest_club
                    or value.order >= out.latest_club[player_id].order):
                out.latest_club[player_id] = value
        for appearance in current.appearances:
            if appearance not in appearances:
                out.appearances.append(appearance)
                appearances.add(appearance)
        merge_map(out.event_info, current.event_info, "event")
        merge_map(out.event_team_event, current.event_team_event, "event-team event")
        out.event_roster_rows.extend(current.event_roster_rows)
        for key, (rosters, source, display) in current.team_rosters.items():
            target = out.team_rosters.setdefault(key, ({}, {}, {}))
            merge_map(target[0], rosters, "published roster")
            merge_map(target[1], source, "published roster source")
            merge_map(target[2], display, "published roster display")
        for key, value in current.team_names.items():
            if key not in out.team_names or value[0] >= out.team_names[key][0]:
                out.team_names[key] = value
        out.bridge_rows.extend(current.bridge_rows)
        out.identity_rows.extend(current.identity_rows)
        out.covered_scored_games += current.covered_scored_games
        out.ghost_scored_games += current.ghost_scored_games
    out.games.sort(key=lambda game: game["sort"])
    return out


def _usa_bridge_candidates(
    con: sqlite3.Connection,
    eu_names: dict[str, set[str]],
    eu_seasons: dict[str, set[int]],
    colliding_compact: set[str],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    usa: dict[str, list[tuple[int, str, bool]]] = defaultdict(list)
    for player_id, name, ambiguous in con.execute(
        "SELECT player_id,display_name,ambiguous FROM players"
    ):
        usa[compact_name_key(name)].append(
            (int(player_id), name, bool(ambiguous))
        )
    usa_seasons: dict[int, set[int]] = defaultdict(set)
    for player_id, season in con.execute(
        """SELECT DISTINCT rp.player_id,e.season
           FROM roster_players rp
           JOIN event_teams et USING(event_team_id)
           JOIN events e USING(event_id)"""
    ):
        usa_seasons[int(player_id)].add(int(season))

    eu_groups: dict[str, set[str]] = defaultdict(set)
    for key in eu_names:
        eu_groups[compact_name_key(key)].add(key)

    bridges: dict[str, int] = {}
    audit = []
    for compact in sorted(eu_groups):
        keys = eu_groups[compact]
        candidates = usa.get(compact, [])
        if compact in colliding_compact or len(candidates) != 1:
            continue
        player_id, usa_name, ambiguous = candidates[0]
        if ambiguous:
            continue
        seasons = set().union(*(eu_seasons[key] for key in keys))
        shared = sorted(seasons & usa_seasons[player_id])
        if not shared:
            continue
        for key in keys:
            bridges[key] = player_id
        names = sorted(
            {name for key in keys for name in eu_names[key]},
            key=lambda name: (name.casefold(), name),
        )
        exact = (
            len(keys) == 1
            and next(iter(keys)) == exact_name_key(usa_name)
            and len(names) == 1
        )
        audit.append({
            "name_key": exact_name_key(usa_name),
            "eu_name": "; ".join(names),
            "eu_name_keys": ";".join(sorted(keys)),
            "match_method": "exact" if exact else "compact",
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
        teams_by_compact_season: dict[
            tuple[str, int, str], set[str]
        ] = defaultdict(set)
        for row in entries:
            key = row["name_key"]
            compact = compact_name_key(key)
            eu_names[key].add(row["player_name"])
            eu_seasons[key].add(int(row["season"]))
            teams_by_compact_season[
                (compact, int(row["season"]), row["division"])
            ].add(team_name_key(row["team_name"]))
        colliding_compact = {
            compact
            for (compact, _season, _division), teams
            in teams_by_compact_season.items()
            if len(teams) > 1
        }
        bridges, out.bridge_rows = _usa_bridge_candidates(
            usa_con, eu_names, eu_seasons, colliding_compact
        )
        bridge_names = {
            row["usau_player_id"]: row["usau_name"] for row in out.bridge_rows
        }

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
                compact = compact_name_key(name_key)
                source_key = (
                    f"{compact}|{division}|{team_key}"
                    if compact in colliding_compact else compact
                )
                player_id = bridges.get(name_key, european_player_id(source_key))
                if player_id in seen:
                    continue
                seen.add(player_id)
                display_name = bridge_names.get(player_id, row["player_name"])
                pids.append(player_id)
                people.append((player_id, display_name))
                out.player_names[player_id] = display_name
                appearance = (player_id, season, division, str(season))
                if appearance not in appearance_seen:
                    out.appearances.append(appearance)
                    appearance_seen.add(appearance)
                order = (season, observation["observed_at"])
                if player_id not in latest_order or order >= latest_order[player_id]:
                    latest_order[player_id] = order
                    out.latest[player_id] = Appearance(display_name, team_name, season)
                    out.latest_club[player_id] = out.latest[player_id]
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
               JOIN euf_event_details d USING(event_id)
               WHERE d.source_owner IN ('eucs-schedule','ultimate-central:euf')
                 AND e.season IN (
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
               JOIN euf_event_details d USING(event_id)
               WHERE d.source_owner IN ('eucs-schedule','ultimate-central:euf')
                 AND e.season IN (
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
