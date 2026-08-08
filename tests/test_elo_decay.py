import unittest

from elo.engine import EloConfig, PlayerElo, PlayerState


class EloDecayTests(unittest.TestCase):
    def test_inactivity_decay_compounds_toward_last_division_base(self):
        model = PlayerElo(EloConfig(
            inactivity_decay=0.5,
            inactivity_grace_days=0,
            division_bases={"college": 1250.0, "club-men": 1500.0},
        ))
        model.players[1] = PlayerState(
            rating=1650.0, games=100, division="college",
            last_date="2025-01-01",
        )

        model.age_players([[1]], "club-men", "2026-01-01")

        self.assertAlmostEqual(1450.0, model.players[1].rating, delta=0.2)
        self.assertEqual("2026-01-01", model.players[1].last_date)

    def test_grace_window_and_same_day_are_idempotent(self):
        model = PlayerElo(EloConfig(
            inactivity_decay=0.5, inactivity_grace_days=90,
        ))
        model.players[1] = PlayerState(
            rating=1700.0, games=100, last_date="2026-01-01",
        )

        model.age_players([[1]], "club-men", "2026-04-01")
        after_grace = model.players[1].rating
        model.age_players([[1]], "club-men", "2026-04-01")

        self.assertEqual(1700.0, after_grace)
        self.assertEqual(after_grace, model.players[1].rating)

    def test_invalid_decay_is_rejected(self):
        model = PlayerElo(EloConfig(inactivity_decay=1.1))

        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            model.age_players([[1]], "club-men", "2026-01-01")


if __name__ == "__main__":
    unittest.main()
