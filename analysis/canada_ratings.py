"""Adapt Canadian tournament rows to the player-Elo replay contract.

Canadian rows live in ``data/euf.db`` but are deliberately not part of the
European Ultimate Central loader: the Canadian source has event rosters and
stable player IDs rather than EUCS season-roster observations. Player IDs are
negative, deterministic, and source-scoped so they cannot collide with USAU,
EUF, WFDF, or UFA identities.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from analysis.euf_ratings import Appearance, EuropeanInputs, EUF_DB, team_name_key

SOURCE_PREFIX = "ultimate-canada:"
EVENT_OFFSET = 2_000_000_000


def canadian_player_id(source_id: str) -> int:
    digest = hashlib.sha256(
        f"ultimate-canada-player\0{source_id}".encode()
    ).hexdigest()
    return -int(digest[:13], 16)


def _event_id(local_id: int) -> int:
    return -EVENT_OFFSET - int(local_id)


def _game_date(date: str | None, fallback: str | None, season: int) -> str:
    return (date or fallback or f"{season}-06-01")[:10]


def load_canadian_inputs(euf_path: Path = EUF_DB) -> EuropeanInputs:
    """Load Canadian games, teams, rosters, and event metadata."""
    out = EuropeanInputs()
    con = sqlite3.connect(f"file:{euf_path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        events = con.execute(
            """SELECT e.event_id,e.name,e.season,e.division,e.start_date,e.end_date
               FROM events e JOIN euf_event_details d USING(event_id)
               WHERE d.source_owner LIKE 'ultimate-canada:%'"""
        ).fetchall()
        if not events:
            return out
        event_ids = {int(row["event_id"]): row for row in events}
        et_rows = con.execute(
            """SELECT et.event_team_id,et.event_id,et.display_name,et.full_name
               FROM event_teams et JOIN events e USING(event_id)
               JOIN euf_event_details d USING(event_id)
               WHERE d.source_owner LIKE 'ultimate-canada:%'"""
        ).fetchall()
        team_by_id = {row["event_team_id"]: row for row in et_rows}
        external_team = {
            row["event_team_id"]: f"canada:{row['event_team_id']}"
            for row in et_rows
        }
        for row in events:
            eid = _event_id(row["event_id"])
            division = row["division"]
            out.event_info[eid] = (
                row["name"], row["start_date"] or row["end_date"] or str(row["season"]),
                int(row["season"]), division,
            )

        # The normalized roster table intentionally keeps the common schema.
        # Resolve a source person through its preserved source mapping and
        # display name; ambiguous duplicate names receive a team/number key.
        people = defaultdict(list)
        for source_id, display_name in con.execute(
            """SELECT sx.source_id,cp.display_name
               FROM source_entities sx JOIN canonical_people cp
                 ON cp.person_id=sx.local_key
               WHERE sx.source LIKE 'ultimate-canada:%'
                 AND sx.entity_type='person'"""
        ):
            people[str(display_name).casefold()].append(str(source_id))

        rosters_by_team: dict[str, list[int]] = defaultdict(list)
        roster_people: dict[str, list[tuple[int, str]]] = defaultdict(list)
        roster_rows = con.execute(
            """SELECT re.event_team_id,re.number,re.name
               FROM roster_entries re JOIN event_teams et
                 ON et.event_team_id=re.event_team_id
               JOIN euf_event_details d ON d.event_id=et.event_id
               WHERE d.source_owner LIKE 'ultimate-canada:%'"""
        ).fetchall()
        for row in roster_rows:
            name = str(row["name"] or "").strip()
            if not name:
                continue
            matches = people.get(name.casefold(), [])
            source_id = matches[0] if len(matches) == 1 else (
                f"{row['event_team_id']}|{row['number'] or ''}|{name}"
            )
            pid = canadian_player_id(source_id)
            etid = external_team[row["event_team_id"]]
            if pid not in rosters_by_team[etid]:
                rosters_by_team[etid].append(pid)
                roster_people[etid].append((pid, name))
            event = event_ids[int(team_by_id[row["event_team_id"]]["event_id"])]
            source_event_id = _event_id(event["event_id"])
            out.event_roster_rows.append((source_event_id, etid, pid, name))
            out.player_names[pid] = name
            appearance = (pid, int(event["season"]), event["division"],
                          str(event["start_date"] or event["season"]))
            if appearance not in out.appearances:
                out.appearances.append(appearance)
            team = team_by_id[row["event_team_id"]]
            value = Appearance(name, team["full_name"] or team["display_name"],
                               int(event["season"]), event["start_date"] or "")
            if pid not in out.latest or value.order >= out.latest[pid].order:
                out.latest[pid] = value
                out.latest_club[pid] = value

        for row in et_rows:
            event = event_ids[int(row["event_id"])]
            eid = _event_id(row["event_id"])
            etid = external_team[row["event_team_id"]]
            division = event["division"]
            team_name = row["full_name"] or row["display_name"]
            club_key = f"canada:{division}:{team_name_key(team_name)}"
            out.rosters[etid] = rosters_by_team.get(etid, [])
            out.clubs[etid] = club_key
            out.event_team_event[etid] = eid
            out.team_names[club_key] = (event["start_date"] or "", team_name)
            by_club, sources, display = out.team_rosters.setdefault(
                (int(event["season"]), division), ({}, {}, {})
            )
            by_club[club_key] = out.rosters[etid]
            sources[club_key] = (event["name"], event["start_date"] or "")
            display[club_key] = team_name

        for row in con.execute(
            """SELECT g.event_id,g.game_key,g.date,g.time,g.stage,
                      g.home_id,g.away_id,g.home_score,g.away_score,g.status,
                      e.season,e.start_date,e.end_date,e.division
               FROM games g JOIN events e USING(event_id)
               JOIN euf_event_details d USING(event_id)
               WHERE d.source_owner LIKE 'ultimate-canada:%'
                 AND g.home_id IS NOT NULL AND g.away_id IS NOT NULL
                 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                 AND NOT (g.home_score = 0 AND g.away_score = 0)
                 AND (g.status IS NULL OR g.status IN ('', 'Final', 'played', 'has_outcome'))
                 AND g.home_score + g.away_score >= 4"""
        ):
            if row["home_id"] not in external_team or row["away_id"] not in external_team:
                continue
            eid = _event_id(row["event_id"])
            home = external_team[row["home_id"]]
            away = external_team[row["away_id"]]
            date = _game_date(row["date"], row["start_date"], int(row["season"]))
            game_key = f"canada:{row['event_id']}:{row['game_key']}"
            out.games.append({
                "event_id": eid,
                "game_key": game_key,
                "date": date,
                "sort": (date, row["time"] or "12:00", eid, game_key),
                "season": int(row["season"]),
                "division": row["division"],
                "stage": row["stage"] or "",
                "home_id": home,
                "away_id": away,
                "home_score": int(row["home_score"]),
                "away_score": int(row["away_score"]),
            })
            out.covered_scored_games += 1
        out.games.sort(key=lambda game: game["sort"])
    finally:
        con.close()
    return out
