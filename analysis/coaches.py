"""Coach assignments and two walk-forward Elo ratings.

``impact`` measures results left unexplained by the pre-game roster rating.
``results`` is a conventional results-only Elo for comparison.  Both require
published coaches on both sides; an unknown staff is never treated as average.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from analysis.euf_overlap import exact_name_key

BASE_RATING = 1500.0
RESULTS_SCALE = 400.0
COACH_K = 24.0
MIN_GAMES = 5
_ROLE = re.compile(r"\(\s*(Head\s+Coach|Assistant\s+Coach|Coach)\s*\)", re.I)
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Coach:
    key: str
    name: str
    role: str


@dataclass
class CoachState:
    impact: float = BASE_RATING
    results: float = BASE_RATING
    games: int = 0


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip(" ,;/&\t\r\n")


def _unmarked_names(value: str) -> list[str]:
    """Conservative fallback for USAU's role-free coach field.

    The mirror removes list structure.  Measured role-free values are either
    one two-token name or a sequence of two-token names.  Odd or otherwise
    ambiguous token counts are omitted rather than merged into a false person.
    """
    value = _clean(value)
    if not value:
        return []
    tokens = value.split()
    if len(tokens) % 2 or len(tokens) > 8:
        return []
    return [" ".join(tokens[i:i + 2]) for i in range(0, len(tokens), 2)]


def parse_coaches(value: str | None) -> list[Coach]:
    """Parse the mirror's free-text ``TeamData(key='Coaches')`` value."""
    if not value:
        return []
    parts = _ROLE.split(value)
    parsed: list[tuple[str, str]] = []
    if len(parts) == 1:
        parsed.extend((name, "Coach") for name in _unmarked_names(parts[0]))
    else:
        # Split produces [name, role, name, role, ..., unlabelled tail].
        for i in range(1, len(parts), 2):
            name = _clean(parts[i - 1])
            if name:
                parsed.append((name, _clean(parts[i]).title()))
        if len(parts) % 2:
            parsed.extend((name, "Coach") for name in _unmarked_names(parts[-1]))

    out: list[Coach] = []
    seen: set[str] = set()
    for name, role in parsed:
        key = exact_name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(Coach(key, name, role))
    return out


def coach_value(data: Iterable[dict]) -> str | None:
    """Return the case-insensitive Coaches value from a TeamData list."""
    for item in data or ():
        if str(item.get("key") or "").casefold() == "coaches":
            return item.get("value")
    return None


def load_assignments(con: sqlite3.Connection) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Return event-team staffs and each coach's latest published spelling."""
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coach_entries'"
    ).fetchone()
    if not exists:
        return {}, {}
    staffs: dict[str, list[str]] = defaultdict(list)
    latest: dict[str, tuple[str, str]] = {}
    rows = con.execute(
        """SELECT ce.event_team_id,ce.coach_key,ce.coach_name,
                  COALESCE(ev.start_date,'')
           FROM coach_entries ce
           JOIN event_teams et USING(event_team_id)
           JOIN events ev USING(event_id)
           ORDER BY ce.event_team_id,ce.coach_key"""
    )
    for team_id, key, name, event_date in rows:
        staffs[team_id].append(key)
        if key not in latest or event_date >= latest[key][0]:
            latest[key] = (event_date, name)
    return {team: tuple(keys) for team, keys in staffs.items()}, {
        key: value[1] for key, value in latest.items()
    }


def _expected(home: float, away: float, scale: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((away - home) / scale))


def _summary(records: list[tuple], seasons: set[int] | None = None) -> dict:
    selected = [row for row in records if seasons is None or row[0] in seasons]
    out = {"n": len(selected)}
    for label, index in (("roster", 3), ("results_only", 4), ("impact", 5)):
        if not selected:
            out[label] = {"accuracy": 0.0, "brier": 0.0, "logloss": 0.0}
            continue
        probs = [min(max(row[index], 1e-12), 1.0 - 1e-12) for row in selected]
        actual = [row[6] for row in selected]
        out[label] = {
            "accuracy": sum((p - 0.5) * (y - 0.5) > 0
                            for p, y in zip(probs, actual)) / len(selected),
            "brier": sum((p - y) ** 2 for p, y in zip(probs, actual)) / len(selected),
            "logloss": -sum(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)
                            for p, y in zip(probs, actual)) / len(selected),
        }
    return out


class CoachRatings:
    """Two staff-mean Elo models updated from the same covered games."""

    def __init__(self, k: float = COACH_K):
        self.k = k
        self.states: dict[str, CoachState] = {}
        self.records: list[tuple] = []
        self.eligible_games = 0
        self.covered_games = 0

    def state(self, key: str) -> CoachState:
        if key not in self.states:
            self.states[key] = CoachState()
        return self.states[key]

    def staff_rating(self, keys: Iterable[str], field: str) -> float:
        values = [getattr(self.state(key), field) for key in keys]
        return sum(values) / len(values)

    def observe(
        self,
        *,
        season: int,
        division: str,
        event_date: str | None,
        home: Iterable[str],
        away: Iterable[str],
        home_roster: float,
        away_roster: float,
        division_scale: float,
        outcome: float,
        eligible: bool = True,
    ) -> tuple[float, float] | None:
        """Update one game; return pre-game (results, impact) probabilities."""
        if eligible:
            self.eligible_games += 1
        home = tuple(dict.fromkeys(home))
        away = tuple(dict.fromkeys(away))
        shared = set(home) & set(away)
        home = tuple(key for key in home if key not in shared)
        away = tuple(key for key in away if key not in shared)
        if not home or not away:
            return None
        self.covered_games += 1

        home_results = self.staff_rating(home, "results")
        away_results = self.staff_rating(away, "results")
        home_impact = self.staff_rating(home, "impact")
        away_impact = self.staff_rating(away, "impact")
        results_expected = _expected(home_results, away_results, RESULTS_SCALE)
        impact_expected = _expected(
            home_roster + home_impact - BASE_RATING,
            away_roster + away_impact - BASE_RATING,
            division_scale,
        )
        roster_expected = _expected(home_roster, away_roster, division_scale)
        results_delta = self.k * (outcome - results_expected)
        impact_delta = self.k * (outcome - impact_expected)
        for key in home:
            state = self.state(key)
            state.results += results_delta
            state.impact += impact_delta
            state.games += 1
        for key in away:
            state = self.state(key)
            state.results -= results_delta
            state.impact -= impact_delta
            state.games += 1
        self.records.append((season, division, event_date or "", roster_expected,
                             results_expected, impact_expected, outcome))
        return results_expected, impact_expected

    def metrics(self) -> dict:
        return {
            "model": {
                "base": BASE_RATING,
                "k": self.k,
                "results_scale": RESULTS_SCALE,
                "impact": "roster Elo plus coach deviation from 1500",
            },
            "coverage": {
                "eligible_games": self.eligible_games,
                "rated_games": self.covered_games,
                "rate": (self.covered_games / self.eligible_games
                         if self.eligible_games else 0.0),
            },
            "all": _summary(self.records),
            "held_out_2024_2025": _summary(self.records, {2024, 2025}),
        }
