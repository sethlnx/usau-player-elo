"""Adapt scored PUL and WUL records to the shared Elo replay contract."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from analysis.euf_overlap import exact_name_key
from analysis.euf_ratings import Appearance, EuropeanInputs, compact_name_key


_JERSEY = re.compile(r"^\d{1,3}\s+")


def _stable_id(kind: str, value: str) -> int:
    digest = hashlib.sha256(f"womens-pro\0{kind}\0{value}".encode()).hexdigest()
    return -int(digest[:13], 16)


def _date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for fmt in ("%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"unsupported women's pro game date: {text!r}")


def _player_name(value: Any) -> str:
    return _JERSEY.sub("", str(value or "").strip())


def _rows(con: sqlite3.Connection, league: str, dataset: str) -> list[dict[str, Any]]:
    try:
        values = con.execute(
            """SELECT season,record_key,payload_json
               FROM womens_pro_records
               WHERE league=? AND dataset=?
               ORDER BY season,record_key""",
            (league, dataset),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(json.loads(payload), _source_season=int(season),
                 _record_key=str(record_key))
            for season, record_key, payload in values]


def _usau_bridges(
    con: sqlite3.Connection,
    names: dict[str, set[str]],
    seasons: dict[str, set[int]],
    colliding: set[str],
) -> dict[str, int]:
    """Conservatively reuse a unique, same-season USAU identity."""
    usa: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    usa_seasons: dict[int, set[int]] = defaultdict(set)
    try:
        for player_id, name, ambiguous in con.execute(
            "SELECT player_id,display_name,ambiguous FROM players"
        ):
            usa[compact_name_key(name)].append((int(player_id), bool(ambiguous)))
        for player_id, season in con.execute(
            """SELECT DISTINCT rp.player_id,e.season
               FROM roster_players rp
               JOIN event_teams et USING(event_team_id)
               JOIN events e USING(event_id)"""
        ):
            usa_seasons[int(player_id)].add(int(season))
    except sqlite3.OperationalError:
        return {}

    bridges: dict[str, int] = {}
    for name_key in names:
        compact = compact_name_key(name_key)
        candidates = usa.get(compact, [])
        if compact in colliding or len(candidates) != 1:
            continue
        player_id, ambiguous = candidates[0]
        if not ambiguous and seasons[name_key] & usa_seasons[player_id]:
            bridges[name_key] = player_id
    return bridges


def _add_game(
    out: EuropeanInputs,
    *,
    league: str,
    season: int,
    date: str,
    home_name: str,
    away_name: str,
    home_score: int,
    away_score: int,
    game_key: str,
    rosters: dict[tuple[int, str, str, str], list[int]],
    roster_names: dict[int, str],
) -> None:
    division = league.lower()
    event_id = _stable_id("event", f"{league}:{season}")
    out.event_info.setdefault(
        event_id, (f"{league} {season}", date, season, division)
    )

    team_ids = []
    for team_name, opponent_name in ((home_name, away_name), (away_name, home_name)):
        team_key = compact_name_key(team_name)
        opponent_key = compact_name_key(opponent_name)
        club_key = f"{division}:{team_key}"
        event_team_id = (
            f"wp:{division}:{season}:{date}:{team_key}:{opponent_key}"
        )
        roster = rosters.get((season, date, team_key, opponent_key), [])
        if not roster:
            roster = [f"ghost:{club_key}:{season}"]
        out.rosters[event_team_id] = roster
        out.clubs[event_team_id] = club_key
        out.event_team_event[event_team_id] = event_id
        out.team_names[club_key] = (date, team_name)
        team_ids.append(event_team_id)

        for player_id in roster:
            if str(player_id).startswith("ghost:"):
                continue
            player_name = roster_names[player_id]
            out.event_roster_rows.append(
                (event_id, event_team_id, player_id, player_name)
            )

        by_season = out.team_rosters.setdefault(
            (season, division), ({}, {}, {})
        )
        previous = by_season[1].get(club_key, ("", ""))[1]
        if date >= previous:
            by_season[0][club_key] = roster
            by_season[1][club_key] = (f"{league} {season}", date)
            by_season[2][club_key] = team_name

    out.games.append({
        "sort": (date, "23:59", event_id, game_key),
        "date": date,
        "season": season,
        "division": division,
        "event_id": event_id,
        "game_key": f"wp:{division}:{game_key}",
        "stage": "league",
        "home_id": team_ids[0],
        "away_id": team_ids[1],
        "home_score": home_score,
        "away_score": away_score,
    })
    out.covered_scored_games += 1


def load_womens_pro_inputs(con: sqlite3.Connection) -> EuropeanInputs:
    """Load every scored PUL/WUL game; WUL rows also supply game rosters.

    PUL's first-party manifest currently publishes scores and team aggregates,
    but no player roster endpoint. Its team-season sides therefore use the
    replay's explicit ``ghost:`` identity: PUL games affect PUL team Elo without
    inventing player identities. WUL's player-by-game export supplies real
    rosters and can update bridged USAU players or stable WUL identities.
    """
    out = EuropeanInputs()

    player_rows = _rows(con, "WUL", "player-standard-game")
    source_names: dict[str, set[str]] = defaultdict(set)
    source_seasons: dict[str, set[int]] = defaultdict(set)
    teams_by_compact_season: dict[tuple[str, int], set[str]] = defaultdict(set)
    parsed_players = []
    for row in player_rows:
        player_name = _player_name(row.get("Player"))
        team_name = str(row.get("Team") or "").strip()
        opponent_name = str(row.get("Opponent") or "").strip()
        date = _date(row.get("Date"))
        if not player_name or not team_name or not opponent_name or not date:
            continue
        season = int(row["_source_season"])
        name_key = exact_name_key(player_name)
        compact = compact_name_key(name_key)
        source_names[name_key].add(player_name)
        source_seasons[name_key].add(season)
        teams_by_compact_season[(compact, season)].add(compact_name_key(team_name))
        parsed_players.append(
            (season, date, team_name, opponent_name, player_name, name_key, compact)
        )

    colliding = {
        compact for (compact, _season), teams in teams_by_compact_season.items()
        if len(teams) > 1
    }
    bridges = _usau_bridges(con, source_names, source_seasons, colliding)
    roster_sets: dict[tuple[int, str, str, str], set[int]] = defaultdict(set)
    roster_names: dict[int, str] = {}
    appearance_seen: set[tuple[int, int, str, str]] = set()
    for season, date, team_name, opponent_name, player_name, name_key, compact in parsed_players:
        team_key = compact_name_key(team_name)
        opponent_key = compact_name_key(opponent_name)
        source_key = f"{compact}|{team_key}" if compact in colliding else compact
        player_id = bridges.get(name_key, _stable_id("wul-player", source_key))
        roster_sets[(season, date, team_key, opponent_key)].add(player_id)
        roster_names.setdefault(player_id, player_name)
        out.player_names.setdefault(player_id, player_name)
        club_key = f"wul:{team_key}"
        appearance = Appearance(player_name, team_name, season, date, True)
        if player_id not in out.latest or appearance.order >= out.latest[player_id].order:
            out.latest[player_id] = appearance
            out.latest_club[player_id] = appearance
        sighting = (player_id, season, "wul", date)
        if sighting not in appearance_seen:
            out.appearances.append(sighting)
            appearance_seen.add(sighting)

    rosters = {key: sorted(values, key=str) for key, values in roster_sets.items()}

    # WUL exports one team-level row per side. Keep one canonical side and
    # require its mirrored row, when present, to agree on the final score.
    wul_games: dict[tuple[int, str, tuple[str, str]], dict[str, Any]] = {}
    for row in _rows(con, "WUL", "team-standard-game"):
        season = int(row["_source_season"])
        date = _date(row.get("Date"))
        team = str(row.get("Team") or "").strip()
        opponent = str(row.get("Opponent") or "").strip()
        if not date or not team or not opponent:
            continue
        score, against = int(float(row["G"])), int(float(row["GA"]))
        ordered = tuple(sorted((team, opponent), key=lambda value: (value.casefold(), value)))
        key = (season, date, ordered)
        home, away = ordered
        candidate = (score, against) if team == home else (against, score)
        previous = wul_games.get(key)
        if previous and (previous["home_score"], previous["away_score"]) != candidate:
            raise ValueError(f"conflicting WUL score rows for {key!r}")
        wul_games[key] = {
            "home": home, "away": away,
            "home_score": candidate[0], "away_score": candidate[1],
        }

    for (season, date, ordered), game in sorted(wul_games.items()):
        game_key = f"{season}:{date}:{compact_name_key(ordered[0])}:{compact_name_key(ordered[1])}"
        _add_game(
            out, league="WUL", season=season, date=date,
            home_name=game["home"], away_name=game["away"],
            home_score=game["home_score"], away_score=game["away_score"],
            game_key=game_key, rosters=rosters, roster_names=roster_names,
        )

    # The PUL ``games`` endpoint is the canonical scored result source. The
    # parallel schedule endpoint repeats the same games and is intentionally
    # not replayed.
    for row in _rows(con, "PUL", "games"):
        if row.get("homeScore") in (None, "") or row.get("awayScore") in (None, ""):
            continue
        season = int(row["_source_season"])
        date = _date(row.get("date"))
        home = str(row.get("homeName") or row.get("homeAbbrev") or "").strip()
        away = str(row.get("awayName") or row.get("awayAbbrev") or "").strip()
        if not date or not home or not away:
            continue
        game_key = (
            f"{season}:{date}:{row.get('homeAbbrev') or compact_name_key(home)}:"
            f"{row.get('awayAbbrev') or compact_name_key(away)}:{row['_record_key']}"
        )
        _add_game(
            out, league="PUL", season=season, date=date,
            home_name=home, away_name=away,
            home_score=int(row["homeScore"]), away_score=int(row["awayScore"]),
            game_key=game_key, rosters={}, roster_names={},
        )

    out.ghost_scored_games = sum(
        not out.rosters.get(side)
        or all(str(player).startswith("ghost:") for player in out.rosters[side])
        for game in out.games for side in (game["home_id"], game["away_id"])
    )
    out.games.sort(key=lambda game: game["sort"])
    return out
