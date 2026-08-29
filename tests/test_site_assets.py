import json
import sqlite3
import tempfile
from html.parser import HTMLParser
import unittest
from pathlib import Path

from analysis.backtest import DB_PATH
from analysis.site import (bucket_urls, load_csv,
                           TEMPLATE,
                           load_player_box_score_payload,
                           load_player_metric_payload, load_ufa_payload,
                           sidecar_version, ufa_game_ratings)


class SiteAssetContractTests(unittest.TestCase):
    def test_refresh_link_opens_authenticated_workflow(self):
        self.assertIn(
            'href="https://github.com/sethlnx/usau-player-elo/actions/workflows/'
            'refresh-rankings.yml"',
            TEMPLATE,
        )
        self.assertIn(">Refresh rankings</a>", TEMPLATE)
        self.assertNotIn("GITHUB_TOKEN", TEMPLATE)

    def test_tournament_detail_is_a_sibling_of_the_list(self):
        class ParentParser(HTMLParser):
            void_tags = {
                "area", "base", "br", "col", "embed", "hr", "img", "input",
                "link", "meta", "param", "source", "track", "wbr",
            }

            def __init__(self):
                super().__init__()
                self.stack = []
                self.parents = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                element_id = attrs.get("id")
                if element_id:
                    parent_attrs = dict(self.stack[-1][1]) if self.stack else {}
                    self.parents[element_id] = parent_attrs.get("id")
                if tag not in self.void_tags:
                    self.stack.append((tag, attrs.items()))

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, -1, -1):
                    if self.stack[index][0] == tag:
                        del self.stack[index:]
                        return

        parser = ParentParser()
        parser.feed(TEMPLATE)

        self.assertEqual("events", parser.parents["tlist"])
        self.assertEqual("events", parser.parents["tview"])

    def test_sidecar_urls_change_with_data_and_generator_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "history.json"
            generator = Path(tmp) / "generator.py"
            source.write_text("first")
            generator.write_text("first")
            first = sidecar_version((source,), (generator,))
            urls = bucket_urls(Path("p"), {0: {}, 31: {}}, first)
            self.assertEqual(f"p/0.js?v={first}", urls["0"])
            self.assertEqual(f"p/31.js?v={first}", urls["31"])

            source.write_text("second")
            self.assertNotEqual(first, sidecar_version((source,), (generator,)))

            source.write_text("first")
            generator.write_text("second")
            self.assertNotEqual(first, sidecar_version((source,), (generator,)))

    def test_player_metric_payload_preserves_missing_scores_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "player_metrics.csv"
            path.write_text(
                "player_id,ovr,thr,pos,off,def,goals,assists,blocks,turnovers,"
                "season,thr_reliability,pos_reliability,off_reliability,"
                "def_reliability,stats_through,history_seasons,"
                "weighted_prior_throw_attempts,model_version\n"
                "7,88,67,74,,68,26,29,4,12,2026,0.7309,0.8,,0.618,"
                "2026-08-08,3,145.5,ufa-eb-v4\n"
            )
            payload, meta = load_player_metric_payload(path)
            missing, missing_meta = load_player_metric_payload(
                Path(tmp) / "absent.csv"
            )
        self.assertEqual(
            [88, 67, 74, None, 68, 26, 29, 4, 12, 2026, 73, 80, None,
             62, "2026-08-08", 3, 145],
            payload["7"],
        )
        self.assertEqual(
            {"modelVersion": "ufa-eb-v4", "season": 2026,
             "statsThrough": "2026-08-08"},
            meta,
        )
        self.assertEqual(({}, {}), (missing, missing_meta))

    def test_box_score_payload_aggregates_complete_events_by_division(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "player_box_scores.csv"
            path.write_text(
                "player_id,season,division,goals,assists,blocks,turnovers,"
                "plus_minus,edge_proxy,team_games,event_end_date,coverage_flags,"
                "model_version\n"
                "7,2025,club-women,3,21,2,17,9,14.82,6,2025-10-26,"
                "gabt-complete,source-aware-edge-v2\n"
                "7,2025,club-mixed,1,2,0,1,2,1.50,6,2025-09-14,"
                "gabt-complete,source-aware-edge-v2\n"
                "9,2025,club-women,4,3,,,7,,6,2025-09-14,"
                "\"missing:blocks,turnovers\",source-aware-edge-v2\n"
            )
            payload, meta = load_player_box_score_payload(path)
            missing, missing_meta = load_player_box_score_payload(
                Path(tmp) / "absent.csv"
            )
        self.assertEqual(3, len(payload["7"]))
        self.assertEqual(
            [2025, -1, 4, 23, 2, 18, 11, 16.32, 1.36, 12, 2, "2025-10-26"],
            payload["7"][0],
        )
        self.assertEqual(
            [2025, 4, 3, 21, 2, 17, 9, 14.82, 2.47, 6, 1, "2025-10-26"],
            payload["7"][2],
        )
        self.assertNotIn("9", payload)
        self.assertEqual({
            "modelVersion": "source-aware-edge-v2",
            "latestSeason": 2025,
            "statsThrough": "2025-10-26",
            "statsThroughBySeasonDivision": {
                "2025|-1": "2025-10-26",
                "2025|3": "2025-09-14",
                "2025|4": "2025-10-26",
            },
        }, meta)
        self.assertEqual(({}, {}), (missing, missing_meta))
    def test_best_roster_without_played_roster_gets_a_lazy_bucket(self):
        from analysis.history_split import split

        history = {
            "events": [["2026-01-01", "Test", 2026, 0]],
            "players": {},
            "teams": {},
            "rosters": {},
            "bestRosters": {"canada:test:club": [0, "2026-01-01", []]},
            "people": [],
            "peoplePid": [],
        }
        _, player_buckets, roster_buckets, _ = split(
            history, {}, {}, player_roles={"9": [2026, 3, 80, 1]},
        )
        role_bucket = player_buckets[9 % 32]
        self.assertEqual([2026, 3, 80, 1], role_bucket["@9"])
        self.assertEqual(1, len(roster_buckets))
        bucket = next(iter(roster_buckets.values()))
        self.assertIn("canada:test:club", bucket["b"])

    def test_revolver_history_includes_wucc_2026(self):
        history = json.loads((DB_PATH.parent / "history.json").read_text())
        wucc_index = next(
            index
            for index, event in enumerate(history["events"])
            if event[1] == "WFDF WUCC 2026" and event[3] == 0
        )
        deltas = history["teams"]["revolver"][0]
        event_indices = []
        current = 0
        for delta in deltas:
            current += delta
            event_indices.append(current)

        self.assertIn(wucc_index, event_indices)



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
            [[1, 2, "2026-01-01", 2, 0]],
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
            linked_year, linked_team, linked_player = next(
                (int(year), team, player)
                for year, teams in payload.items()
                for team in teams
                for player in team["roster"]
                if player["linked"]
            )
            role_rows = {
                (linked_player["pid"], linked_year): {
                    "role": "handler", "confidence": "0.75",
                }
            }
            payload_with_roles = load_ufa_payload(
                con, players, history, role_rows,
            )
        finally:
            con.close()

        self.assertIn("2012", payload)
        self.assertTrue(any(team["rating"] is not None for team in payload["2012"]))
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
        role_player = next(
            player
            for candidate in payload_with_roles[str(linked_year)]
            if candidate["id"] == linked_team["id"]
            for player in candidate["roster"]
            if player["pid"] == linked_player["pid"]
        )
        self.assertEqual("handler", role_player["role"])
        self.assertEqual(0.75, role_player["roleConfidence"])

if __name__ == "__main__":
    unittest.main()
