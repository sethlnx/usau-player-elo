import unittest

from analysis.backtest import build_ufa_stat_events
from elo.engine import EloConfig, PlayerElo


class UfaStatFeatureTests(unittest.TestCase):
    def setUp(self):
        self.data = (
            {"a": "p1", "b": "p2", "c": "p3"},
            [
                ("a", "team", 2024, 10, 10, 20, 100, 40, 1, 5),
                ("b", "team", 2024, 10, 5, 20, 50, 100, 2, 5),
                ("c", "team", 2024, 10, 5, 20, 50, 60, 3, 5),
            ],
        )

    def event_entries(self, **overrides):
        cfg = EloConfig(**overrides)
        return build_ufa_stat_events(self.data, cfg)[0][1]

    def test_zero_weights_preserve_existing_event_mapping(self):
        entries = self.event_entries()
        self.assertEqual(
            [(pid, usage) for pid, usage, _ in entries],
            [("p1", 1.0), ("p2", 1.0), ("p3", 1.0)],
        )
        for _, _, quality in entries:
            self.assertAlmostEqual(quality, 5 / 3)

    def test_completion_volume_increases_usage(self):
        entries = self.event_entries(ufa_completion_usage_weight=1.0)
        usage = {pid: value for pid, value, _ in entries}
        self.assertGreater(usage["p1"], usage["p2"])
        self.assertAlmostEqual(sum(usage.values()) / 3.0, 2.0)

    def test_new_quality_features_are_positive_for_relative_leaders(self):
        entries = self.event_entries(
            ufa_completion_pct_weight=1.0,
            ufa_throwing_yards_weight=1.0,
            ufa_receiving_yards_weight=1.0,
            ufa_hockey_assists_weight=1.0,
        )
        quality = {pid: value for pid, _, value in entries}
        self.assertGreater(quality["p1"], quality["p2"])
        self.assertGreater(quality["p2"], quality["p3"])

    def test_missing_features_are_neutral_and_transfers_zero_sum(self):
        data = (
            {"a": "p1", "b": "p2"},
            [
                ("a", "team", 2018, 10, None, None, None, None, None, 5),
                ("b", "team", 2018, 10, None, None, None, None, None, 5),
            ],
        )
        entries = build_ufa_stat_events(
            data,
            EloConfig(
                ufa_completion_pct_weight=4.0,
                ufa_throwing_yards_weight=4.0,
                ufa_receiving_yards_weight=4.0,
                ufa_hockey_assists_weight=4.0,
            ),
        )[0][1]
        self.assertEqual(
            [(pid, usage) for pid, usage, _ in entries],
            [("p1", 1.0), ("p2", 1.0)],
        )
        for _, _, quality in entries:
            self.assertAlmostEqual(quality, 5 / 3)

        model = PlayerElo(EloConfig(stat_transfer_beta=12.0))
        model.observe_stats(entries)
        self.assertAlmostEqual(model.players["p1"].rating + model.players["p2"].rating, 3000.0)


if __name__ == "__main__":
    unittest.main()
