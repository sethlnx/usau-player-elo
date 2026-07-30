"""Player-level batch re-rating in the shape of USA Ultimate's algorithm.

The team-level baseline (`analysis/usau_baseline.py`) is faithful to USAU's
published spec and therefore inherits its two structural limits: it rates
TEAMS, and it resets every season. This module keeps USAU's rating FORM --
the self-consistent "your rating is the weighted average of the game ratings
you earned, and a game rating is your opponent's rating plus or minus the
score differential x" -- while changing three things:

1. PLAYER-LEVEL. A team-event's strength is a weighted sum of its rostered
   players' ratings; a player's rating is the weighted average of the game
   ratings earned by the team-events they were on.
2. NO SEASON RESET. USAU's exponential date weight (0.5 at the start of a
   season, 1.0 at the end -- a halving over roughly 18 weeks) keeps decaying
   across the season boundary instead of restarting, so strength carries
   into a new year through whoever is on the roster. `HALF_LIFE` defaults to
   126 days, matching USAU's own within-season decay rate.
3. STAT-WEIGHTED. Per-player usage (from the G/A/D/T stat lines that
   Nationals-level events report, plus linked UFA points-played) sets both
   how much a player counts toward team strength and how hard each result
   pulls their own rating.

Why the fixed point is well-posed
---------------------------------
USAU's "iterate thousands of times until stable" is a linear fixed point in
disguise: with weights and the x-differentials held fixed, rating = A*rating
+ b where A is row-stochastic. Going player-level preserves that. Writing
v[t,p] for strength weight (summing to 1 over a roster) and u[t,p] for credit
weight, a player's rating is

    R_p = SUM_t u[t,p] * E_t / SUM_t u[t,p] * W_t

where W_t = SUM_g w_g is the team-event's total game weight and
E_t = SUM_g w_g * (T_opp(g) + s_g * x_g) is its total earned rating, with
T_t = SUM_p v[t,p] * R_p. The coefficient of every R_q in R_p sums to
exactly SUM_t u[t,p]*W_t over the same denominator -- i.e. 1 -- so the map is
row-stochastic and the iteration converges, exactly as USAU's does.

Why a prior pull is REQUIRED (measured, not theoretical)
--------------------------------------------------------
Row-stochastic only buys spectral radius 1 -- not convergence. A first
implementation without the prior term below diverged outright: ratings spread
to -34000 and the spread grew with every successive snapshot. Two causes,
both specific to going player-level:

- The all-ones eigenvector has one copy per CONNECTED COMPONENT of the game
  graph. USAU can ignore this because one division-season is a single
  well-connected blob; a 730-day player-level window is not, so every weakly
  connected cluster carries its own unpinned additive constant that wanders.
  Re-centering the global mean pins exactly one of those constants.
- Isolated pairs oscillate with period 2 (eigenvalue -1): if A and B have
  played only each other, R_A <- R_B + x and R_B <- R_A - x flips forever.

Adding a prior of weight PRIOR_W pseudo-games at 1000 to every player,

    R_p = (PRIOR_W*1000 + SUM_t u[t,p]*E_t) / (PRIOR_W + SUM_t u[t,p]*W_t)

makes the map strictly substochastic: spectral radius < 1, unique fixed
point, guaranteed convergence, and every component anchored including the
isolated ones. It is a real departure from USAU's published spec, and it is
also the statistically right thing -- it shrinks thin-evidence players toward
the population mean instead of letting two games place someone 600 points
out. USAU gets the same protection structurally, via a single connected
division graph plus a 10-game minimum before a team is ranked at all;
neither is available per-player.

The prior also replaces the mean-pinning USAU gets for free (each game
contributes +x to the winner and -x to the loser, which cancels in
aggregate -- exactly true only when u == v, and usage-based credit breaks
that). With the prior anchoring the scale, no re-centering is needed.

Why credit weighting is safe here but not in Elo
------------------------------------------------
`elo/engine.py` distributes each game's delta equally across the roster by
default, because unequal credit in a SEQUENTIAL updater feeds back: a higher
rating earns more weight, which earns more credit, which raises the rating.
A batch solve has no trajectory to compound -- it converges to a
self-consistent equilibrium -- so usage-weighted credit is stable. The
credit weight is normalized within each roster (u = usage / roster-mean
usage, the same form as the engine's `involvement_credit`), which is what
makes it meaningful: a player-level weight alone would cancel out of the
ratio above.

Walk-forward discipline
-----------------------
At each snapshot date D the solve sees only games strictly before D, and
usage priors built only from stat events that ENDED before D (shrunk toward
a neutral 1.0, same form as `PlayerElo._usage`). An event's own stat lines
therefore never weight its own games. Predictions for D's games come from
the pre-D ratings, so every recorded probability is honestly out of sample.

Departures from the published spec, beyond the three intended changes:
- Teams whose roster failed to scrape fall back to a single synthetic
  "ghost" player keyed on club+season, mirroring the Elo backtest.
- The blowout-ignore rule's "at least N other results" floor is counted per
  CLUB over the window (USAU counts per team per season); when the floor
  allows only some blowouts to be dropped, the largest rating gaps go first.
- Division level (college vs club) is not injected as a prior the way
  `EloConfig.division_bases` does it. Here college and club share one pool
  and one mean, and any level gap has to emerge from players who appear in
  both -- a cleaner test of the bridge, and a harder one.

Usage: python -m analysis.usau_player [--half-life D] [--window D] [--quick]
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import (DB_PATH, load_games, load_maps, load_stat_events,
                               load_ufa_stat_events, metrics)
from analysis.usau_baseline import (BASE, BLOWOUT_GAP, MIN_KEPT_RESULTS,
                                    TRAIN_SEASONS, _prob, fit_scale, game_x,
                                    score_weight)

HALF_LIFE = 126.0      # days; USAU's own within-season 0.5 -> 1.0 decay rate
WINDOW_DAYS = 730      # games older than this carry no weight
SHRINK = 4.0           # pseudo-events of neutral 1.0 usage (engine parity)
CLAMP = (0.25, 3.0)
PRIOR_W = 2.0          # pseudo-games at BASE; see "Why a prior pull is REQUIRED"
TOL = 0.25             # ratings live on a ~1000 scale
MAX_ITER_COLD = 4000
MAX_ITER_WARM = 800


def _ord(d: str) -> int:
    return date(int(d[:4]), int(d[5:7]), int(d[8:10])).toordinal()


class Corpus:
    """Flat arrays over the whole game history, reused at every snapshot.

    Index sets are built once; a snapshot is expressed purely as a per-game
    weight vector that is zero outside the window, so nothing is rebuilt.
    """

    def __init__(self, con):
        games = load_games(con)
        rosters, clubs = load_maps(con)

        self.p_index: dict = {}
        te_index: dict = {}
        te_rosters: list = []
        te_club: list = []
        club_index: dict = {}

        def pi(key):
            ix = self.p_index.get(key)
            if ix is None:
                ix = self.p_index[key] = len(self.p_index)
            return ix

        def ti(etid, season):
            ix = te_index.get(etid)
            if ix is None:
                ix = te_index[etid] = len(te_index)
                club = clubs.get(etid, etid)
                roster = rosters.get(etid) or [f"ghost:{club}:{season}"]
                te_rosters.append([pi(p) for p in roster])
                ck = club_index.setdefault(club, len(club_index))
                te_club.append(ck)
            return ix

        gh, ga, xs, sw, sg, dt, ws, ls, seas, divs = [], [], [], [], [], [], [], [], [], []
        for g in games:
            d = (g.get("date") or g["sort"][0])[:10]
            try:
                o = _ord(d)
            except ValueError:
                continue
            h = ti(g["home_id"], g["season"])
            a = ti(g["away_id"], g["season"])
            hs, as_ = g["home_score"], g["away_score"]
            w, l = max(hs, as_), min(hs, as_)
            gh.append(h)
            ga.append(a)
            xs.append(game_x(w, l))
            sw.append(score_weight(w, l))
            sg.append(1.0 if hs > as_ else (-1.0 if hs < as_ else 0.0))
            dt.append(o)
            ws.append(w)
            ls.append(l)
            seas.append(g["season"])
            divs.append(g["division"])

        self.gh = np.array(gh, dtype=np.int64)
        self.ga = np.array(ga, dtype=np.int64)
        self.x = np.array(xs, dtype=np.float64)
        self.sw = np.array(sw, dtype=np.float64)
        self.sign = np.array(sg, dtype=np.float64)
        self.gdate = np.array(dt, dtype=np.int64)
        self.wscore = np.array(ws, dtype=np.int64)
        self.lscore = np.array(ls, dtype=np.int64)
        self.season = np.array(seas, dtype=np.int64)
        self.division = np.array(divs, dtype=object)

        self.te_arr = np.array([t for t, pl in enumerate(te_rosters) for _ in pl],
                               dtype=np.int64)
        self.pl_arr = np.array([p for pl in te_rosters for p in pl], dtype=np.int64)
        self.te_club = np.array(te_club, dtype=np.int64)
        self.n_p = len(self.p_index)
        self.n_te = len(te_rosters)
        self.n_clubs = len(club_index)

        # Stat events -> (end_ordinal, player_ix[], usage[]) sorted by date,
        # so usage priors can be accumulated incrementally as snapshots advance.
        raw = sorted(load_stat_events(con) + load_ufa_stat_events(con),
                     key=lambda e: e[0])
        self.stat_events = []
        for end, entries in raw:
            try:
                o = _ord(end)
            except (ValueError, TypeError):
                continue
            ix = [(self.p_index[p], usage) for p, usage, _q in entries
                  if p in self.p_index]
            if ix:
                self.stat_events.append(
                    (o, np.array([i for i, _ in ix], dtype=np.int64),
                     np.array([u for _, u in ix], dtype=np.float64)))

    def usage_weights(self, inv_sum, inv_events):
        """(v, u) per roster entry from the current shrunk usage priors.

        v is normalized to sum to 1 within a team-event (strength share); u is
        normalized to a roster mean of 1 (credit multiplier), matching
        `PlayerElo._apply`'s involvement-credit form.
        """
        prior = (SHRINK + inv_sum) / (SHRINK + inv_events)
        np.clip(prior, CLAMP[0], CLAMP[1], out=prior)
        ent = prior[self.pl_arr]
        tot = np.bincount(self.te_arr, weights=ent, minlength=self.n_te)
        cnt = np.bincount(self.te_arr, minlength=self.n_te).astype(np.float64)
        tot[tot <= 0] = 1.0
        v = ent / tot[self.te_arr]
        u = ent * cnt[self.te_arr] / tot[self.te_arr]
        return v, u


def solve(c: Corpus, w_eff, v, u, R0, max_iter, prior=PRIOR_W):
    """Iterate R <- (usage-weighted mean of earned game ratings) to a fixed point.

    w_eff is the per-game weight (0 outside the snapshot window / after the
    blowout mask). The prior term keeps the map strictly substochastic, which
    is what makes this converge at all -- see the module docstring.
    Returns (ratings, active_mask, iterations_used).
    """
    W_te = (np.bincount(c.gh, weights=w_eff, minlength=c.n_te)
            + np.bincount(c.ga, weights=w_eff, minlength=c.n_te))
    D_p = np.bincount(c.pl_arr, weights=u * W_te[c.te_arr], minlength=c.n_p)
    active = D_p > 0
    if not active.any():
        return R0, active, 0
    denom = prior + D_p
    floor = prior * BASE
    R = R0.copy()
    sx = c.sign * c.x
    used = 0
    for it in range(max_iter):
        used = it + 1
        T = np.bincount(c.te_arr, weights=v * R[c.pl_arr], minlength=c.n_te)
        E_te = (np.bincount(c.gh, weights=w_eff * (T[c.ga] + sx), minlength=c.n_te)
                + np.bincount(c.ga, weights=w_eff * (T[c.gh] - sx), minlength=c.n_te))
        N_p = np.bincount(c.pl_arr, weights=u * E_te[c.te_arr], minlength=c.n_p)
        new = (floor + N_p) / denom
        delta = np.abs(new - R).max()
        R = new
        if delta < TOL:
            break
    return R, active, used


def blowout_mask(c: Corpus, w_eff, T):
    """Games ignored under USAU's blowout rule at the current team strengths."""
    live = w_eff > 0
    gap = np.where(c.sign > 0, T[c.gh] - T[c.ga], T[c.ga] - T[c.gh])
    lopsided = c.wscore > 2 * c.lscore + 1
    cand = np.flatnonzero(live & (c.sign != 0) & (gap > BLOWOUT_GAP) & lopsided)
    if cand.size == 0:
        return np.ones(len(c.gh), dtype=bool)
    counted = (np.bincount(c.te_club[c.gh[live]], minlength=c.n_clubs)
               + np.bincount(c.te_club[c.ga[live]], minlength=c.n_clubs)).astype(np.int64)
    win_te = np.where(c.sign > 0, c.gh, c.ga)
    lose_te = np.where(c.sign > 0, c.ga, c.gh)
    keep = np.ones(len(c.gh), dtype=bool)
    for gi in cand[np.argsort(-gap[cand])]:        # biggest mismatches first
        wc, lc = c.te_club[win_te[gi]], c.te_club[lose_te[gi]]
        if counted[wc] - 1 >= MIN_KEPT_RESULTS:
            keep[gi] = False
            counted[wc] -= 1
            counted[lc] -= 1
    return keep


