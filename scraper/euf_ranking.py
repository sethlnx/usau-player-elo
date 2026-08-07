"""Import captured EUCS Ranking roster observations into the European DB.

The ranking site is an R/Shiny application, not a stable public API. This module
only consumes a captured snapshot; it never treats transient session URLs as a
reusable upstream contract.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from .euf import EUF_DB, _upsert_canonical_team, init_db, observe

SOURCE = "eucs-ranking"
SOURCE_URL = "https://ranking.ultimatefederation.eu/"
DIVISIONS = {
    "mixed": "euf-mixed",
    "open": "euf-open",
    "women": "euf-women",
    "euf-mixed": "euf-mixed",
    "euf-open": "euf-open",
    "euf-women": "euf-women",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def name_key(name: str) -> str:
    """Conservative key for grouping case/spacing variants, not an identity ID."""
    value = unicodedata.normalize("NFKC", name)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _display_name(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        raise ValueError(f"{field} cannot be empty")
    return cleaned


def _prepare_snapshot(snapshot: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if snapshot.get("source") != SOURCE:
        raise ValueError(f"snapshot source must be {SOURCE!r}")
    observed_at = snapshot.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        raise ValueError("snapshot observed_at is required")
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise ValueError("snapshot rows must be a list")

    prepared = []
    seen_rosters: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"rows[{index}] must be an object")
        try:
            season = int(row["season"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"rows[{index}].season must be an integer") from exc
        if not 1900 <= season <= 2100:
            raise ValueError(f"rows[{index}].season is outside 1900..2100")
        raw_division = _display_name(row.get("division"), f"rows[{index}].division")
        division = DIVISIONS.get(raw_division.casefold())
        if division is None:
            raise ValueError(f"rows[{index}].division is unsupported: {raw_division!r}")
        team_name = _display_name(row.get("team"), f"rows[{index}].team")
        team_source_id = f"{division}|{team_name}"
        roster_id = f"{season}|{team_source_id}"
        if roster_id in seen_rosters:
            raise ValueError(f"duplicate ranking roster {roster_id}")
        seen_rosters.add(roster_id)

        source_url = row.get("url") or snapshot.get("source_url") or SOURCE_URL
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise ValueError(f"rows[{index}].url must be an HTTPS URL")
        payload_hash = row.get("payload_hash")
        if not isinstance(payload_hash, str) or not _HASH_RE.fullmatch(payload_hash):
            raise ValueError(f"rows[{index}].payload_hash must be a SHA-256 hex digest")
        players = row.get("players")
        if not isinstance(players, list):
            raise ValueError(f"rows[{index}].players must be a list")
        player_names = [
            _display_name(player, f"rows[{index}].players[{ordinal}]")
            for ordinal, player in enumerate(players)
        ]
        prepared.append({
            "season": season,
            "division": division,
            "team_name": team_name,
            "team_source_id": team_source_id,
            "roster_id": roster_id,
            "source_url": source_url,
            "payload_hash": payload_hash,
            "players": player_names,
        })
    return observed_at, prepared


def ingest_snapshot(
    con: sqlite3.Connection, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Transactionally replace normalized ranking rosters from one snapshot."""
    observed_at, rows = _prepare_snapshot(snapshot)
    old_team_ids = {
        row[0] for row in con.execute(
            "SELECT local_key FROM source_entities "
            "WHERE source=? AND entity_type='team'",
            (SOURCE,),
        )
    }
    active_team_ids: set[str] = set()
    display_names: set[str] = set()
    keys: set[str] = set()

    with con:
        con.execute("DELETE FROM ranking_roster_observations WHERE source=?", (SOURCE,))
        con.execute(
            "DELETE FROM source_entities WHERE source=? AND entity_type='team'",
            (SOURCE,),
        )
        for row in sorted(
            rows,
            key=lambda item: (item["season"], item["division"], item["team_name"]),
        ):
            team_id = _upsert_canonical_team(
                con,
                SOURCE,
                row["team_source_id"],
                row["team_name"],
                row["source_url"],
                observed_at,
                row["payload_hash"],
            )
            active_team_ids.add(team_id)
            con.execute(
                """INSERT INTO ranking_roster_observations
                   (source,roster_id,season,division,team_id,team_name,source_url,
                    observed_at,payload_hash,record_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    SOURCE,
                    row["roster_id"],
                    row["season"],
                    row["division"],
                    team_id,
                    row["team_name"],
                    row["source_url"],
                    observed_at,
                    row["payload_hash"],
                    len(row["players"]),
                ),
            )
            con.executemany(
                """INSERT INTO ranking_roster_entries
                   (source,roster_id,ordinal,player_name,name_key)
                   VALUES (?,?,?,?,?)""",
                [
                    (SOURCE, row["roster_id"], ordinal, player, name_key(player))
                    for ordinal, player in enumerate(row["players"])
                ],
            )
            observe(
                con,
                SOURCE,
                "roster",
                row["roster_id"],
                source_url=row["source_url"],
                observed_at=observed_at,
                payload_hash=row["payload_hash"],
                state="ok" if row["players"] else "empty",
                count=len(row["players"]),
            )
            display_names.update(row["players"])
            keys.update(name_key(player) for player in row["players"])

        for team_id in old_team_ids - active_team_ids:
            con.execute(
                """DELETE FROM canonical_teams WHERE team_id=?
                   AND NOT EXISTS (
                     SELECT 1 FROM event_teams WHERE canonical_team_id=canonical_teams.team_id
                   ) AND NOT EXISTS (
                     SELECT 1 FROM source_entities WHERE local_key=canonical_teams.team_id
                   ) AND NOT EXISTS (
                     SELECT 1 FROM ranking_roster_observations
                     WHERE team_id=canonical_teams.team_id
                   )""",
                (team_id,),
            )

    return {
        "source": SOURCE,
        "rosters": len(rows),
        "empty_rosters": sum(not row["players"] for row in rows),
        "memberships": sum(len(row["players"]) for row in rows),
        "display_names": len(display_names),
        "name_keys": len(keys),
        "teams": len(active_team_ids),
        "seasons": sorted({row["season"] for row in rows}),
        "divisions": sorted({row["division"] for row in rows}),
    }


def load_snapshot(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("ranking snapshot must be a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a captured EUCS Ranking roster snapshot"
    )
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--db", type=Path, default=EUF_DB)
    args = parser.parse_args()
    con = init_db(args.db)
    try:
        summary = ingest_snapshot(con, load_snapshot(args.snapshot))
    finally:
        con.close()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
