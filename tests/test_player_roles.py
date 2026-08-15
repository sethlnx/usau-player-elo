import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis.player_roles import (
    RoleRecord,
    apply_overrides,
    build_player_roles,
    load_overrides,
    load_position_roles,
    load_usau_stat_roles,
    role_for,
    write_role_csv,
)


class PlayerRoleTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript("""
            CREATE TABLE events (event_id TEXT PRIMARY KEY, season INTEGER);
            CREATE TABLE event_teams (event_team_id TEXT PRIMARY KEY, event_id TEXT);
            CREATE TABLE roster_players (
                event_team_id TEXT, name TEXT, player_id INTEGER
            );
            CREATE TABLE roster_entries (
                event_team_id TEXT, name TEXT, position TEXT,
                points TEXT, assists TEXT, turns TEXT
            );
            INSERT INTO events VALUES ('e24', 2024), ('e25', 2025);
            INSERT INTO event_teams VALUES
                ('t24', 'e24'), ('t25', 'e25'), ('t25b', 'e25');
            INSERT INTO roster_players VALUES
                ('t24', 'Handler', 1), ('t24', 'Cutter', 2),
                ('t24', 'Hybrid', 3), ('t25', 'Stats Handler', 4),
                ('t25', 'Stats Cutter', 5), ('t25', 'Sparse', 6),
                ('t25b', 'Stats Handler', 4), ('t25b', 'Stats Cutter', 5);
            INSERT INTO roster_entries VALUES
                ('t24', 'Handler', 'Handler', '2', '8', '3'),
                ('t24', 'Cutter', 'Cutter', '9', '1', '0'),
                ('t24', 'Hybrid', 'Handler/Cutter', '4', '4', '1'),
                ('t25', 'Stats Handler', '', '1', '12', '8'),
                ('t25', 'Stats Cutter', '', '14', '1', '0'),
                ('t25', 'Sparse', '', '', '', ''),
                ('t25b', 'Stats Handler', '', '1', '12', '8'),
                ('t25b', 'Stats Cutter', '', '14', '1', '0');
        """)

    def tearDown(self):
        self.con.close()

    def test_role_thresholds_abstain_before_classifying(self):
        self.assertEqual("unknown", role_for(1.0, 0.24))
        self.assertEqual("handler", role_for(0.30, 0.25))
        self.assertEqual("cutter", role_for(-0.30, 0.25))
        self.assertEqual("hybrid", role_for(0.0, 0.25))

    def test_position_labels_classify_clear_profiles(self):
        records = load_position_roles(self.con)
        self.assertEqual("handler", records[(1, 2024)].role)
        self.assertEqual("cutter", records[(2, 2024)].role)
        self.assertEqual("hybrid", records[(3, 2024)].role)
        self.assertGreater(records[(1, 2024)].confidence, 0.25)

    def test_usau_goals_assists_and_turns_classify_without_positions(self):
        records = load_usau_stat_roles(self.con)
        self.assertEqual("handler", records[(4, 2025)].role)
        self.assertEqual("cutter", records[(5, 2025)].role)
        self.assertNotIn((6, 2025), records)

    def test_future_season_is_not_used_and_prior_confidence_decays(self):
        evidence = {
            (7, 2024): RoleRecord(7, 2024, "handler", 0.8, 0.8, 20, "position"),
            (7, 2027): RoleRecord(7, 2027, "cutter", -0.8, 0.9, 20, "position"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            overrides = Path(tmp) / "overrides.csv"
            overrides.write_text("player_id,season,role,note\n")
            with patch("analysis.player_roles.infer_roles", return_value=evidence):
                records, current = build_player_roles(
                    self.con, {7: 2026, 8: 2026}, overrides,
                )
        self.assertEqual("handler", current[7].role)
        self.assertAlmostEqual(0.8 * 0.75 * 0.75, current[7].confidence)
        self.assertEqual("prior-position", current[7].source)
        self.assertEqual("unknown", current[8].role)
        self.assertEqual(2026, records[(8, 2026)].season)

    def test_override_wins_and_csv_is_deterministic(self):
        base = {(9, 2025): RoleRecord(9, 2025, "cutter", -0.8, 0.7, 10, "ufa")}
        changed = apply_overrides(base, {(9, 2025): ("hybrid", "reviewed")})
        self.assertEqual("hybrid", changed[(9, 2025)].role)
        self.assertTrue(changed[(9, 2025)].overridden)
        self.assertEqual(1.0, changed[(9, 2025)].confidence)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roles.csv"
            write_role_csv(path, changed)
            with path.open() as f:
                rows = list(csv.DictReader(f))
        self.assertEqual("9", rows[0]["player_id"])
        self.assertEqual("hybrid", rows[0]["role"])
        self.assertEqual("reviewed", rows[0]["note"])

    def test_invalid_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overrides.csv"
            path.write_text("player_id,season,role,note\n1,2025,quarterback,no\n")
            with self.assertRaisesRegex(ValueError, "invalid player role override"):
                load_overrides(path)


if __name__ == "__main__":
    unittest.main()
