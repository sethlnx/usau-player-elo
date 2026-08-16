import sqlite3
import unittest
from unittest.mock import patch

from analysis.backtest import load_ufa_games, replay
from analysis.site import ufa_game_ratings
from elo.engine import EloConfig
from ufa.scrape import SCHEMA, scrape_season


class UFAIngestionTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(SCHEMA)

    def tearDown(self):
        self.con.close()

    def test_season_ingest_uses_annual_schedule_and_keeps_every_status(self):
        games = [
            {
                "gameID": "2026-04-25-A-B",
                "homeTeamID": "b",
                "awayTeamID": "a",
                "homeScore": 20,
                "awayScore": 18,
                "status": "Final",
                "startTimestamp": "2026-04-25T19:00:00-04:00",
                "week": "week-1",
            },
            {
                "gameID": "2026-08-27-C-D",
                "homeTeamID": "d",
                "awayTeamID": "c",
                "homeScore": 0,
                "awayScore": 0,
                "status": "Upcoming",
                "startTimestamp": "2026-08-27T17:00:00-05:00",
                "week": "week-16",
            },
        ]
        calls = []

        def get(endpoint, params, _session, refresh=False):
            calls.append((endpoint, params, refresh))
            if endpoint == "games":
                return games
            return []

        with patch("ufa.scrape.api.get", side_effect=get):
            result = scrape_season(self.con, object(), 2026, refresh=True)

        self.assertIn("2 games", result)
        self.assertEqual(
            [("games", {"date": "2026"}, True)],
            [call for call in calls if call[0] == "games"],
        )
        self.assertEqual(
            [("2026-04-25-A-B", "Final"), ("2026-08-27-C-D", "Upcoming")],
            self.con.execute(
                "SELECT game_id, status FROM ufa_games ORDER BY game_id"
            ).fetchall(),
        )

    def test_replay_adapter_scores_real_finals_as_one_event_per_game(self):
        self.con.executemany(
            "INSERT INTO ufa_teams "
            "(team_id,year,full_name) VALUES (?,?,?)",
            [("a", 2026, "Alpha"), ("b", 2026, "Beta")],
        )
        self.con.executemany(
            "INSERT INTO ufa_games VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("real", 2026, "2026-05-01", "a", "b", 20, 18, "Final", "week-2"),
                ("unplayed", 2026, "2026-05-02", "a", "b", 0, 0, "Final", "week-2"),
                ("forfeit", 2026, "2026-05-03", "a", "b", 1, 0, "Final", "week-2"),
            ],
        )
        links = {}
        stats = []
        for team, offset in (("a", 0), ("b", 100)):
            for number in range(7):
                upid = f"u{offset + number}"
                links[upid] = offset + number
                stats.append(("real", upid, team, 1, 0, 60, 0, 0, 0, 0, 0))
        self.con.executemany(
            "INSERT INTO ufa_game_stats "
            "(game_id,player_id,team_id,o_points_played,d_points_played,"
            "seconds_played,goals,assists,blocks,throwaways,drops) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            stats,
        )

        with patch("ufa.link.resolve_links", return_value=links):
            games, rosters, clubs = load_ufa_games(self.con)

        self.assertEqual(1, len(games))
        game = games[0]
        self.assertEqual("ufa:real", game["event_id"])
        self.assertEqual("real", game["game_key"])
        self.assertEqual("Beta at Alpha", game["event_name"])
        self.assertEqual("ufa:a", clubs[game["home_id"]])
        self.assertEqual("ufa:b", clubs[game["away_id"]])
        self.assertEqual(7, len(rosters[game["home_id"]]))
        self.assertEqual(7, len(rosters[game["away_id"]]))
        self.assertEqual(2, len(game["post_game_stats"]))

    def test_replay_adapter_uses_observed_yards_and_game_environment(self):
        self.con.executemany(
            "INSERT INTO ufa_teams "
            "(team_id,year,full_name) VALUES (?,?,?)",
            [("a", 2026, "Alpha"), ("b", 2026, "Beta")],
        )
        self.con.execute(
            "INSERT INTO ufa_games VALUES (?,?,?,?,?,?,?,?,?)",
            ("rich", 2026, "2026-05-01", "a", "b", 3, 3, "Final", "week-2"),
        )
        rows = [
            ("rich", "u1", "a", 10, 2, 2, 1, 0, 1, 0, 0, 100, 50),
            ("rich", "u2", "a", 10, 1, 1, 0, 0, 0, 0, 0, 50, 25),
            ("rich", "u3", "b", 10, 2, 2, 0, 0, 1, 0, 0, 75, 50),
            ("rich", "u4", "b", 10, 1, 1, 0, 0, 0, 0, 0, 25, 25),
        ]
        self.con.executemany(
            "INSERT INTO ufa_game_stats "
            "(game_id,player_id,team_id,o_points_played,goals,assists,"
            "blocks,callahans,throwaways,stalls,drops,"
            "yards_thrown,yards_received) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        reference = {
            "coefficients": {
                "goals": 0.5815, "assists": 0.6926,
                "blocks": 0.3715, "turnovers": -0.1302,
            },
            "observed_ufa": {
                "yard_coefficient": 0.00663,
                "score_action_coefficient": 0.223,
                "rating_scale": 0.2,
                "possession_value": "game-scoring-efficiency",
            },
        }

        with patch(
            "ufa.link.resolve_links",
            return_value={"u1": 1, "u2": 2, "u3": 3, "u4": 4},
        ):
            games, _, _ = load_ufa_games(self.con, reference)

        self.assertEqual(1, len(games))
        home_entries, home_id = games[0]["post_game_stats"][0]
        self.assertEqual(games[0]["home_id"], home_id)
        quality = {pid: value for pid, _, value in home_entries}
        # Raw E± uses GameSE = 6 / (6 + 2) = 0.75; the rating input is
        # normalized because UFA supplies one update per game, not per event.
        raw_e_plus = 150 * 0.00663 + 4 * 0.223 + (1 - 1) * 0.75
        self.assertAlmostEqual(raw_e_plus * 0.2, quality[1])

    def test_post_game_stats_cannot_change_an_earlier_prediction(self):
        base = {
            "season": 2026, "division": "ufa", "date": "2026-05-01",
            "home_id": "home", "away_id": "away",
            "home_score": 10, "away_score": 10,
        }
        games = [
            {**base, "sort": ("2026-05-01", "12:00", 0, "g1")},
            {
                **base,
                "date": "2026-05-08",
                "sort": ("2026-05-08", "12:00", 0, "g2"),
                "post_game_stats": [
                    ([("p1", 1.0, 5.0), ("p2", 1.0, -5.0)], "home:g2"),
                ],
            },
        ]
        rosters = {"home": ["p1", "p2"], "away": ["p3", "p4"]}
        clubs = {"home": "home", "away": "away"}
        cfg = EloConfig(k=0.0, stat_transfer_beta=1.0)
        ratings = []

        records, _ = replay(
            "player", games, rosters, clubs, cfg,
            on_game=lambda _g, _h, _a, model, _pre:
                ratings.append((model.players["p1"].rating,
                                model.players["p2"].rating)),
        )
        no_stats = [{key: value for key, value in game.items()
                     if key != "post_game_stats"} for game in games]
        control, _ = replay("player", no_stats, rosters, clubs, cfg)

        self.assertEqual(
            [record[3] for record in control],
            [record[3] for record in records],
        )
        self.assertEqual(ratings[0][0], ratings[0][1])
        self.assertGreater(ratings[1][0], ratings[1][1])

    def test_season_rating_is_the_last_game_point_in_that_season(self):
        history = {
            "events": [
                ["2025-05-01", "Game 1", 2025, 50],
                ["2025-06-01", "Game 2", 2025, 50],
                ["2026-05-01", "Game 3", 2026, 50],
            ],
            "teams": {
                "ufa:alpha": [
                    [0, 1, 1],
                    [[1500, 14], [1525, 15], [1510, 14]],
                ],
                "ordinary-club": [[0], [[1800, 20]]],
            },
        }

        self.assertEqual(
            {(2025, "alpha"): 1525, (2026, "alpha"): 1510},
            ufa_game_ratings(history),
        )


if __name__ == "__main__":
    unittest.main()
