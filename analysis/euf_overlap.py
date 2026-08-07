"""Measure name-only overlap between EUCS Ranking rosters and USAU identities."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent


def exact_name_key(name: str) -> str:
    value = unicodedata.normalize("NFKC", name)
    return re.sub(r"\s+", " ", value).strip().casefold()


def folded_name_key(name: str) -> str:
    value = unicodedata.normalize("NFKD", name)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[.’']", "", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _eu_names(
    path: Path, key_fn: Callable[[str], str]
) -> dict[str, dict[str, set[Any]]]:
    grouped: dict[str, dict[str, set[Any]]] = defaultdict(
        lambda: {"names": set(), "seasons": set(), "teams": set()}
    )
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            """SELECT r.player_name,o.season,o.team_name
               FROM ranking_roster_entries r
               JOIN ranking_roster_observations o
                 ON o.source=r.source AND o.roster_id=r.roster_id"""
        )
        for name, season, team in rows:
            key = key_fn(name)
            grouped[key]["names"].add(name)
            grouped[key]["seasons"].add(int(season))
            grouped[key]["teams"].add(team)
    finally:
        con.close()
    return grouped


def _usau_identities(
    path: Path, key_fn: Callable[[str], str]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    con = sqlite3.connect(path)
    try:
        seasons: dict[int, set[int]] = defaultdict(set)
        for player_id, season in con.execute(
            """SELECT DISTINCT rp.player_id,e.season
               FROM roster_players rp
               JOIN event_teams et USING(event_team_id)
               JOIN events e USING(event_id)"""
        ):
            seasons[int(player_id)].add(int(season))
        for player_id, name, ambiguous in con.execute(
            "SELECT player_id,display_name,ambiguous FROM players"
        ):
            grouped[key_fn(name)].append({
                "player_id": int(player_id),
                "name": name,
                "ambiguous": bool(ambiguous),
                "seasons": seasons[int(player_id)],
            })
    finally:
        con.close()
    return grouped


def _comparison(
    eu: dict[str, dict[str, set[Any]]],
    usau: dict[str, list[dict[str, Any]]],
    include_candidates: bool,
) -> dict[str, Any]:
    shared = set(eu) & set(usau)
    unique = {
        key for key in shared
        if len(eu[key]["names"]) == 1
        and len(usau[key]) == 1
        and not usau[key][0]["ambiguous"]
    }
    same_season = {
        key for key in unique
        if eu[key]["seasons"] & usau[key][0]["seasons"]
    }
    result: dict[str, Any] = {
        "eu_name_keys": len(eu),
        "shared_name_keys": len(shared),
        "usau_identities_under_shared_keys": sum(len(usau[key]) for key in shared),
        "unique_unambiguous_candidates": len(unique),
        "same_season_candidates": len(same_season),
    }
    if include_candidates:
        result["candidates"] = [
            {
                "name_key": key,
                "eu_names": sorted(eu[key]["names"]),
                "eu_seasons": sorted(eu[key]["seasons"]),
                "eu_teams": sorted(eu[key]["teams"]),
                "usau_player_id": usau[key][0]["player_id"],
                "usau_name": usau[key][0]["name"],
                "usau_seasons": sorted(usau[key][0]["seasons"]),
                "same_season": key in same_season,
            }
            for key in sorted(unique)
        ]
    return result


def measure_overlap(
    euf_db: Path,
    usau_db: Path,
    *,
    include_candidates: bool = False,
) -> dict[str, Any]:
    exact_eu = _eu_names(euf_db, exact_name_key)
    exact_usau = _usau_identities(usau_db, exact_name_key)
    folded_eu = _eu_names(euf_db, folded_name_key)
    folded_usau = _usau_identities(usau_db, folded_name_key)
    exact = _comparison(exact_eu, exact_usau, include_candidates)
    folded = _comparison(folded_eu, folded_usau, False)
    return {
        "euf_roster_display_names": len({
            name for item in exact_eu.values() for name in item["names"]
        }),
        "usau_identities": sum(len(items) for items in exact_usau.values()),
        "stable_id_comparison_available": False,
        "stable_id_matches": 0,
        "exact_casefolded": exact,
        "diacritic_folded": folded,
        "additional_diacritic_folded_shared_keys": (
            folded["shared_name_keys"] - exact["shared_name_keys"]
        ),
        "interpretation": (
            "These are name-match candidates, not verified cross-source identities. "
            "EUCS Ranking exposes roster names but no stable player IDs."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure non-authoritative USAU/EU roster-name overlap"
    )
    parser.add_argument("--euf-db", type=Path, default=ROOT / "data" / "euf.db")
    parser.add_argument("--usau-db", type=Path, default=ROOT / "data" / "usau.db")
    parser.add_argument("--candidates", action="store_true")
    args = parser.parse_args()
    report = measure_overlap(
        args.euf_db, args.usau_db, include_candidates=args.candidates
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
