import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scraper.eucs_schedule import FetchedDocument, parse_schedule
from scraper.euf import init_db, ingest_event, replace_event, upsert_event

FIXTURE = Path(__file__).parent / "fixtures" / "euf" / "eucs_schedule.html"


def fixture_schedule():
    raw = FIXTURE.read_bytes()
    return parse_schedule(FetchedDocument(
        raw.decode(), "https://fixture.test/schedule", "2024-10-07T00:00:00+00:00",
        hashlib.sha256(raw).hexdigest(), True,
    ), "fixture24")


class EUFIngestionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "euf.db"
        self.con = init_db(self.path)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_schedule_ingestion_is_idempotent_and_consistent(self):
        first = ingest_event(self.con, 2024, fixture_schedule())
        second = ingest_event(self.con, 2024, fixture_schedule())
        self.assertEqual(first, second)
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertEqual(2, self.con.execute("SELECT COUNT(*) FROM event_teams").fetchone()[0])
        self.assertEqual(3, self.con.execute("SELECT COUNT(*) FROM games").fetchone()[0])
        self.assertEqual([], self.con.execute("PRAGMA foreign_key_check").fetchall())
        invalid = self.con.execute(
            """SELECT COUNT(*) FROM games WHERE status IN ('played','has_outcome')
               AND (home_id IS NULL OR away_id IS NULL OR home_score IS NULL
                    OR away_score IS NULL)"""
        ).fetchone()[0]
        self.assertEqual(0, invalid)

    def test_failed_replacement_rolls_back_complete_event(self):
        ingest_event(self.con, 2024, fixture_schedule())
        before = self.con.execute(
            "SELECT game_key,home_score,away_score FROM games ORDER BY game_key"
        ).fetchall()
        teams = [
            {"source_id": "alpha", "name": "Alpha"},
            {"source_id": "beta", "name": "Beta"},
        ]
        games = [{
            "source_id": "new", "home_source_id": "alpha",
            "away_source_id": "beta", "home_score": 15, "away_score": 9,
            "state": "played", "date": "2024-10-04", "time": "09:00",
            "home_name": "Alpha", "away_name": "Beta",
        }]
        event_id = self.con.execute("SELECT event_id FROM events").fetchone()[0]
        with self.assertRaises(ValueError):
            with self.con:
                replace_event(
                    self.con, event_id, "fixture", "fixture-event", "fixture-div",
                    teams, games,
                    [{"team_source_id": "missing", "place": 1,
                      "division": "euf-mixed", "source_id": "standing"}],
                    "unavailable", "https://fixture.test", "2024-10-07", "hash",
                )
        after = self.con.execute(
            "SELECT game_key,home_score,away_score FROM games ORDER BY game_key"
        ).fetchall()
        self.assertEqual(before, after)

    def test_score_disagreement_is_blocking_audit_finding(self):
        with self.con:
            event_id = upsert_event(
                self.con, 2024, "Fixture", "https://fixture.test/event",
                "euf-mixed", "fixture", "source-a",
            )
            teams = [
                {"source_id": "alpha", "name": "Alpha"},
                {"source_id": "beta", "name": "Beta"},
            ]
            common = {
                "source_id": "game-1", "home_source_id": "alpha",
                "away_source_id": "beta", "state": "played",
                "date": "2024-10-04", "time": "09:00",
                "home_name": "Alpha", "away_name": "Beta",
            }
            replace_event(
                self.con, event_id, "source-a", "event-a", "division-a",
                teams, [{**common, "home_score": 15, "away_score": 10}], [],
                "unavailable", "https://fixture.test/a", "2024-10-07", "hash-a",
            )
        with self.con:
            replace_event(
                self.con, event_id, "source-b", "event-b", "division-b",
                teams, [{**common, "home_score": 14, "away_score": 10}], [],
                "unavailable", "https://fixture.test/b", "2024-10-08", "hash-b",
            )
        row = self.con.execute(
            "SELECT severity,detail FROM audit_findings WHERE code='score_conflict'"
        ).fetchone()
        self.assertEqual("blocking", row[0])
        self.assertIn('"source_a_score":[15,10]', row[1])

    def test_tied_final_places_are_preserved(self):
        with self.con:
            event_id = upsert_event(
                self.con, 2024, "Ties", "https://fixture.test/ties",
                "euf-open", "ties", "fixture",
            )
            teams = [
                {"source_id": "alpha", "name": "Alpha"},
                {"source_id": "beta", "name": "Beta"},
            ]
            standings = [
                {"source_id": "stage|alpha", "team_source_id": "alpha",
                 "division": "Open", "place": 5},
                {"source_id": "stage|beta", "team_source_id": "beta",
                 "division": "Open", "place": 5},
            ]
            replace_event(
                self.con, event_id, "fixture", "ties", "ties|open",
                teams, [], standings, "unavailable",
                "https://fixture.test/ties", "2024-10-08", "hash",
            )
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM standings WHERE event_id=? AND place=5",
                (event_id,),
            ).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