def rate_as_of(c: Corpus, cutoff: int, v, u, warm, half_life, window):
    """Ratings from games strictly before `cutoff`, with the blowout set and
    the ratings converged jointly (as in the team-level baseline)."""
    in_win = (c.gdate < cutoff) & (c.gdate >= cutoff - window)
    if not in_win.any():
        return warm, np.zeros(c.n_p, dtype=bool)
    decay = np.zeros(len(c.gh))
    decay[in_win] = 0.5 ** ((cutoff - c.gdate[in_win]) / half_life)
    base_w = c.sw * decay
    w_eff = base_w
    R, active = warm, None
    for _ in range(6):
        R, active, _it = solve(c, w_eff, v, u, R,
                               MAX_ITER_COLD if warm is None else MAX_ITER_WARM)
        T = np.bincount(c.te_arr, weights=v * R[c.pl_arr], minlength=c.n_te)
        keep = blowout_mask(c, base_w, T)
        new_w = base_w * keep
        if np.array_equal(new_w > 0, w_eff > 0):
            break
        w_eff = new_w
    return R, active


def run(half_life=HALF_LIFE, window=WINDOW_DAYS, quick=False):
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c = Corpus(con)
    con.close()
    print(f"corpus: {len(c.gh)} games, {c.n_p} players, {c.n_te} team-events, "
          f"{len(c.stat_events)} stat team-events", flush=True)
    print(f"half-life {half_life}d, window {window}d", flush=True)

    dates = np.unique(c.gdate)
    if quick:                       # smoke test: last two seasons of snapshots
        dates = dates[dates >= _ord("2024-01-01")]
    inv_sum = np.zeros(c.n_p)
    inv_events = np.zeros(c.n_p)
    si = 0
    R = np.full(c.n_p, BASE)
    raw = []
    order = np.argsort(c.gdate, kind="stable")
    by_date: dict = {}
    for gi in order:
        by_date.setdefault(int(c.gdate[gi]), []).append(gi)

    for n, d in enumerate(dates):
        d = int(d)
        while si < len(c.stat_events) and c.stat_events[si][0] < d:
            _o, ix, us = c.stat_events[si]
            np.add.at(inv_sum, ix, us)
            np.add.at(inv_events, ix, 1.0)
            si += 1
        v, u = c.usage_weights(inv_sum, inv_events)
        R, _active = rate_as_of(c, d, v, u, R, half_life, window)
        T = np.bincount(c.te_arr, weights=v * R[c.pl_arr], minlength=c.n_te)
        for gi in by_date.get(d, ()):
            outcome = (0.5 if c.sign[gi] == 0
                       else (1.0 if c.sign[gi] > 0 else 0.0))
            raw.append((int(c.season[gi]), c.division[gi],
                        date.fromordinal(d).isoformat(),
                        T[c.gh[gi]] - T[c.ga[gi]], outcome))
        if n % 50 == 0:
            print(f"  [{n}/{len(dates)}] {date.fromordinal(d)}", flush=True)

    train = [(diff, o) for s, dv, _dt, diff, o in raw
             if s in TRAIN_SEASONS and dv == "club"]
    if not train:
        train = [(diff, o) for _s, dv, _dt, diff, o in raw if dv == "club"]
    scale = fit_scale(train)
    print(f"\nlogistic scale fit on {TRAIN_SEASONS} club: {scale}", flush=True)
    records = [(s, dv, dt, _prob(diff, scale), o) for s, dv, dt, diff, o in raw]

    header = f"{'slice':<28}{'n':>7}{'accuracy':>10}{'brier':>8}{'logloss':>9}"
    print("\nUSAU-form player batch re-rating\n" + header + "\n" + "-" * len(header))
    for label, ss, dd in [
        ("club 2024-25 (holdout)", {2024, 2025}, {"club"}),
        ("club 2024", {2024}, {"club"}),
        ("club 2025", {2025}, {"club"}),
        ("club 2021-23 (train)", set(TRAIN_SEASONS), {"club"}),
        ("college 2024-25", {2024, 2025}, {"college"}),
    ]:
        m = metrics(records, ss, dd)
        if m["n"]:
            print(f"{label:<28}{m['n']:>7}{m['accuracy']:>10.4f}"
                  f"{m['brier']:>8.4f}{m['logloss']:>9.4f}", flush=True)
    m = metrics(records, {2024, 2025}, {"club"}, max_month=7)
    if m["n"]:
        print(f"{'club 2024-25 thru July':<28}{m['n']:>7}{m['accuracy']:>10.4f}"
              f"{m['brier']:>8.4f}{m['logloss']:>9.4f}", flush=True)
    return records


if __name__ == "__main__":
    argv = sys.argv[1:]

    def opt(name, default):
        if name in argv:
            return float(argv[argv.index(name) + 1])
        return default

    run(half_life=opt("--half-life", HALF_LIFE),
        window=opt("--window", WINDOW_DAYS),
        quick="--quick" in argv)
