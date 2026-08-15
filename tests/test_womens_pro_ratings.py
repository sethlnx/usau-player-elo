import json
import sqlite3
import unittest

from analysis.backtest import replay
from analysis.euf_ratings import EuropeanInputs, merge_inputs
from elo.engine import EloConfig
from womens_pro.ratings import load_womens_pro_inputs
from womens_pro.scrape import SCHEMA


CORE_SCHEMA = """
CREATE TABLE players (
    player_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    ambiguous INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    season INTEGER NOT NULL
);
CREATE TABLE event_teams (
    event_team_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL
);
CREATE TABLE roster_players (
    event_team_id TEXT NOT NULL,
    player_id INTEGER NOT NULL
);
"""


class WomensProRatingTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(SCHEMA + CORE_SCHEMA)

    def tearDown(self):
        self.con.close()

    def insert_record(self, league, dataset, season, key, row):
        self.con.execute(
            "INSERT INTO womens_pro_records VALUES (?,?,?,?,?,?,?,?,?)",
            (
                league,
                dataset,
                season,
                str(key),
                row.get("Team") or row.get("homeName"),
                row.get("Player"),
                row.get("Opponent") or row.get("awayName"),
                row.get("Date") or row.get("date"),
                json.dumps(row, separators=(",", ":"), sort_keys=True),
            ),
        )

    def add_wul_fixture(self):
        for key, row in enumerate((
            {
                "Team": "Alpha", "Player": "12 Alex Star", "Opponent": "Beta",
                "Date": "3-1-2024", "Season": "2024 Regular Season",
            },
            {
                "Team": "Beta", "Player": "03 Bea Jones", "Opponent": "Alpha",
                "Date": "3-1-2024", "Season": "2024 Regular Season",
            },
        ), 1):
            self.insert_record("WUL", "player-standard-game", 2024, key, row)
        self.insert_record(
            "WUL", "team-standard-game", 2024, "alpha",
            {"Team": "Alpha", "Opponent": "Beta", "Date": "3-1-2024",
             "Season": "2024 Regular Season", "G": "15", "GA": "10"},
        )
        self.insert_record(
            "WUL", "team-standard-game", 2024, "beta",
            {"Team": "Beta", "Opponent": "Alpha", "Date": "3-1-2024",
             "Season": "2024 Regular Season", "G": "10", "GA": "15"},
        )

    def test_wul_mirrored_scores_deduplicate_and_same_season_identity_bridges(self):
        self.con.execute("INSERT INTO players VALUES (7, 'Alex Star', 0)")
        self.con.execute("INSERT INTO events VALUES ('usau-2024', 2024)")
        self.con.execute("INSERT INTO event_teams VALUES ('usau-alpha', 'usau-2024')")
        self.con.execute("INSERT INTO roster_players VALUES ('usau-alpha', 7)")
        self.add_wul_fixture()

        inputs = load_womens_pro_inputs(self.con)

        self.assertEqual(1, len(inputs.games))
        game = inputs.games[0]
        self.assertEqual(("wul", 15, 10),
                         (game["division"], game["home_score"], game["away_score"]))
        self.assertEqual([7], inputs.rosters[game["home_id"]])
        self.assertEqual("Alex Star", inputs.player_names[7])
        self.assertFalse(any(
            str(player).startswith("ghost:")
            for side in (game["home_id"], game["away_id"])
            for player in inputs.rosters[side]
        ))
        self.assertEqual(0, inputs.ghost_scored_games)

        records, model = replay(
            "player", inputs.games, inputs.rosters, inputs.clubs,
            EloConfig(division_bases={"wul": 1600.0},
                      division_scale={"wul": 160.0}),
        )
        self.assertEqual("wul", records[0][1])
        self.assertGreater(model.players[7].rating, 1600.0)

    def test_conflicting_wul_mirror_is_rejected(self):
        self.add_wul_fixture()
        self.con.execute(
            "UPDATE womens_pro_records SET payload_json=? "
            "WHERE league='WUL' AND dataset='team-standard-game' AND record_key='beta'",
            (json.dumps({"Team": "Beta", "Opponent": "Alpha",
                         "Date": "3-1-2024", "G": "11", "GA": "15"}),),
        )

        with self.assertRaisesRegex(ValueError, "conflicting WUL score"):
            load_womens_pro_inputs(self.con)

    def test_pul_scores_replay_with_explicit_ghost_rosters_and_merge(self):
        self.insert_record(
            "PUL", "games", 2025, "game-1",
            {"season": "2025", "date": "2025-04-05",
             "homeAbbrev": "ALP", "homeName": "Alpha Radiance", "homeScore": 21,
             "awayAbbrev": "BET", "awayName": "Beta Pride", "awayScore": 15},
        )

        inputs = load_womens_pro_inputs(self.con)
        self.assertEqual(1, len(inputs.games))
        game = inputs.games[0]
        self.assertEqual("pul", game["division"])
        for side in (game["home_id"], game["away_id"]):
            self.assertEqual(1, len(inputs.rosters[side]))
            self.assertTrue(str(inputs.rosters[side][0]).startswith("ghost:pul:"))
        self.assertEqual(2, inputs.ghost_scored_games)
        self.assertIn((2025, "pul"), inputs.team_rosters)

        merged = merge_inputs(
            EuropeanInputs(games=[{"sort": ("2024-01-01",), "division": "other"}]),
            inputs,
        )
        self.assertEqual(["other", "pul"], [row["division"] for row in merged.games])
        self.assertIn((2025, "pul"), merged.team_rosters)

        records, model = replay(
            "player", inputs.games, inputs.rosters, inputs.clubs,
            EloConfig(division_bases={"pul": 1600.0},
                      division_scale={"pul": 160.0}),
        )
        self.assertEqual("pul", records[0][1])
        winner = inputs.rosters[game["home_id"]][0]
        loser = inputs.rosters[game["away_id"]][0]
        self.assertGreater(model.players[winner].rating, 1600.0)
        self.assertLess(model.players[loser].rating, 1600.0)


if __name__ == "__main__":
    unittest.main()
