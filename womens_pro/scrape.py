"""Ingest every first-party PUL dataset and lossless WUL dashboard exports.

Usage:
    python -m womens_pro.scrape pul [--refresh]
    python -m womens_pro.scrape wul YEAR DATASET export.csv

PUL publishes a machine-readable manifest, so the importer follows every
endpoint it enumerates. WUL publishes its statistics through a Shiny dashboard,
not a stable API; export any dashboard table as CSV and identify the table with
a dataset slug such as ``player-standard-game`` or ``team-advanced-season``.
Every source field is retained in ``payload_json``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import requests

from scraper.build_db import DB_PATH, connect
from . import api

SCHEMA = """
CREATE TABLE IF NOT EXISTS womens_pro_sources (
    league TEXT NOT NULL,
    dataset TEXT NOT NULL,
    source_season INTEGER NOT NULL,
    schema_version TEXT,
    generated_at TEXT,
    source_url TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    PRIMARY KEY (league, dataset, source_season)
);
CREATE TABLE IF NOT EXISTS womens_pro_records (
    league TEXT NOT NULL,
    dataset TEXT NOT NULL,
    season INTEGER NOT NULL,
    record_key TEXT NOT NULL,
    team TEXT,
    player TEXT,
    opponent TEXT,
    game_date TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (league, dataset, season, record_key)
);
CREATE INDEX IF NOT EXISTS womens_pro_records_player
    ON womens_pro_records (league, player, season);
CREATE INDEX IF NOT EXISTS womens_pro_records_team
    ON womens_pro_records (league, team, season);
CREATE INDEX IF NOT EXISTS womens_pro_records_date
    ON womens_pro_records (league, game_date);
