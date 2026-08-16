import sqlite3
import unittest

from analysis.box_scores import aggregate_player_seasons, build_event_rows
from analysis.backtest import load_stat_events


REFERENCE = {
    "model_version": "source-aware-edge-v2",
    "coefficients": {
        "goals": 0.5815,
        "assists": 0.6926,
        "blocks": 0.3715,
        "turnovers": -0.1302,
    },
}


class BoxScoreTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript("""
            CREATE TABLE events (
                event_id INTEGER PRIMARY KEY, name TEXT, season INTEGER,
                division TEXT, end_date TEXT
            );
            CREATE TABLE event_teams (
                event_team_id TEXT PRIMARY KEY, event_id INTEGER,
                display_name TEXT
            );
            CREATE TABLE roster_entries (
                event_team_id TEXT, name TEXT, points TEXT, assists TEXT,
                ds TEXT, turns TEXT
            );
            CREATE TABLE roster_players (
                event_team_id TEXT, name TEXT, player_id TEXT
            );
            CREATE TABLE games (
                event_id INTEGER, home_id TEXT, away_id TEXT,
                home_score INTEGER, away_score INTEGER, status TEXT
            );

            INSERT INTO events VALUES
                (1, 'Complete Nationals', 2025, 'club-women', '2025-10-26'),
                (2, 'GA Only', 2025, 'club-women', '2025-09-14');
            INSERT INTO event_teams VALUES
                ('t1', 1, 'BENT'), ('opp1', 1, 'Opponent'),
                ('t2', 2, 'BENT');
            INSERT INTO roster_entries VALUES
                ('t1', 'Yina Cartagena', '3', '21', '2', '17'),
                ('t1', 'Zero Line', '', '', '', ''),
                ('t2', 'Partial Player', '4', '3', '', '');
            INSERT INTO roster_players VALUES
                ('t1', 'Yina Cartagena', '7'),
                ('t1', 'Zero Line', '8'),
                ('t2', 'Partial Player', '9');
            INSERT INTO games VALUES
                (1, 't1', 'opp1', 15, 10, 'Final'),
                (1, 't1', 'opp1', 14, 12, 'Final'),
                (2, 't2', 'other', 13, 9, 'Final');
        """)

    def tearDown(self):
        self.con.close()

    def test_complete_event_calculates_proxy_and_treats_player_blanks_as_zero(self):
        rows = build_event_rows(self.con, REFERENCE)
        by_player = {row["player"]: row for row in rows}
        yina = by_player["Yina Cartagena"]
        self.assertEqual(9, yina["plus_minus"])
        self.assertAlmostEqual(14.8187, yina["edge_proxy"], places=4)
        self.assertAlmostEqual(7.4093, yina["edge_proxy_per_team_game"], places=4)
        self.assertEqual("gabt-complete", yina["coverage_flags"])
        self.assertEqual(2, yina["team_games"])

        zero = by_player["Zero Line"]
        self.assertEqual((0, 0, 0, 0), tuple(zero[field] for field in (
            "goals", "assists", "blocks", "turnovers",
        )))
        self.assertEqual(0.0, zero["edge_proxy"])

    def test_event_missing_fields_does_not_manufacture_a_score(self):
        partial = next(
            row for row in build_event_rows(self.con, REFERENCE)
            if row["player"] == "Partial Player"
        )
        self.assertEqual("", partial["blocks"])
        self.assertEqual("", partial["turnovers"])
        self.assertEqual("", partial["edge_proxy"])
        self.assertEqual("missing:blocks,turnovers", partial["coverage_flags"])
    def test_rating_event_quality_uses_the_same_e_plus_proxy(self):
        events = load_stat_events(self.con, REFERENCE)
        self.assertEqual(1, len(events))
        end_date, entries, event_team_id = events[0]
        self.assertEqual("2025-10-26", end_date)
        self.assertEqual("t1", event_team_id)
        quality = {pid: value for pid, _, value in entries}
        self.assertAlmostEqual(14.8187, quality["7"], places=4)
        self.assertEqual(0.0, quality["8"])


    def test_season_aggregate_excludes_partial_events(self):
        aggregate = aggregate_player_seasons(build_event_rows(self.con, REFERENCE))
        by_player = {row["player_id"]: row for row in aggregate}
        self.assertNotIn("9", by_player)
        self.assertEqual(1, by_player["7"]["events"])
        self.assertEqual(2, by_player["7"]["team_games"])
        self.assertEqual(14.82, by_player["7"]["edge_proxy"])
        self.assertEqual(7.41, by_player["7"]["edge_proxy_per_team_game"])


if __name__ == "__main__":
    unittest.main()
