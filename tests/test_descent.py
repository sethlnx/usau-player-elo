import unittest
from unittest.mock import Mock, patch

from analysis import descent
from analysis.backtest import replay
from analysis.rankings import DIVCODE, PUBLISHED, TEAM_DIVISIONS
from elo.engine import EloConfig


class DivisionKDescentTests(unittest.TestCase):
    def setUp(self):
        self.saved_globals = dict(descent._G)
        descent._G.clear()

    def tearDown(self):
        descent._G.clear()
        descent._G.update(self.saved_globals)

    def test_loader_merges_ufa_into_scored_corpus(self):
        usau = [{"sort": ("2022-01-01",), "division": "club-men"}]
        ufa = [{"sort": ("2022-06-01",), "division": "ufa"}]
        connection = Mock()

        with patch("sqlite3.connect", return_value=connection), \
             patch.object(descent, "load_games", return_value=usau), \
             patch.object(descent, "load_maps", return_value=({"usau": [1]}, {"usau": "Club"})), \
             patch.object(descent, "load_womens_pro_inputs",
                          return_value=Mock(games=[], rosters={}, clubs={})), \
             patch.object(descent, "load_ufa_games", return_value=(ufa, {"ufa": [2]}, {"ufa": "UFA"})), \
             patch.object(descent, "load_stat_events", return_value=[]), \
             patch.object(descent, "load_ufa_stat_data", return_value=({}, [])), \
             patch.object(descent, "load_ufa_stat_events", return_value=[]):
            descent._init()

        self.assertEqual(["club-men", "ufa"], [g["division"] for g in descent._G["games"]])
        self.assertEqual({"usau": [1], "ufa": [2]}, descent._G["rosters"])
        self.assertEqual({"usau": "Club", "ufa": "UFA"}, descent._G["clubs"])
        connection.close.assert_called_once_with()

    def test_group_override_changes_ufa_replay(self):
        def game(number, home_score, away_score):
            date = f"2022-06-0{number}"
            return {
                "event_id": f"ufa:{number}", "game_key": str(number),
                "season": 2022, "division": "ufa", "date": date,
                "sort": (date, "12:00", 0, str(number)),
                "home_id": "home", "away_id": "away",
                "home_score": home_score, "away_score": away_score,
            }

        descent._G.update(
            games=[game(1, 15, 10), game(2, 15, 10), game(3, 10, 15)],
            rosters={"home": ["home-player"], "away": ["away-player"]},
            clubs={"home": "Home", "away": "Away"}, stats=[],
        )

        low = descent._score(({"k_scale_group.ufa": 0.25}, ("ufa",)))
        high = descent._score(({"k_scale_group.ufa": 1.5}, ("ufa",)))

        self.assertNotEqual(low["val.ufa"], high["val.ufa"])
        self.assertEqual(0.25, descent.as_config({"k_scale_group.ufa": 0.25}).k_scale["ufa"])

    def test_beach_group_covers_every_published_beach_bracket(self):
        config = descent.as_config({"k_scale_group.beach": 0.5})

        self.assertEqual(
            set(descent.K_SCALE_GROUPS["beach"]),
            {division for division, scale in config.k_scale.items()
             if division.startswith("beach-") and scale == 0.5},
        )
        self.assertEqual(1.0, config.k_scale.get("club-men", 1.0))

    def test_pul_and_wul_fixtures_update_published_elo(self):
        for division in ("pul", "wul"):
            game = {
                "event_id": f"{division}:1",
                "game_key": "1",
                "season": 2026,
                "division": division,
                "date": "2026-04-01",
                "sort": ("2026-04-01", "12:00", 0, "1"),
                "home_id": "home",
                "away_id": "away",
                "home_score": 15,
                "away_score": 10,
            }
            records, model = replay(
                "player",
                [game],
                {"home": ["winner"], "away": ["loser"]},
                {"home": "Home", "away": "Away"},
                EloConfig(**PUBLISHED),
                [],
            )

            self.assertEqual(division, records[0][1])
            self.assertGreater(model.players["winner"].rating, 1600.0)
            self.assertLess(model.players["loser"].rating, 1600.0)
            self.assertEqual("club", descent.DIVISION_TIER[division])
            self.assertIn(division, TEAM_DIVISIONS)

        self.assertEqual(84, DIVCODE["pul"])
        self.assertEqual(85, DIVCODE["wul"])


if __name__ == "__main__":
    unittest.main()
