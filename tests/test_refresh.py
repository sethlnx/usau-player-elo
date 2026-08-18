import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scraper import refresh


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


if __name__ == "__main__":
    unittest.main()
