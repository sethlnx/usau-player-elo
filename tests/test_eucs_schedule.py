import hashlib
import unittest
from pathlib import Path

from scraper.eucs_schedule import (
    EUCSParseError,
    FetchedDocument,
    cross_check_ical,
    parse_schedule,
)

FIXTURES = Path(__file__).parent / "fixtures" / "euf"


def document(name):
    raw = (FIXTURES / name).read_bytes()
    return FetchedDocument(
        content=raw.decode(), source_url=f"https://fixture.test/{name}",
        observed_at="2024-10-07T00:00:00+00:00",
        payload_hash=hashlib.sha256(raw).hexdigest(), from_cache=True,
    )


class EUCSScheduleContractTests(unittest.TestCase):
    def test_schedule_classifies_played_placeholder_and_forfeit(self):
        schedule = parse_schedule(document("eucs_schedule.html"), "fixture24")
        self.assertEqual(["Mixed"], schedule.divisions)
        self.assertEqual(["played", "placeholder", "forfeit"],
                         [game["state"] for game in schedule.games])
        self.assertEqual((15, 10),
                         (schedule.games[0]["home_score"],
                          schedule.games[0]["away_score"]))
        self.assertEqual("101", schedule.games[0]["source_game_id"])
        self.assertIsNone(schedule.games[1]["home"])
        self.assertEqual("Winner of Semi 1", schedule.games[1]["home_label"])

    def test_calendar_is_cross_check_only(self):
        schedule = parse_schedule(document("eucs_schedule.html"), "fixture24")
        result = cross_check_ical(schedule, document("eucs_schedule.ics"))
        self.assertEqual({
            "ical_events": 3,
            "matched_slots": 3,
            "matched_pairings": 3,
            "unmatched_slots": 0,
        }, result)

    def test_duplicate_navigation_ids_are_deduplicated(self):
        duplicate = document("eucs_schedule.html")
        duplicate = FetchedDocument(
            content=duplicate.content.replace("game=103", "game=101"),
            source_url=duplicate.source_url,
            observed_at=duplicate.observed_at,
            payload_hash=duplicate.payload_hash,
            from_cache=True,
        )
        schedule = parse_schedule(duplicate, "fixture24")
        self.assertEqual(2, len(schedule.games))


if __name__ == "__main__":
    unittest.main()
