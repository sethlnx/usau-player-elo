import unittest

from analysis.glicko_rankings import replay_glicko
from glicko.engine import Glicko2Config, PlayerGlicko2, PlayerState


class Glicko2EngineTests(unittest.TestCase):
    def test_matches_published_glicko2_example(self):
        model = PlayerGlicko2(Glicko2Config(involvement_credit=False))
        model.players["player"] = PlayerState(1500.0, 200.0, 0.06)
        model.prepare_event([["player"]], "club-men", "2026-01-01")

        model.update_player(
            "player",
            [(1400.0, 30.0, 1.0, 1.0),
             (1550.0, 100.0, 0.0, 1.0),
             (1700.0, 300.0, 0.0, 1.0)],
            "club-men", "2026-01-01", 3,
        )

        state = model.players["player"]
        self.assertAlmostEqual(1464.06, state.rating, places=1)
        self.assertAlmostEqual(151.52, state.rd, places=1)
        self.assertAlmostEqual(0.059996, state.volatility, places=5)

    def test_tournament_is_one_rating_period(self):
        games = [
            {"event_id": 1, "game_key": "a", "season": 2026,
             "division": "club-men", "date": "2026-05-01",
             "sort": ("2026-05-01", 1), "home_id": "h", "away_id": "a",
             "home_score": 15, "away_score": 10, "stage": "Pool"},
            {"event_id": 1, "game_key": "b", "season": 2026,
             "division": "club-men", "date": "2026-05-02",
             "sort": ("2026-05-02", 2), "home_id": "h", "away_id": "a",
             "home_score": 15, "away_score": 12, "stage": "Final"},
        ]
        records, model = replay_glicko(
            "player", games, {"h": [1, 2], "a": [3, 4]},
            {"h": "Home", "a": "Away"},
            Glicko2Config(involvement_credit=False),
        )

        self.assertEqual(records[0][3], records[1][3])
        self.assertEqual(2, model.players[1].games)
        self.assertEqual(model.players[1].rating, model.players[2].rating)
        self.assertGreater(model.players[1].rating, 1500.0)
        self.assertLess(model.players[3].rating, 1500.0)

    def test_period_boundary_controls_when_results_become_predictive(self):
        games = [
            {"event_id": 1, "game_key": key, "season": 2026,
             "division": "club-men", "date": day,
             "sort": (day, time), "home_id": "h", "away_id": "a",
             "home_score": 15, "away_score": 10, "stage": "Pool"}
            for key, day, time in (
                ("a", "2026-05-01", "09:00"),
                ("b", "2026-05-01", "12:00"),
                ("c", "2026-05-02", "09:00"),
            )
        ]
        probabilities = {}
        for period in ("game", "day", "tournament"):
            records, _model = replay_glicko(
                "player", games, {"h": [1, 2], "a": [3, 4]},
                {"h": "Home", "a": "Away"},
                Glicko2Config(involvement_credit=False), period=period,
            )
            probabilities[period] = [record[3] for record in records]

        self.assertGreater(probabilities["game"][1], probabilities["game"][0])
        self.assertEqual(probabilities["day"][0], probabilities["day"][1])
        self.assertGreater(probabilities["day"][2], probabilities["day"][1])
        self.assertEqual(
            probabilities["tournament"],
            [probabilities["tournament"][0]] * 3,
        )


    def test_inactivity_expands_rating_deviation_not_rating(self):
        model = PlayerGlicko2(Glicko2Config())
        model.players[1] = PlayerState(
            rating=1700.0, rd=80.0, volatility=0.06,
            games=50, last_date="2025-01-01",
        )

        model.advance_to("2026-01-01")

        self.assertEqual(1700.0, model.players[1].rating)
        self.assertGreater(model.players[1].rd, 80.0)


if __name__ == "__main__":
    unittest.main()
