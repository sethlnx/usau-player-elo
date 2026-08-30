import sqlite3
import unittest

from analysis.tournaments import build, decompose
from scraper.build_db import SCHEMA


class TournamentPublicationTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(SCHEMA)

    def tearDown(self):
        self.con.close()

    def add_scheduled_event(self, event_id, name, start_date):
        url = f"https://example.test/{event_id}"
        self.con.execute(
            """INSERT INTO events
               (event_id, season, name, url, start_date, end_date, division)
               VALUES (?, 2999, ?, ?, ?, ?, 'club-men')""",
            (event_id, name, url, start_date, start_date),
        )
        for suffix, team_name in (("a", "Alpha"), ("b", "Beta")):
            self.con.execute(
                """INSERT INTO event_teams
                   (event_team_id, event_id, display_name, roster_fetched)
                   VALUES (?, ?, ?, 1)""",
                (f"{event_id}:{suffix}", event_id, team_name),
            )
        self.con.execute(
            """INSERT INTO games
               (event_id, game_key, stage, date, home_id, away_id,
                home_score, away_score, status)
               VALUES (?, 'game-1', 'Pool A', ?, ?, ?, 0, 0, 'Scheduled')""",
            (event_id, start_date, f"{event_id}:a", f"{event_id}:b"),
        )

    def test_upcoming_scheduled_event_remains_visible(self):
        self.add_scheduled_event(1, "Future Event", "2999-08-22")
        self.add_scheduled_event(2, "Past Event", "2000-08-22")

        payload = build(self.con)

        self.assertEqual(["Future Event"], [event[1] for event in payload["events"]])
        self.assertEqual([], payload["detail"][0]["p"])
        self.assertEqual([], payload["detail"][0]["b"])
        self.assertEqual([], payload["detail"][0]["o"])

    def test_upcoming_pool_schedule_inherits_prior_championship_tree(self):
        events = [
            (10, 2025, "2025 Example Cup", "2025-08-22"),
            (11, 2999, "2026 Example Cup", "2999-08-22"),
        ]
        self.con.executemany(
            """INSERT INTO events
               (event_id, season, name, url, start_date, end_date, division)
               VALUES (?, ?, ?, ?, ?, ?, 'club-men')""",
            [(event_id, season, name, f"https://example.test/cup/{event_id}",
              start, start)
             for event_id, season, name, start in events],
        )
        for event_id, _, _, _ in events:
            self.con.executemany(
                """INSERT INTO event_teams
                   (event_team_id, event_id, display_name, roster_fetched)
                   VALUES (?, ?, ?, 1)""",
                [(f"{event_id}:{team}", event_id, f"{event_id} {team}")
                 for team in "ABCD"],
            )

        prior_pool = [
            ("ab", "A", "B"), ("ac", "A", "C"), ("ad", "A", "D"),
            ("bc", "B", "C"), ("bd", "B", "D"), ("cd", "C", "D"),
        ]
        self.con.executemany(
            """INSERT INTO games
               (event_id, game_key, stage, date, home_id, away_id,
                home_score, away_score, status)
               VALUES (10, ?, 'Pool A', '2025-08-22', ?, ?, 15, 10, 'Final')""",
            [(key, f"10:{home}", f"10:{away}") for key, home, away in prior_pool],
        )
        self.con.executemany(
            """INSERT INTO games
               (event_id, game_key, stage, date, home_id, away_id,
                home_score, away_score, status)
               VALUES (10, ?, ?, '2025-08-23', ?, ?, 15, 10, 'Final')""",
            [
                ("semi-1", "Semifinals", "10:A", "10:D"),
                ("semi-2", "Semifinals", "10:B", "10:C"),
                ("final", "Final", "10:A", "10:B"),
            ],
        )
        self.con.executemany(
            """INSERT INTO games
               (event_id, game_key, stage, date, home_id, away_id,
                home_score, away_score, status)
               VALUES (11, ?, 'Pool A', '2999-08-22', ?, ?, 0, 0, 'Scheduled')""",
            [(key, f"11:{home}", f"11:{away}") for key, home, away in prior_pool],
        )

        payload = build(self.con)
        current_ix = next(
            index for index, event in enumerate(payload["events"])
            if event[0] == 11
        )
        prior_ix = next(
            index for index, event in enumerate(payload["events"])
            if event[0] == 10
        )
        forecast = payload["detail"][current_ix]["f"]

        self.assertEqual([["Pool A", 6]],
                         [[label, len(games)] for label, games in forecast["p"]])
        self.assertEqual(prior_ix, forecast["b"][0])
        self.assertEqual([2, 1],
                         [sum(bool(game) for game in round_games)
                          for round_games in forecast["b"][2]])
        self.assertTrue(all(source[0] == 0
                            for game in forecast["b"][2][0] if game
                            for source in game[:2]))

    def test_completed_round_robin_publishes_standings_winner(self):
        self.con.execute(
            """INSERT INTO events
               (event_id, season, name, url, start_date, end_date, division)
               VALUES (3, 2000, 'Rumspringa Round Robin', 'https://example.test/3',
                       '2000-08-22', '2000-08-22', 'club-men')"""
        )
        self.con.executemany(
            """INSERT INTO event_teams
               (event_team_id, event_id, display_name, roster_fetched)
               VALUES (?, 3, ?, 1)""",
            [("3:a", "EZ"), ("3:b", "Dub Club"), ("3:c", "Rumspringa"),
             ("3:d", "Bodega Cats")],
        )
        self.con.executemany(
            """INSERT INTO games
               (event_id, game_key, stage, date, home_id, away_id,
                home_score, away_score, status)
               VALUES (3, ?, 'Pool A', '2000-08-22', ?, ?, ?, ?, 'played')""",
            [
                ("game-1", "3:a", "3:b", 11, 9),
                ("game-2", "3:c", "3:d", 13, 3),
                ("game-3", "3:c", "3:a", 12, 9),
                ("game-4", "3:b", "3:d", 10, 8),
                ("game-5", "3:a", "3:d", 12, 9),
                ("game-6", "3:c", "3:b", 13, 7),
            ],
        )

        payload = build(self.con)

        event = payload["events"][0]
        self.assertEqual("Rumspringa", payload["teams"][event[10]])

    def test_supplemental_event_ids_are_namespaced(self):


        supplemental = sqlite3.connect(":memory:")
        supplemental.executescript(SCHEMA)
        supplemental.execute(
            """INSERT INTO events
               (event_id, season, name, url, start_date, end_date, division)
               VALUES (1, 2026, 'WFDF WUCC 2026', 'https://results.wfdf.sport/wucc-2026/',
                       '2026-08-15', '2026-08-22', 'club-men')"""
        )
        supplemental.executemany(
            """INSERT INTO event_teams
               (event_team_id, event_id, display_name, roster_fetched)
               VALUES (?, 1, ?, 1)""",
            [("wucc:a", "Alpha"), ("wucc:b", "Beta")],
        )
        supplemental.execute(
            """INSERT INTO games
               (event_id, game_key, stage, date, home_id, away_id,
                home_score, away_score, status)
               VALUES (1, 'game-1', 'Pool A', '2026-08-15',
                       'wucc:a', 'wucc:b', 13, 11, 'played')"""
        )
        try:
            self.add_scheduled_event(1, "USAU Event", "2999-08-22")
            payload = build(self.con, [(supplemental, "wfdf", [1])])
        finally:
            supplemental.close()

        rows = {event[1]: (index, event) for index, event in
                enumerate(payload["events"])}
        self.assertEqual(1, rows["USAU Event"][1][0])
        wucc_index, wucc = rows["WFDF WUCC 2026"]
        self.assertEqual("wfdf:1", wucc[0])
        self.assertEqual(4, wucc[8])
        self.assertEqual("wfdf", payload["eventSources"][wucc_index])
        self.assertIn(wucc_index, payload["detail"])

    def test_official_placements_order_parallel_finals(self):
        def game(key, stage, home, away, hs, away_score):
            return {
                "stage": stage, "date": "2026-08-15", "home": home,
                "away": away, "hs": hs, "as": away_score, "done": True,
                "br": None, "place": None, "btype": None, "bround": None,
                "ord": ("2026-08-15", 0, 0, key),
            }

        games = [
            game("semi-1", "Playoff Semifinals", "Revolver", "Bravo", 15, 10),
            game("semi-2", "Playoff Semifinals", "Charlie", "Delta", 15, 12),
            game("title", "Playoff Finals", "Revolver", "Charlie", 15, 11),
            game("third", "Playoff Finals", "Bravo", "Delta", 15, 13),
        ]

        _pools, brackets, _loose = decompose(
            games, {"Revolver": 1, "Charlie": 2, "Bravo": 3, "Delta": 4}
        )

        self.assertEqual(["champ", "3rd"], [bracket[0] for bracket in brackets])


if __name__ == "__main__":
    unittest.main()
