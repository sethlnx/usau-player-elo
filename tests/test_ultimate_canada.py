import sqlite3
import tempfile
import unittest
from pathlib import Path

from scraper.euf import init_db
from analysis.canada_ratings import load_canadian_inputs
from scraper.ultimate_canada_database import SOURCE, ingest_tournament


class FakeCanadaClient:
    def __init__(self, payload):
        self.payload = payload

    def get_tournament(self, key):
        return (
            self.payload,
            f"https://api.test/tournaments/{key}/data",
            "2026-08-11T00:00:00+00:00",
            "a" * 64,
        )


PAYLOAD = {
    "ok": True,
    "tournament": {
        "name": "Canadian Ultimate Championship 2026",
        "location": "Surrey, BC",
        "startDate": "2026-08-09",
        "endDate": "2026-08-12",
    },
    "divisions": ["Junior Open"],
    "teams": [
        {"team_id": "team-a", "name": "Alpha", "division": "Junior Open", "region": "BC"},
        {"team_id": "team-b", "name": "Beta", "division": "Junior Open", "region": "ON"},
    ],
    "pools": [{
        "poolId": "pool-a",
        "division": "Junior Open",
        "name": "Pool A",
        "participants": [
            {"teamId": "team-a", "label": "Alpha"},
            {"teamId": "team-b", "label": "Beta"},
        ],
        "games": [{
            "gameId": "game-1",
            "team1Id": "team-a",
            "team2Id": "team-b",
            "team1Display": "Alpha",
            "team2Display": "Beta",
            "score1": 15,
            "score2": 11,
            "field": "Field 1",
            "startDateISO": "2026-08-10",
            "startTime": "10:30",
        }],
        "results": [
            {"teamId": "team-a", "team": "Alpha", "rank": 1, "wins": 1, "losses": 0},
            {"teamId": "team-b", "team": "Beta", "rank": 2, "wins": 0, "losses": 1},
        ],
    }],
    "teamRosters": [{
        "team_id": "team-a",
        "player_id": "player-1",
        "player_number": "7",
        "first_name": "Alex",
        "last_name": "Example",
        "division": "Junior Open",
        "goals": 3,
        "assists": 2,
        "turnovers": 1,
        "blocks": 1,
    }],
}


class UltimateCanadaIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "euf.db"
        self.con = init_db(self.path)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_ingests_events_teams_games_standings_and_rosters_idempotently(self):
        first = ingest_tournament(self.con, FakeCanadaClient(PAYLOAD), "2026-cuc-jr")
        second = ingest_tournament(self.con, FakeCanadaClient(PAYLOAD), "2026-cuc-jr")
        self.assertEqual(first, second)
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertEqual(2, self.con.execute("SELECT COUNT(*) FROM event_teams").fetchone()[0])
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM games").fetchone()[0])
        self.assertEqual(2, self.con.execute("SELECT COUNT(*) FROM standings").fetchone()[0])
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM roster_entries").fetchone()[0])
        self.assertEqual(
            [SOURCE],
            [row[0] for row in self.con.execute(
                "SELECT DISTINCT source FROM source_entities ORDER BY source"
            )],
        )
        self.assertEqual(
            "https://canadian-ultimate-database.web.app/tournament/2026-cuc-jr",
            self.con.execute("SELECT url FROM events").fetchone()[0],
        )
        self.assertEqual([], self.con.execute("PRAGMA foreign_key_check").fetchall())

    def test_missing_roster_is_not_invented(self):
        payload = {key: value for key, value in PAYLOAD.items() if key != "teamRosters"}
        ingest_tournament(self.con, FakeCanadaClient(payload), "2026-cuc-jr")
        self.assertEqual(
            "unavailable",
            self.con.execute("SELECT state FROM roster_availability").fetchone()[0],
        )
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM roster_entries").fetchone()[0])

    def test_canadian_games_enter_elo_corpus_with_validity_filters(self):
        ingest_tournament(self.con, FakeCanadaClient(PAYLOAD), "2026-cuc-jr")
        self.con.execute("UPDATE games SET status='Cancelled'")
        self.con.commit()
        empty = load_canadian_inputs(self.path)
        self.assertEqual([], empty.games)

        self.con.execute("UPDATE games SET status='has_outcome'")
        self.con.commit()
        inputs = load_canadian_inputs(self.path)
        self.assertEqual(1, len(inputs.games))
        self.assertEqual("canada-junior-open", inputs.games[0]["division"])
        self.assertEqual(-2000000001, inputs.games[0]["event_id"])


if __name__ == "__main__":
    unittest.main()
