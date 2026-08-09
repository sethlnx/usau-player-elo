"""Sweep leakage-safe team-momentum update speeds on the shared corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis.backtest import metrics, replay
from analysis.rating_corpus import load_corpus
from analysis.rankings import PUBLISHED
from elo.engine import EloConfig

VAL = (2022, 2023)
TEST = (2024, 2025)


def floats(value: str) -> list[float]:
    return [float(item) for item in value.split(",")]


def labels(include_held_out: bool) -> dict[str, tuple[int, ...]]:
    result = {"validation_2022_2023": VAL}
    if include_held_out:
        result["held_out_2024_2025"] = TEST
        result["current_2026"] = (2026,)
    return result


def evaluate(
    games, rosters, clubs, stat_events, *, strength: float, retention: float,
    mode: str, include_held_out: bool,
) -> dict:
    cfg = EloConfig(**{
        **PUBLISHED,
        "momentum_strength": strength,
        "momentum_retention": retention,
        "momentum_mode": mode,
    })
    records, _model = replay("player", games, rosters, clubs, cfg, stat_events)
    result = {
        "strength": strength,
        "retention": retention,
        "mode": mode,
    }
    for label, seasons in labels(include_held_out).items():
        result[label] = metrics(records, seasons=seasons)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strength", default="0,.5,1,2,4")
    parser.add_argument("--retention", default=".25,.5,.75,.9")
    parser.add_argument("--mode", choices=("continuation", "intensity"),
                        default="continuation")
    parser.add_argument("--include-held-out", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    games, rosters, clubs, stat_events = load_corpus()
    results = [
        evaluate(
            games, rosters, clubs, stat_events,
            strength=strength, retention=retention, mode=args.mode,
            include_held_out=args.include_held_out,
        )
        for strength in floats(args.strength)
        for retention in floats(args.retention)
    ]
    encoded = json.dumps(results, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    for result in results:
        val = result["validation_2022_2023"]
        print(
            f"mode={result['mode']} strength={result['strength']:.4f} "
            f"retention={result['retention']:.4f} "
            f"validation_n={val['n']} accuracy={val['accuracy']:.6f} "
            f"logloss={val['logloss']:.9f} brier={val['brier']:.9f}"
        )


if __name__ == "__main__":
    main()
