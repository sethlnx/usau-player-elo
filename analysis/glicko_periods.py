"""Compare Glicko-2 rating-period boundaries on the complete corpus."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from analysis.backtest import (
    DB_PATH, load_games, load_maps, load_stat_events, load_ufa_stat_events,
)
from analysis.euf_ratings import EUF_DB, load_european_inputs, merge_inputs
from analysis.glicko_rankings import published_config, replay_glicko, scorecard
from analysis.international_ratings import load_international_inputs

ROOT = Path(__file__).resolve().parent.parent


def load_corpus():
    con = sqlite3.connect(DB_PATH)
    european = load_european_inputs(con, EUF_DB)
    international = load_international_inputs(con, european, EUF_DB)
    external = merge_inputs(european, international)
    games = sorted(
        [*load_games(con), *external.games], key=lambda game: game["sort"]
    )
    rosters, clubs = load_maps(con)
    rosters.update(external.rosters)
    clubs.update(external.clubs)
    stat_events = sorted(
        load_stat_events(con) + load_ufa_stat_events(con), key=lambda event: event[0]
    )
    con.close()
    return games, rosters, clubs, stat_events


def evaluate(period: str) -> dict:
    games, rosters, clubs, stat_events = load_corpus()
    records, _model = replay_glicko(
        "player", games, rosters, clubs, published_config(), stat_events,
        period=period,
    )
    return {"period": period, **scorecard(records)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("period", choices=("game", "day", "tournament"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(args.period)
    encoded = json.dumps(result, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