"""

_DATASET_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _columns(rows: list[dict]) -> list[str]:
    return sorted({str(key) for row in rows for key in row})


def _value(row: dict, *names: str):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _season(row: dict, source_season: int) -> int:
    value = _value(row, "season", "Season", "year", "Year")
    if value is None:
        return source_season
    match = re.search(r"\b(20\d{2})\b", str(value))
    if not match:
        if source_season:
            return source_season
        raise ValueError(f"record has an invalid season: {value!r}")
    return int(match.group())


def _pul_key(dataset: str, row: dict) -> str:
    if dataset in {"teams", "team-stats", "standings"}:
        value = _value(row, "abbrev", "name")
        if value is not None:
            return str(value)
    if dataset in {"games", "schedule"}:
        parts = [_value(row, "date"), _value(row, "awayAbbrev", "awayName"),
                 _value(row, "homeAbbrev", "homeName"), _value(row, "week")]
        if all(value is not None for value in parts[:3]):
            return "|".join("" if value is None else str(value) for value in parts)
    return hashlib.sha256(_json(row).encode()).hexdigest()[:24]


def _validate_payload(payload: dict, url: str) -> list[dict]:
    if payload.get("league") != "PUL":
        raise ValueError(f"expected league PUL in {url}")
    rows = payload.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"expected a data list of objects in {url}")
    return rows


def _replace_dataset(
    con: sqlite3.Connection,
    *,
    league: str,
    dataset: str,
    source_season: int,
    rows: list[dict],
    schema_version: str | None,
    generated_at: str | None,
    source_url: str,
    key_function,
) -> int:
    if not _DATASET_SLUG.fullmatch(dataset):
        raise ValueError(f"invalid dataset slug: {dataset!r}")

    if source_season:
        con.execute(
            "DELETE FROM womens_pro_records WHERE league=? AND dataset=? AND season=?",
            (league, dataset, source_season),
        )
    else:
        con.execute(
            "DELETE FROM womens_pro_records WHERE league=? AND dataset=?",
            (league, dataset),
        )

    for index, row in enumerate(rows, 1):
        season = _season(row, source_season)
        key = str(key_function(dataset, row, index))
        con.execute(
            "INSERT INTO womens_pro_records VALUES (?,?,?,?,?,?,?,?,?)",
            (
                league,
                dataset,
                season,
                key,
                _value(row, "team", "Team", "name"),
                _value(row, "player", "Player"),
                _value(row, "opponent", "Opponent"),
                _value(row, "date", "Date"),
                _json(row),
            ),
        )

    con.execute(
        "INSERT OR REPLACE INTO womens_pro_sources VALUES (?,?,?,?,?,?,?,?)",
        (
            league,
            dataset,
            source_season,
            schema_version,
            generated_at,
            source_url,
            _json(_columns(rows)),
            len(rows),
        ),
    )
    return len(rows)


def _manifest_endpoints(manifest: dict) -> list[tuple[str, int, str]]:
    if manifest.get("league") != "PUL" or not isinstance(manifest.get("data"), dict):
        raise ValueError("invalid PUL manifest")
    data = manifest["data"]
    endpoints = data.get("endpoints")
    seasons = data.get("seasons")
    if not isinstance(endpoints, dict) or not isinstance(seasons, list):
        raise ValueError("PUL manifest is missing endpoints or seasons")

    found = []
    for name, path in endpoints.items():
        found.append((str(name), 0, api.endpoint_url(path)))
    for entry in seasons:
        if not isinstance(entry, dict) or not isinstance(entry.get("endpoints"), dict):
            raise ValueError("PUL manifest contains an invalid season")
        season = int(entry["season"])
        for name, path in entry["endpoints"].items():
            dataset = "team-stats" if name == "teams" else str(name)
            found.append((dataset, season, api.endpoint_url(path)))
    return found


def ingest_pul(con: sqlite3.Connection, session=None, *, refresh: bool = False) -> dict:
    """Follow the PUL manifest and replace every dataset it publishes."""
    manifest = api.get_json(api.MANIFEST_URL, session, refresh=refresh)
    fetched = []
    for dataset, season, url in _manifest_endpoints(manifest):
        payload = api.get_json(url, session, refresh=refresh)
        fetched.append((dataset, season, url, payload, _validate_payload(payload, url)))

    counts = {}
    with con:
        con.executescript(SCHEMA)
        for dataset, season, url, payload, rows in fetched:
            count = _replace_dataset(
                con,
                league="PUL",
                dataset=dataset,
                source_season=season,
                rows=rows,
                schema_version=payload.get("schemaVersion"),
                generated_at=payload.get("generatedAt"),
                source_url=url,
                key_function=lambda name, row, _index: _pul_key(name, row),
            )
            counts[(dataset, season)] = count
    return counts


def ingest_wul_csv(
    con: sqlite3.Connection,
    path: Path,
    *,
    season: int,
    dataset: str,
    source_url: str = "https://westernultimateleague.shinyapps.io/stats/",
) -> int:
    """Replace one WUL dashboard export without discarding unknown metrics."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"WUL export has no header: {path}")
        rows = [dict(row) for row in reader]
    with con:
        con.executescript(SCHEMA)
        return _replace_dataset(
            con,
            league="WUL",
            dataset=dataset,
            source_season=season,
            rows=rows,
            schema_version=None,
            generated_at=None,
            source_url=source_url,
            key_function=lambda _name, _row, index: str(index),
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    pul = commands.add_parser("pul", help="ingest every endpoint in the PUL manifest")
    pul.add_argument("--refresh", action="store_true")
    wul = commands.add_parser("wul", help="ingest one CSV exported by the WUL dashboard")
    wul.add_argument("season", type=int)
    wul.add_argument("dataset")
    wul.add_argument("file", type=Path)
    wul.add_argument(
        "--source-url",
        default="https://westernultimateleague.shinyapps.io/stats/",
    )
    args = parser.parse_args(argv)

    con = connect(args.db)
    try:
        if args.command == "pul":
            with requests.Session() as session:
                counts = ingest_pul(con, session, refresh=args.refresh)
            total = sum(counts.values())
            print(f"PUL: {total} rows across {len(counts)} manifest datasets")
        else:
            count = ingest_wul_csv(
                con,
                args.file,
                season=args.season,
                dataset=args.dataset,
                source_url=args.source_url,
            )
            print(f"WUL {args.season} {args.dataset}: {count} rows")
    finally:
        con.close()


if __name__ == "__main__":
    main()
