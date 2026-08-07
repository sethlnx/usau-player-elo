"""Adapt official WFDF event rosters and games to the Elo replay contract.

WFDF player IDs are scoped to one results installation, not globally stable.
The adapter therefore prefers conservative same-season USAU/EU name bridges,
then uses a deterministic international name ID. A name duplicated across two
teams at one championship is never bridged or merged automatically.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from analysis.backtest import CLUB_SUFFIX, norm_club
from analysis.euf_overlap import exact_name_key
from analysis.euf_ratings import (
    EUF_DB,
    EuropeanInputs,
    _usa_bridge_candidates,
    compact_name_key,
    team_name_key,
)
from scraper.wfdf import EVENTS, WFDF_SOURCE


def international_player_id(source_key: str) -> int:
    """Stable negative ID in a JS-safe range disjoint from EU-only IDs."""
    digest = hashlib.sha256(f"wfdf-results\0{source_key}".encode()).hexdigest()
    return -(2 ** 52 + int(digest[:13], 16))


def _usa_club_index(con: sqlite3.Connection) -> dict[tuple[str, str], set[str]]:
    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for full_name, display_name, division in con.execute(
        """SELECT et.full_name,et.display_name,e.division
           FROM event_teams et JOIN events e USING(event_id)"""
    ):
        name = full_name or display_name
        if not name:
            continue
        club = norm_club(full_name, display_name) + CLUB_SUFFIX.get(division, "")
        index[(division, team_name_key(name))].add(club)
    return index


def _european_club_index(
    european: EuropeanInputs,
) -> dict[tuple[str, str], set[str]]:
    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for club, (_date, name) in european.team_names.items():
        if not club.startswith("euf:"):
            continue
        parts = club.split(":", 2)
        if len(parts) == 3:
            index[(parts[1], team_name_key(name))].add(club)
    return index


def _existing_european_people(
    european: EuropeanInputs,
) -> tuple[
    dict[tuple[int, str, str], set[int]],
    dict[tuple[int, str, str], set[int]],
]:
    exact: dict[tuple[int, str, str], set[int]] = defaultdict(set)
    compact: dict[tuple[int, str, str], set[int]] = defaultdict(set)
    for player_id, season, division, _start in european.appearances:
        name = european.player_names.get(player_id)
        if not name:
            continue
        exact[(season, division, exact_name_key(name))].add(player_id)
        compact[(season, division, compact_name_key(name))].add(player_id)
    return exact, compact


def _one(values: set[Any] | None) -> Any | None:
    if values and len(values) == 1:
        return next(iter(values))
    return None


def load_international_inputs(
    usa_con: sqlite3.Connection,
    european: EuropeanInputs,
    euf_path: Path = EUF_DB,
) -> EuropeanInputs:
    out = EuropeanInputs()
    con = sqlite3.connect(f"file:{euf_path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        roster_rows = con.execute(
            """SELECT e.event_id,e.season,e.division,e.start_date,e.end_date,
                      d.event_code,et.event_team_id,et.display_name,
                      ct.country,r.number,r.name
               FROM roster_entries r
               JOIN event_teams et USING(event_team_id)
               JOIN events e USING(event_id)
               JOIN euf_event_details d USING(event_id)
               LEFT JOIN canonical_teams ct
                 ON ct.team_id=et.canonical_team_id
               WHERE d.source_owner=?
               ORDER BY e.start_date,e.event_id,et.event_team_id,r.name,r.number""",
            (WFDF_SOURCE,),
        ).fetchall()
        if not roster_rows:
            return out

        teams_by_event_name: dict[tuple[int, str], set[str]] = defaultdict(set)
        for row in roster_rows:
            teams_by_event_name[
                (int(row["event_id"]), compact_name_key(row["name"]))
            ].add(row["event_team_id"])
        colliding_compact = {
            compact
            for (_event_id, compact), teams in teams_by_event_name.items()
            if len(teams) > 1
        }

        source_names: dict[str, set[str]] = defaultdict(set)
        source_seasons: dict[str, set[int]] = defaultdict(set)
        for row in roster_rows:
            compact = compact_name_key(row["name"])
            if compact in colliding_compact:
                continue
            key = exact_name_key(row["name"])
            source_names[key].add(row["name"])
            source_seasons[key].add(int(row["season"]))
        usa_bridges, usa_audit = _usa_bridge_candidates(
            usa_con, source_names, source_seasons, colliding_compact
        )
        usa_names = {
            row["usau_player_id"]: row["usau_name"] for row in usa_audit
        }
        eu_exact, eu_compact = _existing_european_people(european)
        usa_clubs = _usa_club_index(usa_con)
        eu_clubs = _european_club_index(european)
        analogous_eu = {
            "club-men": "euf-open",
            "club-women": "euf-women",
            "club-mixed": "euf-mixed",
        }

        event_rows = con.execute(
            """SELECT e.event_id,e.name,e.season,e.division,e.start_date,e.end_date,
                      d.event_code,et.event_team_id,et.display_name,ct.country
               FROM event_teams et
               JOIN events e USING(event_id)
               JOIN euf_event_details d USING(event_id)
               LEFT JOIN canonical_teams ct
                 ON ct.team_id=et.canonical_team_id
               WHERE d.source_owner=?
               ORDER BY e.start_date,e.event_id,et.display_name""",
            (WFDF_SOURCE,),
        ).fetchall()
        source_event: dict[int, int] = {}
        source_team: dict[str, str] = {}
        team_club: dict[str, str] = {}
        team_display: dict[str, str] = {}
        for row in event_rows:
            event_id = -int(row["event_id"])
            event_team_id = f"wfdf:{row['event_team_id']}"
            source_event[int(row["event_id"])] = event_id
            source_team[row["event_team_id"]] = event_team_id
            out.event_team_event[event_team_id] = event_id
            out.event_info[event_id] = (
                row["name"], row["start_date"] or row["end_date"] or str(row["season"]),
                int(row["season"]), row["division"],
            )
            event_key = row["event_code"].split(":", 1)[0]
            spec = EVENTS.get(event_key)
            name = row["display_name"]
            key = team_name_key(name)
            club = None
            if spec is not None and spec.competition == "club":
                club = _one(usa_clubs.get((row["division"], key)))
                eu_division = analogous_eu.get(row["division"])
                if club is None and eu_division is not None:
                    club = _one(eu_clubs.get((eu_division, key)))
            if club is None:
                kind = "national" if spec and spec.competition == "national" else "club"
                club = (
                    f"wfdf-{kind}:{row['division']}:"
                    f"{team_name_key(row['country'])}:{key}"
                )
            team_club[row["event_team_id"]] = club
            team_display[row["event_team_id"]] = name
            out.clubs[event_team_id] = club
            prior = out.team_names.get(club)
            dated_name = (row["start_date"] or "", name)
            if prior is None or dated_name[0] >= prior[0]:
                out.team_names[club] = dated_name

        assignments: dict[tuple[str, str, str | None], tuple[int, str, str]] = {}
        appearances: set[tuple[int, int, str, str]] = set()
        latest_order: dict[int, tuple[int, str]] = {}
        for row in roster_rows:
            name_key = exact_name_key(row["name"])
            compact = compact_name_key(row["name"])
            season = int(row["season"])
            division = row["division"]
            match_method = "generated"
            player_id = None
            display_name = row["name"]
            if compact not in colliding_compact:
                player_id = usa_bridges.get(name_key)
                if player_id is not None:
                    match_method = "usau-name"
                    display_name = usa_names.get(player_id, display_name)
                else:
                    eu_division = analogous_eu.get(division)
                    if eu_division is not None:
                        candidates = eu_exact.get((season, eu_division, name_key))
                        player_id = _one(candidates)
                        if player_id is None:
                            player_id = _one(
                                eu_compact.get((season, eu_division, compact))
                            )
                        if player_id is not None:
                            match_method = "euf-name"
                            display_name = european.player_names.get(
                                player_id, display_name
                            )
            if player_id is None:
                if compact in colliding_compact:
                    source_key = (
                        f"{row['event_id']}|{row['event_team_id']}|"
                        f"{row['number'] or ''}|{name_key}"
                    )
                    match_method = "event-team-scoped"
                else:
                    source_key = compact
                player_id = international_player_id(source_key)
            assignment_key = (
                row["event_team_id"], row["name"], row["number"]
            )
            assignments[assignment_key] = (player_id, display_name, match_method)
            event_team_id = source_team[row["event_team_id"]]
            event_id = source_event[int(row["event_id"])]
            out.rosters.setdefault(event_team_id, []).append(player_id)
            out.player_names.setdefault(player_id, display_name)
            out.event_roster_rows.append(
                (event_id, event_team_id, player_id, display_name)
            )
            appearance = (
                player_id, season, division,
                row["start_date"] or row["end_date"] or str(season),
            )
            if appearance not in appearances:
                out.appearances.append(appearance)
                appearances.add(appearance)
            order = (season, row["start_date"] or "")
            if player_id not in latest_order or order >= latest_order[player_id]:
                latest_order[player_id] = order
                out.latest[player_id] = (
                    display_name, team_club[row["event_team_id"]], season
                )
            out.identity_rows.append({
                "event": row["event_code"].split(":", 1)[0],
                "team": team_display[row["event_team_id"]],
                "source_name": row["name"],
                "match_method": match_method,
                "player_id": player_id,
                "display_name": display_name,
            })

        for event_team_id, roster in out.rosters.items():
            out.rosters[event_team_id] = list(dict.fromkeys(roster))

        for row in con.execute(
            """SELECT g.event_id,g.game_key,g.date,g.time,g.home_id,g.away_id,
                      g.home_score,g.away_score,g.stage,e.season,e.division,
                      e.start_date,e.end_date
               FROM games g
               JOIN events e USING(event_id)
               JOIN euf_event_details d USING(event_id)
               WHERE d.source_owner=?
                 AND g.home_id IS NOT NULL AND g.away_id IS NOT NULL
                 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                 AND g.status IN ('played','has_outcome')
                 AND g.home_score + g.away_score >= 4""",
            (WFDF_SOURCE,),
        ):
            home = source_team.get(row["home_id"])
            away = source_team.get(row["away_id"])
            if home is None or away is None:
                raise ValueError(
                    f"WFDF game {row['event_id']}|{row['game_key']} has no team mapping"
                )
            event_id = source_event[int(row["event_id"])]
            effective = row["date"] or row["start_date"] or f"{row['season']}-01-01"
            out.games.append({
                "sort": (effective, row["time"] or "23:59", event_id, row["game_key"]),
                "date": effective,
                "season": int(row["season"]),
                "division": row["division"],
                "event_id": event_id,
                "game_key": f"wfdf:{row['game_key']}",
                "stage": row["stage"],
                "home_id": home,
                "away_id": away,
                "home_score": int(row["home_score"]),
                "away_score": int(row["away_score"]),
            })
            if out.rosters.get(home) and out.rosters.get(away):
                out.covered_scored_games += 1
            else:
                out.ghost_scored_games += 1
        out.games.sort(key=lambda game: game["sort"])
        return out
    finally:
        con.close()
