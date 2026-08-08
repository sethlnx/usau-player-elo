import unittest

from analysis.backtest import replay
from elo.engine import EloConfig


class StatTransferTrajectoryTests(unittest.TestCase):
    """A player's published/trajectory rating must never trail their peak.

    Regression coverage for the Jack Williams bug: `PlayerElo.observe_stats`
    applies zero-sum rating transfers walk-forward, deferred past the event
    that earned them for leakage safety. A caller building a rating
    trajectory from `on_game` alone (the old behavior) missed those
    transfers entirely, so a player's published rating could sit ABOVE every
    point on their own curve — and, worse, keep moving forever on stat lines
    with no rated event behind them (e.g. a UFA season with no USAU game in
    this corpus), inflating a retired player indefinitely. `replay`'s
    `on_stats` hook exists so a caller can book each transfer against the
    event_team_id that earned it, exactly as `on_game` books game deltas.
    """

    def config(self, **kwargs):
        cfg = dict(
            k=48.0, provisional_games=0, division_scale={"club-men": 260.0},
            division_bases={"club-men": 1500.0}, stat_transfer_beta=12.0,
            stat_transfer_clamp=90.0,
        )
        cfg.update(kwargs)
        return EloConfig(**cfg)

    def games(self):
        return [
            {"event_id": "e1", "game_key": "g1", "home_id": "H", "away_id": "A",
             "home_score": 15, "away_score": 10, "season": 2023,
             "division": "club-men", "sort": ("2023-01-01",), "date": "2023-01-01"},
        ]

    def test_on_stats_fires_with_event_team_id_and_post_transfer_rating(self):
        """The hook must expose the event_team_id it was given, unaltered,
        and the model state it observes must already include that stat
        event's transfer — otherwise a caller cannot correctly attribute the
        movement to its event, the exact gap that let the bug through."""
        seen = []

        def on_stats(end, entries, etid, model):
            seen.append((end, etid, {pid: model.players[pid].rating for pid, *_ in entries}))

        stat_events = [
            ("2023-06-01", [("p1", 1.0, 20), ("p2", 1.0, -20)], "team-event-42"),
        ]
        games = [
            {"event_id": "e1", "game_key": "g1", "home_id": "H", "away_id": "A",
             "home_score": 15, "away_score": 10, "season": 2023,
             "division": "club-men", "sort": ("2023-07-01",), "date": "2023-07-01"},
        ]
        rosters = {"H": ["p1"], "A": ["p2"]}
        clubs = {"H": "home-club", "A": "away-club"}

        records, model = replay(
            "player", games, rosters, clubs, self.config(),
            stat_events=stat_events, on_stats=on_stats,
        )

        self.assertEqual(len(seen), 1)
        end, etid, ratings = seen[0]
        self.assertEqual(end, "2023-06-01")
        self.assertEqual(etid, "team-event-42")
        # p1 out-quality'd p2, so the zero-sum transfer moved p1 up, p2 down,
        # and the hook must already see that movement (not the pre-transfer
        # base rating both players started at).
        self.assertGreater(ratings["p1"], 1500.0)
        self.assertLess(ratings["p2"], 1500.0)
        self.assertAlmostEqual(
            (ratings["p1"] - 1500.0) + (ratings["p2"] - 1500.0), 0.0, places=6)

    def test_ungrounded_stat_event_has_no_event_team_id_to_attribute_to(self):
        """A stat event loaded with no rated event behind it (the UFA-season
        case) must be passed through as event_team_id=None, so a caller can
        recognize it has nowhere on the curve to book the movement -- rather
        than silently attributing it to whatever game happens to be
        replaying when the walk-forward drain fires."""
        seen_etids = []

        def on_stats(end, entries, etid, model):
            seen_etids.append(etid)

        stat_events = [
            ("2024-09-01", [("p1", 1.0, 20), ("p2", 1.0, -20)], None),
        ]
        games = self.games()
        games[0]["date"] = "2024-10-01"
        games[0]["sort"] = ("2024-10-01",)
        rosters = {"H": ["p1"], "A": ["p2"]}
        clubs = {"H": "home-club", "A": "away-club"}

        replay(
            "player", games, rosters, clubs, self.config(),
            stat_events=stat_events, on_stats=on_stats,
        )

        self.assertEqual(seen_etids, [None])

    def test_omitting_on_stats_does_not_change_model_state(self):
        """The hook is purely an observation channel: whether or not a
        caller passes on_stats, the replayed model ends in the same state.
        A caller attributing trajectories must never be able to perturb the
        ratings the hook is reporting on."""
        stat_events = [
            ("2023-06-01", [("p1", 1.0, 20), ("p2", 1.0, -20)], "team-event-42"),
        ]
        games = [
            {"event_id": "e1", "game_key": "g1", "home_id": "H", "away_id": "A",
             "home_score": 15, "away_score": 10, "season": 2023,
             "division": "club-men", "sort": ("2023-07-01",), "date": "2023-07-01"},
        ]
        rosters = {"H": ["p1"], "A": ["p2"]}
        clubs = {"H": "home-club", "A": "away-club"}

        _, with_hook = replay(
            "player", games, rosters, clubs, self.config(),
            stat_events=stat_events, on_stats=lambda *a: None,
        )
        _, without_hook = replay(
            "player", games, rosters, clubs, self.config(),
            stat_events=stat_events,
        )

        self.assertAlmostEqual(
            with_hook.players["p1"].rating, without_hook.players["p1"].rating)
        self.assertAlmostEqual(
            with_hook.players["p2"].rating, without_hook.players["p2"].rating)


if __name__ == "__main__":
    unittest.main()
