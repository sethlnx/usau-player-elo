"""Sweep annual and inactivity-based Elo decay on validation by default."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis.backtest import metrics, replay
from analysis.rating_corpus import load_corpus
from analysis.rankings import PUBLISHED
from elo.engine import EloConfig


def rates(value: str) -> list[float]:
    return [float(item) for item in value.split(",")]


def evaluate(
    games,
    rosters,
    clubs,
    stat_events,
    annual_decay: float,
    inactivity_decay: float,
    grace_days: int,
    include_held_out: bool = False,
) -> dict:
    config = EloConfig(
        **{
            **PUBLISHED,
            "offseason_regression": annual_decay,
            "inactivity_decay": inactivity_decay,
            "inactivity_grace_days": grace_days,
        }
    )
    records, _model = replay(
        "player", games, rosters, clubs, config, stat_events,
    )
    result = {
        "annual_decay": annual_decay,
        "inactivity_decay": inactivity_decay,
        "grace_days": grace_days,
        "fit_2017_2021": metrics(records, seasons=range(2017, 2022)),
        "validation_2022_2023": metrics(records, seasons=(2022, 2023)),
    }
    if include_held_out:
        result["held_out_2024_2025"] = metrics(
            records, seasons=(2024, 2025)
        )
        result["current_2026"] = metrics(records, seasons=(2026,))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--annual", type=rates, default=[0.0])
    parser.add_argument("--inactivity", type=rates, default=[0.0])
    parser.add_argument("--grace-days", type=int, default=90)
    parser.add_argument("--include-held-out", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    games, rosters, clubs, stat_events = load_corpus()
    results = []
    for annual_decay in args.annual:
        for inactivity_decay in args.inactivity:
            result = evaluate(
                games, rosters, clubs, stat_events,
                annual_decay, inactivity_decay, args.grace_days,
                args.include_held_out,
            )
            results.append(result)
            validation = result["validation_2022_2023"]
            print(
                f"annual={annual_decay:.4f} inactivity={inactivity_decay:.4f} "
                f"grace={args.grace_days} validation_n={validation['n']:,} "
                f"accuracy={validation['accuracy']:.6f} "
                f"logloss={validation['logloss']:.9f} "
                f"brier={validation['brier']:.9f}"
            )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, sort_keys=True) + "\n")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
