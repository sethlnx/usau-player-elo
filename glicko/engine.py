"""Player-level Glicko-2 adapted to event-roster team competition.

A tournament is one rating period. Every game at that event is predicted from
its opening ratings, then each rostered player receives one batch Glicko-2
update from all of the event's results. Team strength is the same
softmax-weighted player mean used by the Elo model. Team RD is the uncertainty
of that weighted mean. Calendar gaps expand RD lazily in 30-day periods.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

GLICKO_SCALE = 173.7178
GLICKO_CENTER = 1500.0


@dataclass
class Glicko2Config:
    base_rating: float = 1500.0
    division_bases: dict[str, float] = field(default_factory=dict)
    initial_rd: float = 350.0
    initial_volatility: float = 0.06
    tau: float = 0.5
    convergence: float = 1e-6
    rating_period_days: float = 30.0
    team_weight_tau: float = 500.0
    involvement_credit: bool = True
    involvement_shrink: float = 4.0
    involvement_clamp: tuple[float, float] = (0.25, 3.0)


@dataclass
class PlayerState:
    rating: float
    rd: float
    volatility: float
    games: int = 0
    division: str = "club-men"
    last_date: str | None = None
    inv_sum: float = 0.0
    inv_events: int = 0


class PlayerGlicko2:
    def __init__(self, config: Glicko2Config | None = None):
        self.cfg = config or Glicko2Config()
        self.players: dict = {}
        self._prepared: dict = {}

    def _base(self, division: str) -> float:
        return self.cfg.division_bases.get(division, self.cfg.base_rating)

    def _materialize(self, roster: list, division: str) -> None:
        for player_id in roster:
            if player_id not in self.players:
                self.players[player_id] = PlayerState(
                    self._base(division), self.cfg.initial_rd,
                    self.cfg.initial_volatility, division=division,
                )

    @staticmethod
    def _as_date(value: str) -> date:
        return date.fromisoformat(value[:10])

    def prepare_event(self, rosters: list[list], division: str, event_date: str) -> None:
        """Materialize and lazily age every participant to this rating period."""
        self._prepared = {}
        for roster in rosters:
            self._materialize(roster, division)
            for player_id in roster:
                if player_id in self._prepared:
                    continue
                state = self.players[player_id]
                periods = 1.0
                if state.last_date is not None:
                    days = max(0, (self._as_date(event_date) - self._as_date(state.last_date)).days)
                    periods = max(1.0, days / self.cfg.rating_period_days)
                phi = state.rd / GLICKO_SCALE
                base_phi = math.sqrt(
                    phi * phi + state.volatility * state.volatility * max(0.0, periods - 1.0)
                )
                prediction_phi = math.sqrt(
                    base_phi * base_phi + state.volatility * state.volatility
                )
                self._prepared[player_id] = (base_phi, prediction_phi)

    def _prediction_rd(self, player_id) -> float:
        prepared = self._prepared.get(player_id)
        if prepared is None:
            return self.players[player_id].rd
        return prepared[1] * GLICKO_SCALE

    def _weights(self, roster: list) -> list[float]:
        ratings = [self.players[player_id].rating for player_id in roster]
        if not ratings:
            return []
        tau = self.cfg.team_weight_tau
        if math.isinf(tau):
            return [1.0 / len(ratings)] * len(ratings)
        maximum = max(ratings)
        raw = [math.exp((rating - maximum) / tau) for rating in ratings]
        total = sum(raw)
        return [weight / total for weight in raw]

    def team_snapshot(self, roster: list) -> tuple[float, float]:
        if not roster:
            return self.cfg.base_rating, self.cfg.initial_rd
        weights = self._weights(roster)
        rating = sum(
            weight * self.players[player_id].rating
            for weight, player_id in zip(weights, roster)
        )
        rd = math.sqrt(sum(
            (weight * self._prediction_rd(player_id)) ** 2
            for weight, player_id in zip(weights, roster)
        ))
        return rating, rd

    def team_rating(self, roster: list) -> float:
        if not roster:
            return self.cfg.base_rating
        return self.team_snapshot(roster)[0]

    def pregame_ratings(
        self, home_roster: list, away_roster: list,
        division: str = "club-men",
    ) -> tuple[float, float]:
        self._materialize(home_roster, division)
        self._materialize(away_roster, division)
        return self.team_rating(home_roster), self.team_rating(away_roster)

    @staticmethod
    def _g(phi: float) -> float:
        return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))

    def expect_teams(
        self, home: tuple[float, float], away: tuple[float, float],
    ) -> float:
        difference = (home[0] - away[0]) / GLICKO_SCALE
        combined_phi = math.hypot(home[1], away[1]) / GLICKO_SCALE
        return 1.0 / (1.0 + math.exp(-self._g(combined_phi) * difference))

    def _usage(self, state: PlayerState) -> float:
        if not self.cfg.involvement_credit:
            return 1.0
        lo, hi = self.cfg.involvement_clamp
        shrink = self.cfg.involvement_shrink
        value = (shrink + state.inv_sum) / (shrink + state.inv_events)
        return min(max(value, lo), hi)

    def roster_usage(self, roster: list) -> dict:
        usages = [self._usage(self.players[player_id]) for player_id in roster]
        mean = sum(usages) / len(usages) if usages else 1.0
        return {
            player_id: usage / mean
            for player_id, usage in zip(roster, usages)
        }

    def observe_stats(self, entries: list) -> None:
        for player_id, involvement, _quality in entries:
            if player_id not in self.players:
                self.players[player_id] = PlayerState(
                    self.cfg.base_rating, self.cfg.initial_rd,
                    self.cfg.initial_volatility,
                )
            state = self.players[player_id]
            state.inv_sum += involvement
            state.inv_events += 1

    def _new_volatility(
        self, phi: float, sigma: float, variance: float, delta: float,
    ) -> float:
        a = math.log(sigma * sigma)
        tau = self.cfg.tau

        def objective(x: float) -> float:
            ex = math.exp(x)
            numerator = ex * (delta * delta - phi * phi - variance - ex)
            denominator = 2.0 * (phi * phi + variance + ex) ** 2
            return numerator / denominator - (x - a) / (tau * tau)

        lower = a
        if delta * delta > phi * phi + variance:
            upper = math.log(delta * delta - phi * phi - variance)
        else:
            step = 1
            upper = a - step * tau
            while objective(upper) < 0.0:
                step += 1
                upper = a - step * tau
        f_lower, f_upper = objective(lower), objective(upper)
        while abs(upper - lower) > self.cfg.convergence:
            candidate = lower + (lower - upper) * f_lower / (f_upper - f_lower)
            f_candidate = objective(candidate)
            if f_candidate * f_upper <= 0.0:
                lower, f_lower = upper, f_upper
            else:
                f_lower /= 2.0
            upper, f_upper = candidate, f_candidate
        return math.exp(lower / 2.0)

    def update_player(
        self,
        player_id,
        observations: list[tuple[float, float, float, float]],
        division: str,
        event_date: str,
        game_count: int,
    ) -> None:
        state = self.players[player_id]
        base_phi = self._prepared[player_id][0]
        mu = (state.rating - GLICKO_CENTER) / GLICKO_SCALE
        total_information = 0.0
        score_residual = 0.0
        for opponent_rating, opponent_rd, score, weight in observations:
            opponent_mu = (opponent_rating - GLICKO_CENTER) / GLICKO_SCALE
            opponent_phi = opponent_rd / GLICKO_SCALE
            g = self._g(opponent_phi)
            expected = 1.0 / (1.0 + math.exp(-g * (mu - opponent_mu)))
            total_information += weight * g * g * expected * (1.0 - expected)
            score_residual += weight * g * (score - expected)
        if total_information <= 0.0:
            return
        variance = 1.0 / total_information
        delta = variance * score_residual
        volatility = self._new_volatility(
            base_phi, state.volatility, variance, delta,
        )
        phi_star = math.sqrt(base_phi * base_phi + volatility * volatility)
        phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / variance)
        mu += phi * phi * score_residual
        state.rating = GLICKO_CENTER + GLICKO_SCALE * mu
        state.rd = GLICKO_SCALE * phi
        state.volatility = volatility
        state.games += game_count
        state.division = division
        state.last_date = event_date

    def advance_to(self, final_date: str) -> None:
        """Expand current RDs through the final observation date without games."""
        target = self._as_date(final_date)
        for state in self.players.values():
            if state.last_date is None:
                continue
            days = max(0, (target - self._as_date(state.last_date)).days)
            periods = days / self.cfg.rating_period_days
            phi = state.rd / GLICKO_SCALE
            state.rd = GLICKO_SCALE * math.sqrt(
                phi * phi + state.volatility * state.volatility * periods
            )
            state.last_date = final_date

    def new_season(self) -> None:
        """Glicko-2 uncertainty drift is calendar-based; no rating regression."""
