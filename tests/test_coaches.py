import unittest
import sqlite3

from analysis.coaches import BASE_RATING, CoachRatings, parse_coaches
from analysis.history_split import _trends
from scraper.build_db import SCHEMA
from scraper.graphql import ingest_event


class CoachParsingTests(unittest.TestCase):
    def test_parses_role_markers_nicknames_and_unlabelled_assistants(self):
        coaches = parse_coaches(
            "Aaron (AJ) Abraham (Head Coach) Allen Boitz (Head Coach)\n\tCasey LeMay"
        )
        self.assertEqual(
            [
                ("Aaron (AJ) Abraham", "Head Coach"),
                ("Allen Boitz", "Head Coach"),
                ("Casey LeMay", "Coach"),
            ],
            [(coach.name, coach.role) for coach in coaches],
        )

    def test_splits_measured_role_free_two_token_names(self):
        self.assertEqual(
            ["Tabetha Ridgway", "Caleigh Moore"],
            [coach.name for coach in parse_coaches("Tabetha Ridgway Caleigh Moore")],
        )

    def test_omits_ambiguous_unlabelled_text(self):
        self.assertEqual([], parse_coaches("William C Brandon Van Deusen"))


class CoachRatingTests(unittest.TestCase):
    def test_results_and_impact_answer_different_questions(self):
        ratings = CoachRatings(k=24)
        probabilities = ratings.observe(
            season=2025,
            division="club-men",
            event_date="2025-06-01",
            home=["favorite-coach"],
            away=["underdog-coach"],
            home_roster=1800,
            away_roster=1400,
            division_scale=400,
            outcome=0.0,
        )
        self.assertEqual((0.5, 10 / 11), probabilities)
        favorite = ratings.states["favorite-coach"]
        underdog = ratings.states["underdog-coach"]
        self.assertLess(favorite.results, BASE_RATING)
        self.assertGreater(underdog.results, BASE_RATING)
        self.assertLess(favorite.impact, favorite.results)
        self.assertGreater(underdog.impact, underdog.results)

    def test_unknown_opponent_staff_is_excluded_not_assumed_average(self):
        ratings = CoachRatings()
        result = ratings.observe(
            season=2025,
            division="club-men",
            event_date="2025-06-01",
            home=["known"],
            away=[],
            home_roster=1600,
            away_roster=1500,
            division_scale=400,
            outcome=1.0,
        )
        self.assertIsNone(result)
        self.assertEqual({}, ratings.states)
        self.assertEqual(1, ratings.eligible_games)
        self.assertEqual(0, ratings.covered_games)

    def test_staff_mean_moves_once_per_result(self):
        ratings = CoachRatings(k=20)
        ratings.observe(
            season=2025,
            division="club-mixed",
            event_date="2025-06-01",
            home=["a", "b"],
            away=["c"],
            home_roster=1500,
            away_roster=1500,
            division_scale=400,
            outcome=1.0,
        )
        self.assertEqual(1510, ratings.states["a"].results)
        self.assertEqual(1510, ratings.states["b"].results)
        self.assertEqual(1490, ratings.states["c"].results)


class CoachPipelineTests(unittest.TestCase):
    def test_graphql_ingest_replaces_coach_assignments(self):
        con = sqlite3.connect(":memory:")
        con.executescript(SCHEMA)
        con.execute(
            """INSERT INTO events
               (event_id,season,name,url,division)
               VALUES (1,2025,'Test','test','club-men')"""
        )
        team = {
            "event_team_id": "team-a", "display_name": "Alpha",
            "full_name": "Alpha", "city": "A",
            "coach_source": "Alice Able (Head Coach)",
            "coaches": parse_coaches("Alice Able (Head Coach)"),
        }
        data = {"teams": {"a": team}, "rosters": {}, "games": []}
        ingest_event(con, 1, data)
        self.assertEqual(
            [("alice able", "Alice Able", "Head Coach")],
            con.execute(
                "SELECT coach_key,coach_name,role FROM coach_entries"
            ).fetchall(),
        )
        self.assertEqual(
            1,
            con.execute(
                "SELECT coach_data_fetched FROM events WHERE event_id=1"
            ).fetchone()[0],
        )
        team["coach_source"] = "Bob Baker"
        team["coaches"] = parse_coaches("Bob Baker")
        ingest_event(con, 1, data)
        self.assertEqual(
            [("bob baker",)],
            con.execute("SELECT coach_key FROM coach_entries").fetchall(),
        )
        con.close()

    def test_trends_select_impact_and_results_ratings(self):
        history = {
            "events": [
                ["2024-01-01", "First", 2024, 0],
                ["2025-01-01", "Second", 2025, 0],
            ],
            "players": {},
            "teams": {},
            "coaches": {
                "alice": [[0, 1], [[1510, 1490, 1, "alpha"],
                                     [1530, 1480, 2, "alpha"]]],
            },
            "coachNames": {"alice": "Alice Able"},
        }
        trends = _trends(history, {}, {}, [2024, 2025], {2024: 0, 2025: 1})
        self.assertEqual([1510, 1530], trends["h|all|all"]["top"][0][3])
        self.assertEqual([1490, 1480], trends["w|all|all"]["top"][0][3])


if __name__ == "__main__":
    unittest.main()
