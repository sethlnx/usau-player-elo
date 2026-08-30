import sqlite3
import tempfile
import unittest
from pathlib import Path

from analysis.euf_overlap import measure_overlap
from analysis.euf_ratings import (
    EuropeanInputs,
    _usa_bridge_candidates,
    european_player_id,
    merge_inputs,
)
from analysis.international_ratings import (
    _load_manual_usa_aliases,
    _usa_assignment,
)
from scraper.euf import init_db
from scraper.euf_ranking import SOURCE, ingest_snapshot


def snapshot(rows):
    return {
        "source": SOURCE,
        "source_url": "https://ranking.ultimatefederation.eu/",
        "observed_at": "2026-08-07T20:00:00+00:00",
        "rows": rows,
    }


def roster(season, division, team, players, digest="a" * 64):
    return {
        "season": str(season),
        "division": division,
        "team": team,
        "url": f"https://fixture.test/session/{team}/dataobj/team_master_roster",
        "payload_hash": digest,
        "players": players,
    }


class EUFRankingIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "euf.db"
        self.con = init_db(self.path)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_snapshot_replaces_rosters_idempotently_with_provenance(self):
        first = snapshot([
            roster(2025, "Mixed", "Alpha", ["Aidan  Downey", "Émile Test"]),
            roster(2025, "Open", "Empty", []),
        ])
        self.assertEqual(2, ingest_snapshot(self.con, first)["memberships"])
        self.assertEqual(2, ingest_snapshot(self.con, first)["memberships"])
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM ranking_roster_observations"
            ).fetchone()[0],
        )
        self.assertEqual(
            [("Aidan Downey", "aidan downey"), ("Émile Test", "émile test")],
            self.con.execute(
                "SELECT player_name,name_key FROM ranking_roster_entries "
                "ORDER BY ordinal"
            ).fetchall(),
        )
        self.assertEqual(
            [("empty", 0), ("ok", 2)],
            self.con.execute(
                "SELECT state,record_count FROM source_observations "
                "WHERE source=? ORDER BY state",
                (SOURCE,),
            ).fetchall(),
        )
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM source_entities "
                "WHERE source=? AND entity_type='team'",
                (SOURCE,),
            ).fetchone()[0],
        )
        self.assertEqual([], self.con.execute("PRAGMA foreign_key_check").fetchall())

        updated = snapshot([
            roster(2025, "Mixed", "Alpha", ["Aidan Downey"], "b" * 64),
        ])
        summary = ingest_snapshot(self.con, updated)
        self.assertEqual(1, summary["rosters"])
        self.assertEqual(1, summary["memberships"])
        self.assertEqual(1, self.con.execute(
            "SELECT COUNT(*) FROM ranking_roster_entries"
        ).fetchone()[0])
        self.assertEqual(1, self.con.execute(
            "SELECT COUNT(*) FROM source_entities WHERE source=?",
            (SOURCE,),
        ).fetchone()[0])

    def test_invalid_snapshot_rolls_back_existing_rosters(self):
        valid = roster(2025, "Mixed", "Alpha", ["Alice Example"])
        ingest_snapshot(self.con, snapshot([valid]))
        before = self.con.execute(
            "SELECT roster_id,record_count FROM ranking_roster_observations"
        ).fetchall()
        with self.assertRaisesRegex(ValueError, "duplicate ranking roster"):
            ingest_snapshot(self.con, snapshot([valid, valid]))
        self.assertEqual(
            before,
            self.con.execute(
                "SELECT roster_id,record_count FROM ranking_roster_observations"
            ).fetchall(),
        )

    def test_overlap_report_distinguishes_names_from_stable_ids(self):
        ingest_snapshot(self.con, snapshot([
            roster(2024, "Mixed", "Alpha", ["Alice Example", "Émile Test"]),
        ]))
        usau = Path(self.temp.name) / "usau.db"
        db = sqlite3.connect(usau)
        db.executescript("""
          CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            ambiguous INTEGER NOT NULL
          );
          CREATE TABLE events (event_id INTEGER PRIMARY KEY, season INTEGER NOT NULL);
          CREATE TABLE event_teams (
            event_team_id TEXT PRIMARY KEY,
            event_id INTEGER NOT NULL
          );
          CREATE TABLE roster_players (
            event_team_id TEXT NOT NULL,
            player_id INTEGER NOT NULL
          );
          INSERT INTO players VALUES (1,'Alice Example',0),(2,'Emile Test',0);
          INSERT INTO events VALUES (1,2024);
          INSERT INTO event_teams VALUES ('team',1);
          INSERT INTO roster_players VALUES ('team',1),('team',2);
        """)
        db.commit()
        db.close()
        report = measure_overlap(self.path, usau)
        self.assertFalse(report["stable_id_comparison_available"])
        self.assertEqual(0, report["stable_id_matches"])
        self.assertEqual(1, report["exact_casefolded"]["shared_name_keys"])
        self.assertEqual(2, report["diacritic_folded"]["shared_name_keys"])
        self.assertEqual(1, report["additional_diacritic_folded_shared_keys"])

    def test_european_player_ids_are_stable_negative_and_javascript_safe(self):
        first = european_player_id("alice example")
        self.assertEqual(first, european_player_id("alice example"))
        self.assertNotEqual(first, european_player_id("bob example"))
        self.assertLess(first, 0)
        self.assertGreaterEqual(first, -(2**53 - 1))
        self.assertLess(first % 32, 32)

    def test_external_inputs_publish_the_latest_team_roster(self):
        older = EuropeanInputs(team_rosters={
            (2026, "club-mixed"): (
                {"hybrid": [1]},
                {"hybrid": ("EUCS Ranking 2026 season roster", "2026-08-07")},
                {"hybrid": "Hybrid"},
            ),
        })
        newer = EuropeanInputs(team_rosters={
            (2026, "club-mixed"): (
                {"hybrid": [1, 2]},
                {"hybrid": ("WFDF WUCC 2026", "2026-08-15")},
                {"hybrid": "Hybrid"},
            ),
        })
        merged = merge_inputs(older, newer)
        rosters, sources, display = merged.team_rosters[
            (2026, "club-mixed")
        ]
        self.assertEqual([1, 2], rosters["hybrid"])
        self.assertEqual(
            ("WFDF WUCC 2026", "2026-08-15"), sources["hybrid"]
        )
        self.assertEqual("Hybrid", display["hybrid"])

    def test_reviewed_wfdf_alias_targets_one_usau_identity(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
          CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            ambiguous INTEGER NOT NULL
          );
          INSERT INTO players VALUES (12957,'AJ Merriman',0);
        """)
        alias_path = Path(self.temp.name) / "wfdf_player_aliases.csv"
        alias_path.write_text(
            "source_name,usau_name\nAlexander Merriman,AJ Merriman\n"
        )
        aliases = _load_manual_usa_aliases(db, alias_path)
        db.close()

        self.assertEqual(
            (12957, "AJ Merriman"),
            aliases["alexander merriman"],
        )
        self.assertEqual(
            (12957, "AJ Merriman", "usau-alias"),
            _usa_assignment(
                "Alexander Merriman", set(), aliases, {}, {},
            ),
        )
        self.assertIsNone(_usa_assignment(
            "Alexander Merriman", {"alexandermerriman"}, aliases, {}, {},
        ))

    def test_reviewed_wfdf_alias_can_disambiguate_by_team(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
          CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            ambiguous INTEGER NOT NULL
          );
          CREATE TABLE event_teams (
            event_team_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL
          );
          CREATE TABLE roster_players (
            event_team_id TEXT NOT NULL,
            name TEXT NOT NULL,
            player_id INTEGER NOT NULL
          );
          INSERT INTO players VALUES (1,'Mac Hecht',1),(2,'Mac Hecht',1);
          INSERT INTO event_teams VALUES ('a','DiG'),('b','Revolver');
          INSERT INTO roster_players VALUES
            ('a','Mac Hecht',1),('b','Mac Hecht',2);
        """)
        alias_path = Path(self.temp.name) / "wfdf_player_aliases.csv"
        alias_path.write_text(
            "source_name,usau_name,usau_team\n"
            "Malcolm Hecht,Mac Hecht,Revolver\n"
        )

        aliases = _load_manual_usa_aliases(db, alias_path)
        db.close()

        self.assertEqual(
            (2, "Mac Hecht"),
            aliases["malcolm hecht"],
        )

    def test_rating_bridge_requires_unique_same_season_noncolliding_name(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
          CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            ambiguous INTEGER NOT NULL
          );
          CREATE TABLE events (event_id INTEGER PRIMARY KEY, season INTEGER NOT NULL);
          CREATE TABLE event_teams (
            event_team_id TEXT PRIMARY KEY,
            event_id INTEGER NOT NULL
          );
          CREATE TABLE roster_players (
            event_team_id TEXT NOT NULL,
            player_id INTEGER NOT NULL
          );
          INSERT INTO players VALUES
            (1,'Alice Example',0),(2,'Bob Example',0),(3,'Carol Example',0),
            (4,'Ambiguous Example',1),(5,'Daan De Marrée',0),
            (6,'Tobe Decraene',0);
          INSERT INTO events VALUES (10,2025),(11,2024);
          INSERT INTO event_teams VALUES ('now',10),('old',11);
          INSERT INTO roster_players VALUES
            ('now',1),('now',2),('old',3),('now',4),('now',5),('now',6);
        """)
        bridges, audit = _usa_bridge_candidates(
            db,
            {
                "alice example": {"Alice Example"},
                "bob example": {"Bob Example"},
                "carol example": {"Carol Example"},
                "ambiguous example": {"Ambiguous Example"},
                "daan de marrée": {"Daan De Marrée"},
                "daan demarree": {"Daan DeMarree"},
                "tobe decraene": {"Tobe Decraene"},
            },
            {
                "alice example": {2025},
                "bob example": {2025},
                "carol example": {2025},
                "ambiguous example": {2025},
                "daan de marrée": {2024, 2025},
                "daan demarree": {2025},
                "tobe decraene": {2025},
            },
            {"bobexample"},
        )
        db.close()
        self.assertEqual({
            "alice example": 1,
            "daan de marrée": 5,
            "daan demarree": 5,
            "tobe decraene": 6,
        }, bridges)
        by_name = {row["usau_name"]: row for row in audit}
        self.assertEqual("exact", by_name["Alice Example"]["match_method"])
        self.assertEqual("compact", by_name["Daan De Marrée"]["match_method"])
        self.assertEqual(
            "daan de marrée;daan demarree",
            by_name["Daan De Marrée"]["eu_name_keys"],
        )
        self.assertEqual([2025], by_name["Tobe Decraene"]["shared_seasons"])


if __name__ == "__main__":
    unittest.main()
