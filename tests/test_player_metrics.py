import csv
import tempfile
import unittest
from pathlib import Path

from analysis.player_metrics import (
    MODEL_VERSION,
    OUTPUT_FIELDS,
    REFERENCE_SEASONS,
    STAT_FIELDS,
    build_snapshot,
    fit_reference,
    write_snapshot,
)


class PlayerMetricTests(unittest.TestCase):
    def stat_row(self, pid, season, ability, exposure=1.0):
        row = {
            "ufa_player_id": f"u{pid}", "player_id": pid, "season": season,
            "stats_through": f"{season}-08-01",
            **{field: 0.0 for field in STAT_FIELDS},
        }
        attempts = 100 * exposure
        total_points = 150 * exposure
        o_points = 100 * exposure
        d_points = 50 * exposure
        row.update({
            "throwattempts": attempts,
            "completions": attempts * (0.85 + 0.01 * ability),
            "throwaways": attempts * (0.12 - 0.01 * ability),
            "stalls": exposure,
            "hucksattempted": 20 * exposure,
            "huckscompleted": (6 + ability) * exposure,
            "yardsthrown": (450 + 20 * ability) * exposure,
            "goals": (5 + ability) * exposure,
            "assists": (4 + ability) * exposure,
            "hockeyassists": 2 * exposure,
            "yardsreceived": (400 + 30 * ability) * exposure,
            "opointsplayed": o_points,
            "dpointsplayed": d_points,
            "oopportunities": 80 * exposure,
            "oopportunityscores": (35 + 3 * ability) * exposure,
            "opointsscored": (50 + 3 * ability) * exposure,
            "blocks": (2 + ability) * exposure,
            "dopportunities": 60 * exposure,
            "dopportunitystops": (20 + 2 * ability) * exposure,
            "dpointsscored": (10 + 2 * ability) * exposure,
            "secondsplayed": total_points * 60,
        })
        return row

    def setUp(self):
        self.reference_rows = []
        self.players = {}
        self.roles = {}
        roles = ("handler", "hybrid", "cutter")
        for i in range(60):
            pid = i + 1
            season = REFERENCE_SEASONS[i % len(REFERENCE_SEASONS)]
            role = roles[i // 20]
            self.reference_rows.append(self.stat_row(pid, season, i % 10))
            self.players[pid] = {
                "player": f"Reference {pid}", "elo": str(1500 + 10 * i),
                "sigma": "60", "lo90": "1400", "hi90": "1600",
            }
            self.roles[(pid, season)] = {
                "season": str(season), "role": role,
                "confidence": "0.8", "source": "ufa",
            }
        self.reference = fit_reference(
            self.reference_rows, self.players, self.roles, fitted_at="2026-01-01",
        )

    def current_row(self, pid, quality, exposure=1.0):
        row = {
            "ufa_player_id": f"u{pid}", "player_id": pid, "season": 2026,
            "stats_through": "2026-08-08",
            **{field: 0.0 for field in STAT_FIELDS},
        }
        attempts = 100 * exposure
        total_points = 150 * exposure
        o_points = 100 * exposure
        d_points = 50 * exposure
        if quality == "good":
            completion, errors, hucks, yards = 0.98, 0.01, 0.90, 10.0
            action_rate, conversion, receiving, o_success = 0.50, 0.80, 10.0, 0.80
            block_rate, stop_rate, d_success = 0.20, 0.80, 0.60
        else:
            completion, errors, hucks, yards = 0.75, 0.20, 0.20, 2.0
            action_rate, conversion, receiving, o_success = 0.05, 0.20, 2.0, 0.20
            block_rate, stop_rate, d_success = 0.01, 0.20, 0.10
        row.update({
            "throwattempts": attempts,
            "completions": attempts * completion,
            "throwaways": attempts * errors,
            "hucksattempted": 20 * exposure,
            "huckscompleted": 20 * exposure * hucks,
            "yardsthrown": attempts * yards,
            "opointsplayed": o_points,
            "dpointsplayed": d_points,
            "goals": total_points * action_rate / 2,
            "assists": total_points * action_rate / 2,
            "oopportunities": 80 * exposure,
            "oopportunityscores": 80 * exposure * conversion,
            "yardsreceived": total_points * receiving,
            "opointsscored": o_points * o_success,
            "blocks": d_points * block_rate,
            "dopportunities": 60 * exposure,
            "dopportunitystops": 60 * exposure * stop_rate,
            "dpointsscored": d_points * d_success,
            "secondsplayed": total_points * 60,
        })
        return row

    def add_current_player(self, pid, name, elo):
        self.players[pid] = {
            "player": name, "elo": str(elo), "sigma": "55",
            "lo90": str(elo - 90), "hi90": str(elo + 90),
        }
        self.roles[(pid, 2026)] = {
            "season": "2026", "role": "handler",
            "confidence": "0.9", "source": "ufa",
        }

    def test_scorecard_orders_supported_attributes_without_changing_ovr_rank(self):
        self.add_current_player(101, "Good", 1800)
        self.add_current_player(102, "Bad", 2000)
        rows = build_snapshot(
            self.reference_rows + [
                self.current_row(101, "good"), self.current_row(102, "bad"),
                {**self.current_row(999, "good"), "player_id": None},
            ],
            self.players, self.roles, self.reference, 2026, "2026-08-14",
        )
        self.assertEqual(["Bad", "Good"], [row["player"] for row in rows])
        by_name = {row["player"]: row for row in rows}
        for attribute in ("thr", "off", "def"):
            self.assertGreater(by_name["Good"][attribute], by_name["Bad"][attribute])
        self.assertEqual(MODEL_VERSION, by_name["Good"]["model_version"])
        self.assertIn("current-season-cumulative", by_name["Good"]["coverage_flags"])

    def test_small_samples_shrink_more_toward_average(self):
        self.add_current_player(103, "High exposure", 1800)
        self.add_current_player(104, "Low exposure", 1800)
        rows = build_snapshot(
            self.reference_rows + [
                self.current_row(103, "good", 2.0),
                self.current_row(104, "good", 0.05),
            ],
            self.players, self.roles, self.reference, 2026, "2026-08-14",
        )
        by_name = {row["player"]: row for row in rows}
        self.assertGreater(
            abs(by_name["High exposure"]["thr"] - 50),
            abs(by_name["Low exposure"]["thr"] - 50),
        )
        self.assertGreater(
            by_name["High exposure"]["thr_reliability"],
            by_name["Low exposure"]["thr_reliability"],
        )

    def test_post_cutoff_season_total_cannot_enter_snapshot(self):
        self.add_current_player(107, "Future", 1800)
        future = self.current_row(107, "good")
        future["stats_through"] = "2026-08-20"
        rows = build_snapshot(
            self.reference_rows + [future], self.players, self.roles,
            self.reference, 2026, "2026-08-14",
        )
        self.assertEqual([], rows)

    def test_missing_defensive_exposure_is_null_not_average(self):
        self.add_current_player(105, "No defense", 1800)
        row = self.current_row(105, "good")
        for field in (
            "dpointsplayed", "dopportunities", "dopportunitystops",
            "dpointsscored", "blocks", "callahans",
        ):
            row[field] = 0
        result = build_snapshot(
            self.reference_rows + [row], self.players, self.roles,
            self.reference, 2026, "2026-08-14",
        )[0]
        self.assertEqual("", result["def"])
        self.assertNotIn("attribute:def", result["coverage_flags"])

    def test_snapshot_csv_has_stable_versioned_contract(self):
        self.add_current_player(106, "Writer", 1800)
        rows = build_snapshot(
            self.reference_rows + [self.current_row(106, "good")],
            self.players, self.roles, self.reference, 2026, "2026-08-14",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.csv"
            write_snapshot(path, rows)
            with path.open(newline="") as handle:
                reader = csv.DictReader(handle)
                written = list(reader)
                self.assertEqual(list(OUTPUT_FIELDS), reader.fieldnames)
        self.assertEqual("1", written[0]["rank"])
        self.assertEqual("ufa-season", written[0]["stat_source"])
        self.assertEqual("2026-01-01", written[0]["reference_fitted_at"])
        self.assertEqual("2026-08-08", written[0]["stats_through"])


if __name__ == "__main__":
    unittest.main()
