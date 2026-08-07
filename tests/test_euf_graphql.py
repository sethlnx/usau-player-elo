import asyncio
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from api.euf_schema import event_game_loader, schema
from scraper.eucs_schedule import FetchedDocument, parse_schedule
from scraper.euf import init_db, ingest_event
from scraper.euf_ranking import SOURCE as RANKING_SOURCE, ingest_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "euf" / "eucs_schedule.html"


class EUFGraphQLContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "euf.db"
        con = init_db(self.path)
        raw = FIXTURE.read_bytes()
        schedule = parse_schedule(FetchedDocument(
            raw.decode(), "https://fixture.test/schedule",
            "2024-10-07T00:00:00+00:00", hashlib.sha256(raw).hexdigest(), True,
        ), "fixture24")
        ingest_event(con, 2024, schedule)
        event_id, event_team_id = con.execute(
            """SELECT event_id,event_team_id FROM event_teams
               WHERE display_name='Alpha'"""
        ).fetchone()
        con.execute(
            "UPDATE roster_availability SET state='public' WHERE event_id=?",
            (event_id,),
        )
        con.execute(
            """INSERT INTO roster_entries
               (event_team_id,number,name,points,assists,ds,turns)
               VALUES (?,?,?,?,?,?,?)""",
            (event_team_id, "7", "Fixture Player", "4", "3", "2", "1"),
        )
        con.commit()
        ingest_snapshot(con, {
            "source": RANKING_SOURCE,
            "source_url": "https://ranking.ultimatefederation.eu/",
            "observed_at": "2026-08-07T20:00:00+00:00",
            "rows": [
                {
                    "season": "2025",
                    "division": "Mixed",
                    "team": "Ranking Alpha",
                    "url": "https://fixture.test/session/roster",
                    "payload_hash": "a" * 64,
                    "players": ["Alice Example", "Bob Example"],
                },
                {
                    "season": "2025",
                    "division": "Mixed",
                    "team": "Ranking Empty",
                    "url": "https://fixture.test/session/empty",
                    "payload_hash": "b" * 64,
                    "players": [],
                },
            ],
        })
        con.close()
        self.previous = os.environ.get("EUF_DB")
        os.environ["EUF_DB"] = str(self.path)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("EUF_DB", None)
        else:
            os.environ["EUF_DB"] = self.previous
        self.temp.cleanup()

    def execute(self, query):
        result = asyncio.run(schema.execute(
            query,
            context_value={"event_games": event_game_loader(str(self.path))},
        ))
        self.assertIsNone(result.errors, result.errors)
        return result.data

    def test_event_games_sources_and_nullable_roster(self):
        data = self.execute("""
          { events(source:"eucs-schedule", eventCode:"fixture24", season:2024,
                   division:"euf-mixed", team:"Alpha", first:10) {
              totalCount pageInfo { hasNextPage }
              nodes { id eventCode divisions rosterState
                sources { source sourceId sourceUrl payloadHash }
                games(first:10, played:true) {
                  totalCount nodes { homeTeam awayTeam homeScore awayScore status played }
                }
              }
            }
            teams(eventCode:"fixture24", name:"Alpha", first:10) {
              totalCount nodes {
                id name rosterAvailable players
                eventRosterEntries {
                  eventCode event season division team name number
                  points assists ds turns
                }
              }
            }
          }
        """)
        events = data["events"]
        self.assertEqual(1, events["totalCount"])
        self.assertFalse(events["pageInfo"]["hasNextPage"])
        self.assertEqual(2, events["nodes"][0]["games"]["totalCount"])
        self.assertEqual("eucs-schedule", events["nodes"][0]["sources"][0]["source"])
        team = data["teams"]["nodes"][0]
        self.assertEqual("Alpha", team["name"])
        self.assertTrue(team["rosterAvailable"])
        self.assertEqual(["Fixture Player"], team["players"])
        entry = team["eventRosterEntries"][0]
        self.assertEqual("fixture24", entry["eventCode"])
        self.assertTrue(entry["event"])
        self.assertEqual(2024, entry["season"])
        self.assertEqual("euf-mixed", entry["division"])
        self.assertEqual("Alpha", entry["team"])
        self.assertEqual({
            "name": "Fixture Player", "number": "7", "points": "4",
            "assists": "3", "ds": "2", "turns": "1",
        }, {key: entry[key] for key in (
            "name", "number", "points", "assists", "ds", "turns",
        )})

    def test_ranking_team_exposes_observed_roster_names(self):
        data = self.execute("""
          { teams(source:"eucs-ranking", season:2025, name:"Ranking Alpha",
                  first:10) {
              totalCount nodes {
                name rosterAvailable players sources { source sourceId }
              }
            }
            empty: teams(source:"eucs-ranking", name:"Ranking Empty", first:10) {
              nodes { rosterAvailable players }
            }
          }
        """)
        self.assertEqual(1, data["teams"]["totalCount"])
        team = data["teams"]["nodes"][0]
        self.assertTrue(team["rosterAvailable"])
        self.assertEqual(["Alice Example", "Bob Example"], team["players"])
        self.assertEqual("eucs-ranking", team["sources"][0]["source"])
        empty = data["empty"]["nodes"][0]
        self.assertFalse(empty["rosterAvailable"])
        self.assertEqual([], empty["players"])

    def test_unknown_ids_return_null_and_schema_has_no_mutation(self):
        data = self.execute("""
          { event(id:"not-an-id") { id } team(id:"not-a-team") { id }
            __schema { mutationType { name } } }
        """)
        self.assertIsNone(data["event"])
        self.assertIsNone(data["team"])
        self.assertIsNone(data["__schema"]["mutationType"])

    def test_pages_are_bounded(self):
        with self.assertLogs(level="ERROR"):
            result = schema.execute_sync("{ games(first:201) { totalCount } }")
        self.assertIsNotNone(result.errors)
        self.assertIn("first must be between 1 and 200", str(result.errors[0]))


if __name__ == "__main__":
    unittest.main()
