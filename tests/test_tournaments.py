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
    def test_official_placement_relabels_parallel_final(self):
        def game(key, stage, home, away, hs, away_score):
            return {
                "stage": stage, "date": "2026-08-15", "home": home,
                "away": away, "hs": hs, "as": away_score, "done": True,
                "br": None, "place": None, "btype": None, "bround": None,
                "ord": ("2026-08-15", 0, 0, key),
            }

        games = [
            game("semi-1", "Playoff Semifinals", "A", "B", 15, 10),
            game("semi-2", "Playoff Semifinals", "C", "D", 15, 12),
            game("final", "Playoff Finals", "A", "C", 11, 15),
        ]

        _pools, brackets, _loose = decompose(
            games, {"C": 3, "A": 4, "B": 5, "D": 6}
        )

        self.assertEqual("3rd", brackets[0][0])


if __name__ == "__main__":
    unittest.main()
