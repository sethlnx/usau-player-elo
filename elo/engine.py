"""Player-level Elo engine.

Team rating = softmax-weighted mean of rostered players' Elos (weight rises
with rating: better players are assumed to see more playing time). The game
delta D is applied to every rostered player EQUALLY — weighting affects only
how team strength is computed, never how credit is distributed. Since the
weights are normalized, a uniform bump of D moves the weighted mean by
exactly D.

Provisional players (first `provisional_games` games) take an amplified
version of the same delta so newcomers converge faster.

Debut initialization is BASE-RATED: a first-time player starts at their
division's base — college lower than club. Because college and club teams
never play each other, that division gap cannot emerge from results; the base
is where the "college is a lower level" prior is injected. The provisional
multiplier then converges newcomers quickly. (The earlier context-based init —
teammate mean minus `rookie_discount` — remains available via
`EloConfig.context_init` for comparison.)
"""

import math
from dataclasses import dataclass, field


@dataclass
class EloConfig:
    base_rating: float = 1500.0        # generic fallback (post-replay lookups)
    division_bases: dict = field(default_factory=lambda: {
        "club": 1500.0,
        "college": 1300.0,             # prior: college is a weaker level; tuned
        "college-d3": 1100.0,          # prior: D-III below D-I; tuned
        "ufa": 1550.0,                 # prior: UFA rosters skew elite-club
    })
    k_scale: dict = field(default_factory=dict)  # per-division K multiplier
    context_init: bool = False         # True: debut at teammate mean - discount
    rookie_discount: float = 50.0      # only used when context_init is True
    k: float = 40.0                    # team-level K per game
    scale: float = 400.0               # logistic scale (fallback)
    # NOT a home field: ultimate is played at neutral tournament sites. This is
    # the bonus for being the team USAU lists first, which is the seed — listed
    # -first wins 70.1% of club games, 78-81% in pools A-D but only ~62% by the
    # semis. PUBLISHED sets it to 0: it imports USAU's seeding judgement into a
    # results-only model, and it is worthless on recent data (paired holdout
    # Δlogloss -0.00003, CI [-0.00211, +0.00197]). Its entire value was in the
    # cold-start seasons, where the ratings knew nothing and the seed did. Kept
    # as a knob because it is credited in BOTH prediction and update; omitted
    # from the update it would feed rating to whoever is listed first, who is
    # systematically the stronger team. See analysis/rankings.py PUBLISHED.
    home_advantage: float = 0.0
    # Per-division logistic scale. The divisions never meet, and their
    # rating-difference -> win-probability curves have different slopes
    # (fit: ~290 club, ~350 college), so one global scale cannot serve both.
    division_scale: dict = field(default_factory=dict)
    tau: float = 150.0                 # softmax temperature; math.inf = plain mean
    mov_norm: float = 7.0              # margin where the MOV multiplier hits 1.0
    use_mov: bool = True
    # Newcomer responsiveness. `provisional_multiplier` (M) is the excess K a
    # debutant gets; `provisional_games` (N) is its scale. `provisional_shape`
    # picks how M decays to 1:
    #   "cliff"       M flat for N games, then 1 — a discontinuity, so a rating
    #                 slews into place and then freezes dead at game N.
    #   "linear"      M ramping down to 1 at game N.
    #   "exponential" 1 + (M-1)*exp(-games/N). Integrates to the SAME total
    #                 excess credit as the cliff, (M-1)*N, so it is the
    #                 apples-to-apples smooth counterpart: same budget, spread
    #                 front-loaded instead of paid flat then cut off.
    #   "hyperbolic"  1 + (M-1)*N/(N+games). Heaviest tail — the integral
    #                 diverges logarithmically, so veterans keep a residual
    #                 excess forever (at M=6, N=20: still 1.31x at 300 games).
    provisional_games: int = 10
    provisional_multiplier: float = 2.0
    provisional_shape: str = "cliff"
    offseason_regression: float = 0.15  # fraction pulled toward base between seasons
    # Per-player stat lines (G/A/D/T at stat-reporting events), ingested only
    # after an event ends. Mechanism A splits each game delta by usage; B
    # transfers rating zero-sum between teammates by net stat quality.
    involvement_credit: bool = False    # A: weight deltas by usage index
    involvement_shrink: float = 4.0     # pseudo-events of neutral 1.0 usage
    involvement_clamp: tuple = (0.25, 3.0)
    stat_transfer_beta: float = 0.0     # B: Elo per net-stat vs team mean (0 = off)
    stat_transfer_clamp: float = 60.0   # per-event cap on a B transfer


