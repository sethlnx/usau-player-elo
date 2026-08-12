import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analysis.backtest import DB_PATH
from analysis.site import (bucket_urls, content_version, load_csv,
                           load_ufa_payload, ufa_game_ratings)


class SiteAssetContractTests(unittest.TestCase):
    def test_sidecar_urls_share_content_version_and_change_with_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "history.json"
            source.write_text("first")
            first = content_version((source,))
            urls = bucket_urls(Path("p"), {0: {}, 31: {}}, first)
            self.assertEqual(f"p/0.js?v={first}", urls["0"])
            self.assertEqual(f"p/31.js?v={first}", urls["31"])

            source.write_text("second")
            self.assertNotEqual(first, content_version((source,)))


    def test_head_to_head_ignores_draws_and_self_fixtures(self):
        from analysis.history_split import head_to_head_records

        history = {
            "events": [["2026-01-01", "Test", 2026, 0]],
            "games": {
                "0": [
                    [1, 2, 10, 8, 0, 0, 0],
                    [2, 1, 7, 9, 0, 0, 0],
                    [1, 2, 8, 8, 0, 0, 0],
                    [3, 3, 10, 1, 0, 0, 0],
                ]
            },
        }

        self.assertEqual(
            head_to_head_records(history),
            [[1, 2, 2026, 2, 0]],
        )

    def test_ufa_payload_contains_current_and_historical_rosters(self):
        con = sqlite3.connect(DB_PATH)
        try:
            players = {
                int(row["player_id"]): row
                for row in load_csv("player_elo.csv")
            }
            history = json.loads((DB_PATH.parent / "history.json").read_text())
            payload = load_ufa_payload(con, players, history)
        finally:
            con.close()

        self.assertIn("2021", payload)
        self.assertIn("2025", payload)
        self.assertGreaterEqual(len(payload["2025"]), 20)
        team = payload["2025"][0]
        self.assertTrue(team["name"])
        self.assertGreater(team["wins"] + team["losses"], 0)
        self.assertGreater(len(team["roster"]), 0)
        team_id = team["id"].split(":", 1)[1]
        self.assertEqual(
            ufa_game_ratings(history)[(2025, team_id)],
            team["rating"],
        )

if __name__ == "__main__":
    unittest.main()
