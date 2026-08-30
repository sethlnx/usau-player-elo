import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scraper import refresh
from scraper.build_db import SCHEMA


class TournamentRefreshTests(unittest.TestCase):
    def test_missing_finished_event_is_added_to_unified_database(self):
        event = {
            "id": "event-1",
            "url": "https://play.usaultimate.org/events/new-event",
            "name": "New Event",
            "startDate": "2026-08-01",
            "endDate": "2026-08-02",
        }
        data = {
            "games": [
                {
                    "status": "Final",
                    "home_id": "home",
                    "away_id": "away",
                    "home_score": 15,
                    "away_score": 12,
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "usau.db"
            database.touch()
            with (
                patch.object(refresh, "DB_PATH", database),
                patch.object(refresh, "list_events", return_value=[event]),
                patch.object(refresh, "fetch_event", return_value=data),
                patch.object(refresh, "upsert_event", return_value=1) as upsert,
                patch.object(refresh, "ingest_event") as ingest,
            ):
                result = refresh.refresh_division(
                    "club-women", [2026], workers=1, dry_run=False
                )

        self.assertEqual((0, 1, 1), result)
        upsert.assert_called_once()
        ingest.assert_called_once_with(upsert.call_args.args[0], 1, data)


    def test_unfetched_coaches_backfill_outside_requested_season(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = refresh.connect(Path(tmp) / "usau.db")
            con.executescript(SCHEMA)
            con.execute(
                """INSERT INTO events
                   (event_id, season, name, url, start_date, end_date, division)
                   VALUES (1, 2024, 'Old Event', '/old', '2024-01-01',
                           '2024-01-02', 'club-women')"""
            )
            con.execute(
                """INSERT INTO games
                   (event_id, game_key, home_score, away_score, status)
                   VALUES (1, 'g1', 15, 10, 'Final')"""
            )

            stale = refresh.stale_events(
                con, "club-women", [2026], "2026-01-01"
            )
            self.assertEqual((1, "Old Event", 2024, 1, True), stale["/old"])

            con.execute(
                "UPDATE events SET coach_data_fetched=1 WHERE event_id=1"
            )
            self.assertEqual(
                {},
                refresh.stale_events(
                    con, "club-women", [2026], "2026-01-01"
                ),
            )
            con.close()

    def test_less_complete_mirror_can_verify_no_published_staff(self):
        event = {
            "id": "event-1",
            "url": "/old",
            "name": "Old Event",
            "startDate": "2024-01-01",
            "endDate": "2024-01-02",
        }
        data = {
            "games": [{
                "status": "Final", "home_id": "a", "away_id": "b",
                "home_score": 15, "away_score": 10,
            }],
            "teams": {"a": {"coach_source": None}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "usau.db"
            con = refresh.connect(database)
            con.executescript(SCHEMA)
            con.execute(
                """INSERT INTO events
                   (event_id, season, name, url, start_date, end_date, division)
                   VALUES (1, 2024, 'Old Event', '/old', '2024-01-01',
                           '2024-01-02', 'club-women')"""
            )
            con.executemany(
                """INSERT INTO games
                   (event_id, game_key, home_score, away_score, status)
                   VALUES (1, ?, 15, 10, 'Final')""",
                [("g1",), ("g2",)],
            )
            con.commit()
            con.close()

            with (
                patch.object(refresh, "DB_PATH", database),
                patch.object(refresh, "list_events", return_value=[event]),
                patch.object(refresh, "fetch_event", return_value=data),
                patch.object(refresh, "ingest_event") as ingest,
            ):
                result = refresh.refresh_division(
                    "club-women", [2024], workers=1, dry_run=False
                )
            con = refresh.connect(database)
            fetched = con.execute(
                "SELECT coach_data_fetched FROM events WHERE event_id=1"
            ).fetchone()[0]
            con.close()

        self.assertEqual((1, 0, 0), result)
        self.assertEqual(1, fetched)
        ingest.assert_not_called()

    def test_unlisted_empty_division_is_verified_coach_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "usau.db"
            con = refresh.connect(database)
            con.executescript(SCHEMA)
            con.execute(
                """INSERT INTO events
                   (event_id, season, name, url, start_date, end_date, division)
                   VALUES (1, 2024, 'Empty Event', '/empty', '2024-01-01',
                           '2024-01-02', 'club-women')"""
            )
            con.commit()
            con.close()

            with (
                patch.object(refresh, "DB_PATH", database),
                patch.object(refresh, "list_events", return_value=[]),
                patch.object(refresh, "fetch_event") as fetch,
            ):
                result = refresh.refresh_division(
                    "club-women", [2024], workers=1, dry_run=False
                )
            con = refresh.connect(database)
            fetched = con.execute(
                "SELECT coach_data_fetched FROM events WHERE event_id=1"
            ).fetchone()[0]
            con.close()

        self.assertEqual((1, 0, 0), result)
        self.assertEqual(1, fetched)
        fetch.assert_not_called()

if __name__ == "__main__":
    unittest.main()
