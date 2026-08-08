"""Publish a Glicko-2 sister dataset from the complete player corpus."""

from __future__ import annotations

import json
import os
from collections import OrderedDict, defaultdict
from functools import partial
from pathlib import Path

from analysis.backtest import metrics
from analysis.rankings import PUBLISHED, main as publish_rankings
from glicko.engine import Glicko2Config, PlayerGlicko2

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "glicko"
PUBLISHED_PERIOD = "day"


def published_config() -> Glicko2Config:
    return Glicko2Config(
        division_bases=dict(PUBLISHED["division_bases"]),
        team_weight_tau=PUBLISHED["tau"],
        involvement_credit=PUBLISHED["involvement_credit"],
        involvement_shrink=PUBLISHED["involvement_shrink"],
        involvement_clamp=(0.25, 3.0),
    )


def replay_glicko(
    _model_kind: str,
    games,
    rosters,
    clubs,
    cfg: Glicko2Config,
    stat_events=None,
    on_game=None,
    on_stats=None,
    *,
    period: str = "tournament",
):
    """Replay game, tournament-day, or whole-tournament rating periods.

    on_stats is accepted for signature parity with `replay` (both are used as
    `main`'s pluggable `replay_fn`) but unused: Glicko-2 output is a research
    comparison, never published, so there is no trajectory to book against.
    """
    model = PlayerGlicko2(cfg)
    stats = stat_events or []
    stat_index = 0
    records = []

    if period not in {"game", "day", "tournament"}:
        raise ValueError(f"unknown Glicko-2 rating period: {period}")
    grouped = OrderedDict()
    for game in games:
        event_id = game["event_id"]
        game_date = game.get("date") or game["sort"][0]
        if period == "game":
            key = (event_id, game["game_key"])
        elif period == "day":
            key = (event_id, game_date)
        else:
            key = (event_id,)
        grouped.setdefault(key, []).append(game)
    event_groups = sorted(
        grouped.values(), key=lambda group: min(game["sort"] for game in group)
    )

    for event_games in event_groups:
        start_date = min(
            (game.get("date") or game["sort"][0]) for game in event_games
        )
        end_date = max(
            (game.get("date") or game["sort"][0]) for game in event_games
        )
        while stat_index < len(stats) and stats[stat_index][0] < start_date:
            model.observe_stats(stats[stat_index][1])
            stat_index += 1

        division = event_games[0].get("division", "club-men")
        season = event_games[0]["season"]
        event_rosters = {}
        for game in event_games:
            for side in ("home", "away"):
                team_id = game[f"{side}_id"]
                event_rosters[team_id] = rosters.get(team_id) or [
                    f"ghost:{clubs.get(team_id)}:{season}"
                ]
        model.prepare_event(list(event_rosters.values()), division, start_date)
        openings = {
            team_id: model.team_snapshot(roster)
            for team_id, roster in event_rosters.items()
        }
        usages = {
            team_id: model.roster_usage(roster)
            for team_id, roster in event_rosters.items()
        }
        observations = defaultdict(list)
        game_counts = defaultdict(int)
        scored = []

        for game in event_games:
            home_id, away_id = game["home_id"], game["away_id"]
            home, away = event_rosters[home_id], event_rosters[away_id]
            home_snapshot, away_snapshot = openings[home_id], openings[away_id]
            expected = model.expect_teams(home_snapshot, away_snapshot)
            outcome = (
                1.0 if game["home_score"] > game["away_score"]
                else 0.0 if game["home_score"] < game["away_score"]
                else 0.5
            )
            records.append((
                game["season"], game.get("division", "club-men"),
                game.get("date"), expected, outcome,
            ))
            scored.append((game, home, away))
            for player_id in home:
                observations[player_id].append((
                    away_snapshot[0], away_snapshot[1], outcome,
                    usages[home_id][player_id],
                ))
                game_counts[player_id] += 1
            for player_id in away:
                observations[player_id].append((
                    home_snapshot[0], home_snapshot[1], 1.0 - outcome,
                    usages[away_id][player_id],
                ))
                game_counts[player_id] += 1

        for player_id, player_observations in observations.items():
            model.update_player(
                player_id, player_observations, division, end_date,
                game_counts[player_id],
            )

        if on_game is not None:
            closings = {
                team_id: model.team_rating(roster)
                for team_id, roster in event_rosters.items()
            }
            team_games = defaultdict(int)
            for game, _home, _away in scored:
                team_games[game["home_id"]] += 1
                team_games[game["away_id"]] += 1
            for game, home, away in scored:
                home_id, away_id = game["home_id"], game["away_id"]
                home_share = (
                    closings[home_id] - openings[home_id][0]
                ) / team_games[home_id]
                away_share = (
                    closings[away_id] - openings[away_id][0]
                ) / team_games[away_id]
                synthetic_pre = (
                    closings[home_id] - home_share,
                    closings[away_id] - away_share,
                )
                on_game(game, home, away, model, synthetic_pre)

    if games:
        final_date = max((game.get("date") or game["sort"][0]) for game in games)
        model.advance_to(final_date)
    return records, model


def scorecard(records) -> dict:
    divisions = sorted({division for _season, division, _date, _e, _o in records})
    return {
        "all": metrics(records),
        "validation_2022_2023": metrics(records, seasons=(2022, 2023)),
        "held_out_2024_2025": metrics(records, seasons=(2024, 2025)),
        "current_2026": metrics(records, seasons=(2026,)),
        "held_out_by_division": {
            division: metrics(records, seasons=(2024, 2025), divisions=(division,))
            for division in divisions
        },
    }


def main() -> None:
    output = Path(os.environ.get("GLICKO_OUTPUT_DIR", DEFAULT_OUTPUT))
    period = os.environ.get("GLICKO_PERIOD", PUBLISHED_PERIOD)
    if period not in {"game", "day", "tournament"}:
        raise ValueError(f"unknown Glicko-2 rating period: {period}")
    output.mkdir(parents=True, exist_ok=True)
    records, _model = publish_rankings(
        cfg=published_config(),
        replay_fn=partial(replay_glicko, period=period),
        output_dir=output,
    )
    scores = {"period": period, **scorecard(records)}
    with open(output / "metrics.json", "w") as handle:
        json.dump(scores, handle, separators=(",", ":"), sort_keys=True)
    for label in ("all", "held_out_2024_2025", "current_2026"):
        result = scores[label]
        print(
            f"{label}: n={result.get('n', 0):,} "
            f"accuracy={result.get('accuracy', float('nan')):.4f} "
            f"logloss={result.get('logloss', float('nan')):.6f} "
            f"brier={result.get('brier', float('nan')):.6f}"
        )
    print(f"rating period: {period}")
    print(f"wrote {output / 'metrics.json'}")


if __name__ == "__main__":
    main()
