"""Adapt official WFDF event rosters and games to the Elo replay contract.

WFDF player IDs are scoped to one results installation, not globally stable.
The adapter therefore prefers conservative same-season USAU/EU name bridges,
then uses a deterministic international name ID. A name duplicated across two
teams at one championship is never bridged or merged automatically.
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from analysis.backtest import CLUB_SUFFIX, norm_club
from analysis.euf_overlap import exact_name_key
from analysis.euf_ratings import (
    EUF_DB,
    Appearance,
    EuropeanInputs,
    _usa_bridge_candidates,
    compact_name_key,
    team_name_key,
)
from scraper.wfdf import EVENTS, WFDF_SOURCE

WFDF_PLAYER_ALIASES = EUF_DB.parent / "wfdf_player_aliases.csv"


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


def _load_manual_usa_aliases(
    con: sqlite3.Connection,
    path: Path = WFDF_PLAYER_ALIASES,
) -> dict[str, tuple[int, str]]:
    """Load reviewed WFDF source-name -> unique USAU identity assignments."""
    if not path.exists():
        return {}
    usa: dict[str, list[tuple[int, str, bool]]] = defaultdict(list)
    for player_id, display_name, ambiguous in con.execute(
        "SELECT player_id,display_name,ambiguous FROM players"
    ):
        usa[exact_name_key(display_name)].append(
            (int(player_id), display_name, bool(ambiguous))
        )

    aliases: dict[str, tuple[int, str]] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            source_name = (row.get("source_name") or "").strip()
            usau_name = (row.get("usau_name") or "").strip()
            usau_team = (row.get("usau_team") or "").strip()
            if not source_name or not usau_name:
                raise ValueError(f"incomplete WFDF player alias in {path}")
            source_key = exact_name_key(source_name)
            candidates = usa.get(exact_name_key(usau_name), [])
            if usau_team and candidates:
                ids = [candidate[0] for candidate in candidates]
                placeholders = ",".join("?" for _ in ids)
                team_key = exact_name_key(usau_team)
                matched = {
                    int(player_id)
                    for player_id, team_name in con.execute(
                        f"""SELECT DISTINCT rp.player_id,t.display_name
                            FROM roster_players rp
                            JOIN event_teams t USING(event_team_id)
                            WHERE rp.player_id IN ({placeholders})""",
                        ids,
                    )
                    if exact_name_key(team_name) == team_key
                }
                candidates = [
                    candidate for candidate in candidates
                    if candidate[0] in matched
                ]
            if len(candidates) != 1 or (candidates[0][2] and not usau_team):
                raise ValueError(
                    f"WFDF player alias target is not one unambiguous USAU "
                    f"identity: {source_name!r} -> {usau_name!r}"
                )
            assignment = candidates[0][:2]
            prior = aliases.get(source_key)
            if prior is not None and prior != assignment:
                raise ValueError(
                    f"conflicting WFDF player aliases for {source_name!r}"
                )
            aliases[source_key] = assignment
    return aliases


def _usa_assignment(
    source_name: str,
    colliding_compact: set[str],
    manual_aliases: dict[str, tuple[int, str]],
    name_bridges: dict[str, int],
    usa_names: dict[int, str],
) -> tuple[int, str, str] | None:
    """Return a reviewed or automatic USAU assignment for one WFDF name."""
    if compact_name_key(source_name) in colliding_compact:
        return None
    name_key = exact_name_key(source_name)
    manual = manual_aliases.get(name_key)
    if manual is not None:
        return manual[0], manual[1], "usau-alias"
    player_id = name_bridges.get(name_key)
    if player_id is None:
        return None
    return player_id, usa_names.get(player_id, source_name), "usau-name"


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
        manual_usa_aliases = _load_manual_usa_aliases(usa_con)
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
        team_is_club: dict[str, bool] = {}
        team_publication: dict[str, tuple[int, str, str, str]] = {}
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
                is_national = bool(spec and spec.competition == "national")
                kind = "national" if is_national else "club"
                club = (
                    f"wfdf-{kind}:{row['division']}:"
                    f"{team_name_key(row['country'])}:{key}"
                )
            else:
                is_national = False
            team_club[row["event_team_id"]] = club
            team_display[row["event_team_id"]] = name
            team_is_club[row["event_team_id"]] = not is_national
            team_publication[row["event_team_id"]] = (
                int(row["season"]),
                row["division"],
                row["name"],
                row["start_date"] or row["end_date"] or str(row["season"]),
            )
            out.clubs[event_team_id] = club
            prior = out.team_names.get(club)
            dated_name = (row["start_date"] or "", name)
            if prior is None or dated_name[0] >= prior[0]:
                out.team_names[club] = dated_name

        assignments: dict[tuple[str, str, str | None], tuple[int, str, str]] = {}
        appearances: set[tuple[int, int, str, str]] = set()
        latest_order: dict[int, tuple[int, str]] = {}
        latest_club_order: dict[int, tuple[int, str]] = {}
        for row in roster_rows:
            name_key = exact_name_key(row["name"])
            compact = compact_name_key(row["name"])
            season = int(row["season"])
            division = row["division"]
            match_method = "generated"
            player_id = None
            display_name = row["name"]
            usa_assignment = _usa_assignment(
                row["name"], colliding_compact, manual_usa_aliases,
                usa_bridges, usa_names,
            )
            if usa_assignment is not None:
                player_id, display_name, match_method = usa_assignment
            elif compact not in colliding_compact:
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
            this_appearance = Appearance(
                display_name, team_display[row["event_team_id"]], season,
                row["start_date"] or "",
                team_is_club[row["event_team_id"]],
            )
            if player_id not in latest_order or order >= latest_order[player_id]:
                latest_order[player_id] = order
                out.latest[player_id] = this_appearance
            if team_is_club[row["event_team_id"]] and (
                player_id not in latest_club_order
                or order >= latest_club_order[player_id]
            ):
                latest_club_order[player_id] = order
                out.latest_club[player_id] = this_appearance
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
        for source_team_id, event_team_id in source_team.items():
            roster = out.rosters.get(event_team_id)
            if not roster or not team_is_club[source_team_id]:
                continue
            season, division, event_name, event_date = team_publication[
                source_team_id
            ]
            club = team_club[source_team_id]
            by_club, source, display = out.team_rosters.setdefault(
                (season, division), ({}, {}, {})
            )
            prior = source.get(club)
            if prior is not None and prior[1] > event_date:
                continue
            by_club[club] = roster
            source[club] = (event_name, event_date)
            display[club] = team_display[source_team_id]

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
