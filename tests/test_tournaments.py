import sqlite3
import unittest

from analysis.tournaments import build
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


if __name__ == "__main__":
    unittest.main()
