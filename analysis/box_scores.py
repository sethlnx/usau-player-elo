"""Build versioned player-event box scores from USAU roster statistics.

The output keeps source totals and missing-field provenance explicit. E± is a
G/A/B/T proxy for a yardage-aware target. Complete events feed the rating
transfer only after their end date; partial events never receive an E± value.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "usau.db"
DEFAULT_REFERENCE = ROOT / "data" / "box_score_reference.json"
DEFAULT_OUTPUT = ROOT / "data" / "player_box_scores.csv"
STAT_FIELDS = ("goals", "assists", "blocks", "turnovers")
SOURCE_COLUMNS = {
    "goals": "points",
    "assists": "assists",
    "blocks": "ds",
    "turnovers": "turns",
}
OUTPUT_FIELDS = (
    "player_id", "player", "event_id", "event_team_id", "event", "team",
    "season", "division", "event_end_date", "team_games", "goals",
    "assists", "blocks", "turnovers", "plus_minus", "edge_proxy",
    "edge_proxy_per_team_game", "scoring_efficiency", "coverage_flags",
    "model_version",
)


def load_reference(path: Path = DEFAULT_REFERENCE) -> dict:
    reference = json.loads(path.read_text())
    coefficients = reference.get("coefficients", {})
    missing = set(STAT_FIELDS) - set(coefficients)
    if missing:
        raise ValueError(f"box-score reference lacks coefficients: {sorted(missing)}")
    if not reference.get("model_version"):
        raise ValueError("box-score reference lacks model_version")
    return reference


def _number(value: str | int | float | None) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def _played_team_games(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute("""
        SELECT event_team_id, COUNT(*)
        FROM (
            SELECT home_id AS event_team_id
            FROM games
            WHERE home_id IS NOT NULL AND away_id IS NOT NULL
              AND home_score IS NOT NULL AND away_score IS NOT NULL
              AND NOT (home_score = 0 AND away_score = 0)
              AND (status IS NULL OR status IN ('', 'Final'))
              AND home_score + away_score >= 4
            UNION ALL
            SELECT away_id AS event_team_id
            FROM games
            WHERE home_id IS NOT NULL AND away_id IS NOT NULL
              AND home_score IS NOT NULL AND away_score IS NOT NULL
              AND NOT (home_score = 0 AND away_score = 0)
              AND (status IS NULL OR status IN ('', 'Final'))
              AND home_score + away_score >= 4
        )
        GROUP BY event_team_id
    """)
    return {str(event_team_id): int(games) for event_team_id, games in rows}


def load_source_rows(
    con: sqlite3.Connection, season: int | None = None,
) -> list[dict]:
    """Load linked roster lines from events carrying any player statistics."""
    columns = ", ".join(f"re.{column}" for column in SOURCE_COLUMNS.values())
    season_filter = "AND ev.season = ?" if season is not None else ""
    parameters = (season,) if season is not None else ()
    rows = con.execute(f"""
        WITH observed_events AS (
            SELECT DISTINCT observed_team.event_id
            FROM roster_entries observed
            JOIN event_teams observed_team USING(event_team_id)
            WHERE observed.points != '' OR observed.assists != ''
               OR observed.ds != '' OR observed.turns != ''
        )
        SELECT rp.player_id, re.name, re.event_team_id, et.event_id,
               ev.name, et.display_name, ev.season, ev.division, ev.end_date,
               {columns}
        FROM observed_events
        JOIN event_teams et USING(event_id)
        JOIN roster_entries re USING(event_team_id)
        JOIN roster_players rp
          ON rp.event_team_id = re.event_team_id AND rp.name = re.name
        JOIN events ev USING(event_id)
        WHERE 1 = 1 {season_filter}
        ORDER BY ev.end_date, et.event_id, re.event_team_id, re.name
    """, parameters).fetchall()
    source_names = tuple(SOURCE_COLUMNS.values())
    output = []
    for raw in rows:
        row = {
            "player_id": str(raw[0]), "player": raw[1],
            "event_team_id": str(raw[2]), "event_id": int(raw[3]),
            "event": raw[4], "team": raw[5] or "", "season": int(raw[6]),
            "division": raw[7], "event_end_date": raw[8] or "",
        }
        row.update(zip(source_names, raw[9:]))
        output.append(row)
    return output


def build_event_rows(
    con: sqlite3.Connection, reference: dict, season: int | None = None,
) -> list[dict]:
    """Return one provenance-rich box-score row per linked player-event team."""
    source_rows = load_source_rows(con, season)
    games_by_team = _played_team_games(con)
    coverage: dict[int, dict[str, bool]] = defaultdict(
        lambda: {field: False for field in STAT_FIELDS}
    )
    event_totals: dict[int, dict[str, int]] = defaultdict(
        lambda: {field: 0 for field in STAT_FIELDS}
    )
    for row in source_rows:
        event_id = row["event_id"]
        for field, column in SOURCE_COLUMNS.items():
            observed = row.get(column) not in (None, "")
            coverage[event_id][field] |= observed
            if observed:
                event_totals[event_id][field] += _number(row[column])

    coefficients = reference["coefficients"]
    output = []
    for source in source_rows:
        if season is not None and source["season"] != season:
            continue
        event_id = source["event_id"]
        available = coverage[event_id]
        missing = [field for field in STAT_FIELDS if not available[field]]
        stats = {
            field: _number(source[column]) if available[field] else ""
            for field, column in SOURCE_COLUMNS.items()
        }
        complete = not missing
        plus_minus = edge_proxy = per_game = scoring_efficiency = ""
        if complete:
            plus_minus = (
                stats["goals"] + stats["assists"] + stats["blocks"]
                - stats["turnovers"]
            )
            edge_proxy = sum(
                coefficients[field] * stats[field] for field in STAT_FIELDS
            )
            team_games = games_by_team.get(source["event_team_id"], 0)
            if team_games:
                per_game = edge_proxy / team_games
            totals = event_totals[event_id]
            denominator = totals["goals"] + totals["turnovers"]
            if denominator:
                scoring_efficiency = totals["goals"] / denominator
        else:
            team_games = games_by_team.get(source["event_team_id"], 0)

        flags = "gabt-complete" if complete else "missing:" + ",".join(missing)
        output.append({
            "player_id": source["player_id"], "player": source["player"],
            "event_id": event_id, "event_team_id": source["event_team_id"],
            "event": source["event"], "team": source["team"],
            "season": source["season"], "division": source["division"],
            "event_end_date": source["event_end_date"], "team_games": team_games,
            **stats, "plus_minus": plus_minus,
            "edge_proxy": round(edge_proxy, 4) if complete else "",
            "edge_proxy_per_team_game": round(per_game, 4) if per_game != "" else "",
            "scoring_efficiency": (
                round(scoring_efficiency, 4) if scoring_efficiency != "" else ""
            ),
            "coverage_flags": flags,
            "model_version": reference["model_version"],
        })
    return output


def aggregate_player_seasons(rows: list[dict]) -> list[dict]:
    """Aggregate complete event rows; partial events never become zeroes."""
    grouped = {}
    for row in rows:
        if row["coverage_flags"] != "gabt-complete":
            continue
        key = (str(row["player_id"]), int(row["season"]))
        aggregate = grouped.setdefault(key, {
            "player_id": str(row["player_id"]), "season": int(row["season"]),
            "goals": 0, "assists": 0, "blocks": 0, "turnovers": 0,
            "plus_minus": 0, "edge_proxy": 0.0, "team_games": 0,
            "events": 0, "stats_through": "", "model_version": row["model_version"],
        })
        for field in (*STAT_FIELDS, "plus_minus", "team_games"):
            aggregate[field] += row[field]
        aggregate["edge_proxy"] += float(row["edge_proxy"])
        aggregate["events"] += 1
        aggregate["stats_through"] = max(
            aggregate["stats_through"], row["event_end_date"]
        )
    output = []
    for aggregate in grouped.values():
        aggregate["edge_proxy"] = round(aggregate["edge_proxy"], 2)
        aggregate["edge_proxy_per_team_game"] = (
            round(aggregate["edge_proxy"] / aggregate["team_games"], 2)
            if aggregate["team_games"] else ""
        )
        output.append(aggregate)
    output.sort(key=lambda row: (row["player_id"], row["season"]))
    return output


def write_event_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--season", type=int)
    args = parser.parse_args()
    reference = load_reference(args.reference)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = build_event_rows(con, reference, args.season)
    con.close()
    write_event_rows(args.output, rows)
    complete = sum(row["coverage_flags"] == "gabt-complete" for row in rows)
    print(f"wrote {args.output} ({len(rows):,} rows; {complete:,} complete GABT)")


if __name__ == "__main__":
    main()