@dataclass
class PlayerState:
    rating: float
    games: int = 0
    division: str = "club"  # last division played; offseason regression target
    inv_sum: float = 0.0    # sum of observed usage indices (1.0 = avg teammate)
    inv_events: int = 0


class PlayerElo:
    def __init__(self, config: EloConfig | None = None):
        self.cfg = config or EloConfig()
        self.players: dict = {}

    def _state(self, pid) -> PlayerState:
        if pid not in self.players:
            self.players[pid] = PlayerState(self.cfg.base_rating)
        return self.players[pid]

    def _materialize(self, roster: list, division: str):
        """Create ratings for a game's debut players.

        Debuts start at the division base. With cfg.context_init they instead
        start at the mean of already-rated teammates minus the rookie discount
        (falling back to the division base). Must run for both rosters BEFORE
        any team_rating call for the game.
        """
        start = self.cfg.division_bases.get(division, self.cfg.base_rating)
        if self.cfg.context_init:
            rated = [self.players[p].rating for p in roster if p in self.players]
            if rated:
                start = sum(rated) / len(rated) - self.cfg.rookie_discount
        for pid in roster:
            if pid not in self.players:
                self.players[pid] = PlayerState(start, division=division)

    def team_rating(self, roster: list) -> float:
        """Softmax-weighted mean of the roster's ratings."""
        ratings = [self._state(p).rating for p in roster]
        if not ratings:
            return self.cfg.base_rating
        if math.isinf(self.cfg.tau):
            return sum(ratings) / len(ratings)
        m = max(ratings)
        weights = [math.exp((r - m) / self.cfg.tau) for r in ratings]
        total = sum(weights)
        return sum(w * r for w, r in zip(weights, ratings)) / total

    def pregame_ratings(self, home_roster: list, away_roster: list,
                        division: str = "club") -> tuple[float, float]:
        """(home, away) team ratings as play_game is about to see them.

        Materializes both rosters first. team_rating on its own would reach a
        debutant through _state and create him at the GLOBAL base instead of
        his division's — which is exactly what _materialize's docstring forbids
        reading around, and it silently moves every rating downstream.
        """
        self._materialize(home_roster, division)
        self._materialize(away_roster, division)
        return self.team_rating(home_roster), self.team_rating(away_roster)

    def expect(self, ra: float, rb: float, division: str = "club") -> float:
        scale = self.cfg.division_scale.get(division, self.cfg.scale)
        return 1.0 / (1.0 + 10 ** ((rb - ra) / scale))

    def mov_multiplier(self, margin: int) -> float:
        if not self.cfg.use_mov or margin <= 0:
            return 1.0
        return math.log1p(margin) / math.log1p(self.cfg.mov_norm)

    def play_game(self, home_roster: list, away_roster: list,
                  home_score: int, away_score: int,
                  division: str = "club") -> float:
        """Update ratings; returns the pre-game P(home wins)."""
        self._materialize(home_roster, division)
        self._materialize(away_roster, division)
        ra = self.team_rating(home_roster) + self.cfg.home_advantage
        rb = self.team_rating(away_roster)
        expected = self.expect(ra, rb, division)
        if home_score == away_score:
            outcome = 0.5
        else:
            outcome = 1.0 if home_score > away_score else 0.0
        margin = abs(home_score - away_score)
        k = self.cfg.k * self.cfg.k_scale.get(division, 1.0)
        delta = k * self.mov_multiplier(margin) * (outcome - expected)
        self._apply(home_roster, delta, division)
        self._apply(away_roster, -delta, division)
        return expected

    def _usage(self, st: PlayerState) -> float:
        """Shrunk, clamped usage estimate: neutral 1.0 until stats say otherwise."""
        lo, hi = self.cfg.involvement_clamp
        k0 = self.cfg.involvement_shrink
        u = (k0 + st.inv_sum) / (k0 + st.inv_events)
        return min(max(u, lo), hi)

    def observe_stats(self, entries: list):
        """Ingest one team-event's stat lines: [(pid, usage_index, quality), ...].

        Callers must only feed events that have already ended (walk-forward).
        Updates usage history (mechanism A); when stat_transfer_beta > 0, also
        transfers rating zero-sum between the listed teammates by net quality
        (mechanism B) — re-centered after clamping so team totals never move.
        """
        for pid, index, _q in entries:
            st = self._state(pid)
            st.inv_sum += index
            st.inv_events += 1
        beta = self.cfg.stat_transfer_beta
        if beta > 0 and len(entries) > 1:
            mean_q = sum(q for _, _, q in entries) / len(entries)
            cap = self.cfg.stat_transfer_clamp
            ts = [min(max(beta * (q - mean_q), -cap), cap) for _, _, q in entries]
            recenter = sum(ts) / len(ts)
            for (pid, _, _), t in zip(entries, ts):
                self.players[pid].rating += t - recenter

    def provisional(self, games: int) -> float:
        """Excess K multiplier for a player with `games` games played."""
        m, n = self.cfg.provisional_multiplier, self.cfg.provisional_games
        if m <= 1.0 or n <= 0:
            return 1.0
        shape = self.cfg.provisional_shape
        if shape == "cliff":
            return m if games < n else 1.0
        if shape == "linear":
            return max(1.0, m - (m - 1.0) * games / n)
        if shape == "exponential":
            return 1.0 + (m - 1.0) * math.exp(-games / n)
        if shape == "hyperbolic":
            return 1.0 + (m - 1.0) * n / (n + games)
        raise ValueError(f"unknown provisional_shape {shape!r}")

    def _apply(self, roster: list, delta: float, division: str):
        if self.cfg.involvement_credit and len(roster) > 1:
            usages = [self._usage(self._state(p)) for p in roster]
            mean_u = sum(usages) / len(usages)
            mults = [u / mean_u for u in usages]   # roster mean stays exactly 1
        else:
            mults = [1.0] * len(roster)
        for pid, m in zip(roster, mults):
            st = self._state(pid)
            prov = self.provisional(st.games)
            st.rating += delta * m * prov
            st.games += 1
            st.division = division

    def new_season(self):
        """Regress every player toward their division's base between seasons.

        Regressing toward the generic base would pull the whole college pool
        up ~(1500-1300)*reg per season — a built-in inflation with no signal
        behind it — so the target is the base of the division last played.
        """
        reg = self.cfg.offseason_regression
        if reg <= 0:
            return
        for st in self.players.values():
            base = self.cfg.division_bases.get(st.division, self.cfg.base_rating)
            st.rating = base + (1 - reg) * (st.rating - base)


class TeamElo:
    """Baseline: plain team-level Elo keyed on any hashable team identity."""

    def __init__(self, config: EloConfig | None = None):
        self.cfg = config or EloConfig()
        self.inner = PlayerElo(self.cfg)

    def play_game(self, home_key, away_key, home_score, away_score,
                  division: str = "club") -> float:
        return self.inner.play_game([home_key], [away_key],
                                    home_score, away_score, division)

    def new_season(self):
        self.inner.new_season()

    @property
    def teams(self):
        return self.inner.players
