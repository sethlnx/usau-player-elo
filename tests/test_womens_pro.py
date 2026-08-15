import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from womens_pro import api
from womens_pro.scrape import ingest_pul, ingest_wul_csv


class WomensProIngestionTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")

    def tearDown(self):
        self.con.close()

    def test_pul_follows_every_manifest_endpoint_and_preserves_fields(self):
        manifest = {
            "schemaVersion": "1.0",
            "league": "PUL",
            "data": {
                "endpoints": {
                    "games": "/api/v1/games.json",
                    "teams": "/api/v1/teams.json",
                },
                "seasons": [{
                    "season": "2026",
                    "endpoints": {
                        "standings": "/api/v1/2026/standings.json",
                        "schedule": "/api/v1/2026/schedule.json",
                        "teams": "/api/v1/2026/teams.json",
                    },
                }],
            },
        }
        payloads = {
            api.MANIFEST_URL: manifest,
            api.endpoint_url("/api/v1/games.json"): {
                "schemaVersion": "1.0", "generatedAt": "now", "league": "PUL",
                "data": [{"season": "2026", "week": 1, "date": "2026-04-04",
                          "awayAbbrev": "RAL", "homeAbbrev": "ATX",
                          "awayScore": 15, "homeScore": 8}],
            },
            api.endpoint_url("/api/v1/teams.json"): {
                "schemaVersion": "1.0", "generatedAt": "now", "league": "PUL",
                "data": [{"abbrev": "ATX", "name": "Austin Torch",
                          "colors": {"primary": "#f15a22", "secondary": "#1c1c1c"}}],
            },
            api.endpoint_url("/api/v1/2026/standings.json"): {
                "schemaVersion": "1.0", "generatedAt": "now", "league": "PUL",
                "data": [{"abbrev": "ATX", "name": "Austin Torch", "wins": 3,
                          "losses": 3, "last6": ["W", "L", "W", "L", "W", "L"]}],
            },
            api.endpoint_url("/api/v1/2026/schedule.json"): {
                "schemaVersion": "1.0", "generatedAt": "now", "league": "PUL",
                "data": [{"week": 1, "date": "2026-04-04", "awayAbbrev": "RAL",
                          "homeAbbrev": "ATX", "locationAddress": "1102 S Congress Ave",
                          "youtubeUrl": "https://youtu.be/example", "cancelled": False}],
            },
            api.endpoint_url("/api/v1/2026/teams.json"): {
                "schemaVersion": "1.0", "generatedAt": "now", "league": "PUL",
                "data": [{"abbrev": "ATX", "name": "Austin Torch", "goals": 70,
                          "completionRate": 83.8, "futureMetric": 12}],
            },
        }

        with patch("womens_pro.scrape.api.get_json", side_effect=lambda url, *_a, **_k: payloads[url]) as get:
            counts = ingest_pul(self.con, object())

        self.assertEqual(6, get.call_count)
        self.assertEqual(1, counts[("team-stats", 2026)])
        sources = self.con.execute(
            "SELECT dataset, source_season, row_count FROM womens_pro_sources "
            "ORDER BY dataset, source_season"
        ).fetchall()
        self.assertEqual(
            [("games", 0, 1), ("schedule", 2026, 1), ("standings", 2026, 1),
             ("team-stats", 2026, 1), ("teams", 0, 1)],
            sources,
        )
        payload = json.loads(self.con.execute(
            "SELECT payload_json FROM womens_pro_records "
            "WHERE league='PUL' AND dataset='team-stats'"
        ).fetchone()[0])
        self.assertEqual(12, payload["futureMetric"])
        self.assertEqual("Austin Torch", self.con.execute(
            "SELECT team FROM womens_pro_records "
            "WHERE league='PUL' AND dataset='standings'"
        ).fetchone()[0])

    def test_reingest_replaces_stale_pul_rows(self):
        manifest = {
            "league": "PUL",
            "data": {"endpoints": {"teams": "/api/v1/teams.json"}, "seasons": []},
        }
        teams_url = api.endpoint_url("/api/v1/teams.json")
        first = {"league": "PUL", "data": [
            {"abbrev": "A", "name": "Alpha"}, {"abbrev": "B", "name": "Beta"}
        ]}
        second = {"league": "PUL", "data": [{"abbrev": "A", "name": "Alpha"}]}

        with patch("womens_pro.scrape.api.get_json", side_effect=[manifest, first]):
            ingest_pul(self.con)
        with patch("womens_pro.scrape.api.get_json", side_effect=[manifest, second]):
            ingest_pul(self.con)

        self.assertEqual(teams_url, self.con.execute(
            "SELECT source_url FROM womens_pro_sources WHERE dataset='teams'"
        ).fetchone()[0])
        self.assertEqual(1, self.con.execute(
            "SELECT count(*) FROM womens_pro_records WHERE dataset='teams'"
        ).fetchone()[0])

    def test_wul_csv_is_lossless_and_indexes_common_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advanced.csv"
            path.write_text(
                "Team,Player,Opponent,Date,Season,OIS,Custom Metric\n"
                "Seattle Tempest,7 Ada Example,Utah Wild,3-14-2026,"
                "2026 Regular Season,12.5,unpublished\n"
            )
            count = ingest_wul_csv(
                self.con, path, season=2026, dataset="player-advanced-game"
            )

        self.assertEqual(1, count)
        team, player, opponent, game_date, payload_json = self.con.execute(
            "SELECT team, player, opponent, game_date, payload_json "
            "FROM womens_pro_records"
        ).fetchone()
        self.assertEqual(
            ("Seattle Tempest", "7 Ada Example", "Utah Wild", "3-14-2026"),
            (team, player, opponent, game_date),
        )
        self.assertEqual("unpublished", json.loads(payload_json)["Custom Metric"])
        columns = json.loads(self.con.execute(
            "SELECT columns_json FROM womens_pro_sources"
        ).fetchone()[0])
        self.assertIn("Custom Metric", columns)

    def test_pul_rejects_off_site_manifest_endpoint(self):
        manifest = {
            "league": "PUL",
            "data": {"endpoints": {"games": "https://example.com/games.json"}, "seasons": []},
        }
        with patch("womens_pro.scrape.api.get_json", return_value=manifest):
            with self.assertRaises(ValueError):
                ingest_pul(self.con)


if __name__ == "__main__":
    unittest.main()
