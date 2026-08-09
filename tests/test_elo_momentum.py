import unittest

from analysis.backtest import replay

from elo.engine import EloConfig, PlayerElo


class EloMomentumTests(unittest.TestCase):
    def model(self, **kwargs):
        config = dict(
            k=40.0,
            use_mov=False,
            momentum_strength=2.0,
            momentum_retention=0.0,
        )
        config.update(kwargs)
        return PlayerElo(EloConfig(**config))

    def test_continuing_hot_and_cold_streak_accelerates_shared_update(self):
        momentum = self.model()
        baseline = self.model(momentum_strength=0.0)

        for model in (momentum, baseline):
            model.play_game(["home"], ["away"], 15, 10, home_team="H", away_team="A")
        before_momentum = momentum.players["home"].rating
        before_baseline = baseline.players["home"].rating
        momentum.play_game(["home"], ["away"], 15, 10, home_team="H", away_team="A")
        baseline.play_game(["home"], ["away"], 15, 10, home_team="H", away_team="A")

        momentum_delta = momentum.players["home"].rating - before_momentum
        baseline_delta = baseline.players["home"].rating - before_baseline
        self.assertGreater(momentum_delta, baseline_delta)
        self.assertAlmostEqual(
            3000.0,
            momentum.players["home"].rating + momentum.players["away"].rating,
        )

    def test_continuation_does_not_accelerate_streak_reversal(self):
        momentum = self.model()
        baseline = self.model(momentum_strength=0.0)

        for model in (momentum, baseline):
            model.play_game(["home"], ["away"], 15, 10, home_team="H", away_team="A")
        momentum.play_game(["home"], ["away"], 10, 15, home_team="H", away_team="A")
        baseline.play_game(["home"], ["away"], 10, 15, home_team="H", away_team="A")

        self.assertAlmostEqual(
            baseline.players["home"].rating, momentum.players["home"].rating,
        )
        self.assertAlmostEqual(
            baseline.players["away"].rating, momentum.players["away"].rating,
        )

    def test_intensity_accelerates_a_streak_reversal(self):
        intensity = self.model(momentum_mode="intensity")
        baseline = self.model(momentum_strength=0.0)

        for model in (intensity, baseline):
            model.play_game(["home"], ["away"], 15, 10, home_team="H", away_team="A")
        before_intensity = intensity.players["home"].rating
        before_baseline = baseline.players["home"].rating
        intensity.play_game(["home"], ["away"], 10, 15, home_team="H", away_team="A")
        baseline.play_game(["home"], ["away"], 10, 15, home_team="H", away_team="A")

        intensity_delta = before_intensity - intensity.players["home"].rating
        baseline_delta = before_baseline - baseline.players["home"].rating
        self.assertGreater(intensity_delta, baseline_delta)

    def test_replay_keys_momentum_by_canonical_club(self):
        games = [
            {"event_id": 1, "game_key": str(index), "season": 2026,
             "division": "club-men", "date": f"2026-05-0{index}",
             "sort": (f"2026-05-0{index}", "12:00"), "home_id": "home",
             "away_id": "away", "home_score": 15, "away_score": 10}
            for index in (1, 2, 3)
        ]
        rosters = {"home": ["home-player"], "away": ["away-player"]}
        clubs = {"home": "Home club", "away": "Away club"}
        momentum_cfg = self.model().cfg
        baseline_cfg = self.model(momentum_strength=0.0).cfg

        momentum, _ = replay(
            "player", games, rosters, clubs, momentum_cfg,
        )
        baseline, _ = replay(
            "player", games, rosters, clubs, baseline_cfg,
        )

        self.assertGreater(momentum[2][3], baseline[2][3])

    def test_invalid_momentum_retention_is_rejected(self):
        model = self.model(momentum_retention=1.1)

        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            model.play_game(["home"], ["away"], 15, 10, home_team="H", away_team="A")

    def test_negative_momentum_strength_is_rejected(self):
        model = self.model(momentum_strength=-0.1)

        with self.assertRaisesRegex(ValueError, "must not be negative"):
            model.play_game(["home"], ["away"], 15, 10, home_team="H", away_team="A")


if __name__ == "__main__":
    unittest.main()
