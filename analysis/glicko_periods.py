"""Compare Glicko-2 rating-period boundaries on the complete corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis.glicko_rankings import published_config, replay_glicko, scorecard
from analysis.rating_corpus import load_corpus


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
