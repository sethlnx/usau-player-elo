"""USAU rankings-algorithm baseline (v2.0, as published 2018-01-12).

Reimplements USA Ultimate's iterative ranking algorithm from our own game
data and evaluates it walk-forward — comparison model 4 from the project
plan, which the original results table never included.

Per game the winner earns opponent_rating + x and the loser earns
opponent_rating - x, where x depends only on the score:

    r = L / (W - 1)
    x = 125 + 475 * sin(min(1, 2*(1-r)) * 0.4*pi) / sin(0.4*pi)

(125 for every one-point game, capped at 600, cap reached iff W > 2L;
reproduces USAU's published example 15-11 -> 381.) A team's rating is the
weighted mean of its game ratings with weight = date_weight * score_weight:

    score_weight = sqrt(min(1, (W + max(L, (W-1)//2)) / 19))  # 1 iff W>=13 or W+L>=19
    date_weight  = exponential by week, first season week 0.5 -> last week 1.0

Ratings start at 1000 and iterate to a fixed point. Blowout rule: a win with
W > 2L+1 by a team rated more than 600 above its opponent is ignored for
both teams, but only while the winner keeps at least 5 other counted results.

Walk-forward evaluation: ratings are recomputed (warm-started) at every game
DATE from that season's earlier games only, then that date's games are
predicted from the rating difference via a logistic whose scale is fit on
the train seasons (2021-2023 club). Season and division pools are fully
independent (USAU resets yearly; college and club never meet).

Departures from the published spec, forced by ambiguity or by our data:
- USAU publishes weekly; we snapshot per game date. Algorithm-fair, but the
  baseline is still blind to earlier rounds of the same day, unlike the Elo
  models, which update after every game. That gap is inherent to a batch
  rating published on a cadence.
- The weekly date-weight ratio needs the season's calendar length; we take
  it from the span of the season's game dates (calendar knowledge, not
  results; only the week-over-week ratio matters in a weighted mean).
- When the 5-result floor allows only some of a winner's blowouts to be
  ignored, the spec doesn't say which; we ignore the largest rating gaps
  first.
- Teams below USAU's 10-game ranking minimum are still rated here (a
  prediction must be made for every game the other models predict).

Usage: python -m analysis.usau_baseline
"""

import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH, load_games, load_maps, metrics

BASE = 1000.0
BLOWOUT_GAP = 600.0
MIN_KEPT_RESULTS = 5
TRAIN_SEASONS = (2021, 2022, 2023)
_SIN_04PI = math.sin(0.4 * math.pi)


def game_x(w: int, l: int) -> float:
    """Rating differential earned by the winner of a W-L game."""
    if w == l:
        return 0.0
    r = l / (w - 1)
    return 125 + 475 * math.sin(min(1.0, 2 * (1 - r)) * 0.4 * math.pi) / _SIN_04PI


def score_weight(w: int, l: int) -> float:
    return math.sqrt(min(1.0, (w + max(l, (w - 1) // 2)) / 19))


class SeasonPool:
    """One (division, season)'s games prepped for the iterative rater.

    Each game becomes (home_ix, away_ix, x, weight, sign, W, L, date) with
    sign +1/-1/0 for home win/loss/tie and weight already the product of
    score weight and the exponential date weight.
    """

    def __init__(self, season_games: list[dict], clubs: dict):
        raw = sorted(season_games, key=lambda g: g["sort"])
        dates = [g["sort"][0][:10] for g in raw]
        first = min(dates)
        weeks = [self._days_between(first, d) // 7 for d in dates]
        ratio = 2 ** (1 / max(max(weeks), 1))   # first week 0.5 -> last week 1.0
        team_ix: dict = {}
        self.games = []
        for g, d, wk in zip(raw, dates, weeks):
            hi = team_ix.setdefault(clubs.get(g["home_id"], g["home_id"]), len(team_ix))
            ai = team_ix.setdefault(clubs.get(g["away_id"], g["away_id"]), len(team_ix))
            hs, as_ = g["home_score"], g["away_score"]
            w, l = max(hs, as_), min(hs, as_)
            sign = 1.0 if hs > as_ else (-1.0 if hs < as_ else 0.0)
            self.games.append((hi, ai, game_x(w, l),
                               score_weight(w, l) * ratio ** wk, sign, w, l, d))
        self.n_teams = len(team_ix)

    @staticmethod
    def _days_between(a: str, b: str) -> int:
        from datetime import date
        try:
            da = date(int(a[0:4]), int(a[5:7]), int(a[8:10]))
            db = date(int(b[0:4]), int(b[5:7]), int(b[8:10]))
            return (db - da).days
        except ValueError:
            return 0


def _converge(gs, n, r, ignored, tol=0.05, max_iter=2000):
    """Iterate rating <- weighted mean of game ratings to a fixed point."""
    for _ in range(max_iter):
        acc = [0.0] * n
        den = [0.0] * n
        for gi, (hi, ai, x, wgt, sign, _w, _l, _d) in enumerate(gs):
            if gi in ignored:
                continue
            acc[hi] += wgt * (r[ai] + sign * x)
            den[hi] += wgt
            acc[ai] += wgt * (r[hi] - sign * x)
            den[ai] += wgt
        new = [acc[i] / den[i] if den[i] else BASE for i in range(n)]
        delta = max(map(abs, (a - b for a, b in zip(new, r))), default=0.0)
        r = new
        if delta < tol:
            break
    return r


def _blowout_set(gs, r) -> set:
    """Games ignored under the blowout rule at the current ratings."""
    games_of = [0] * len(r)
    for hi, ai, *_ in gs:
        games_of[hi] += 1
        games_of[ai] += 1
    cands = []
    for gi, (hi, ai, _x, _wgt, sign, w, l, _d) in enumerate(gs):
        if sign == 0:
            continue
        win, lose = (hi, ai) if sign > 0 else (ai, hi)
        gap = r[win] - r[lose]
        if gap > BLOWOUT_GAP and w > 2 * l + 1:
            cands.append((gap, gi, win, lose))
    cands.sort(reverse=True)                 # biggest mismatches ignored first
    ignored: set = set()
    removed = [0] * len(r)                   # per-team count of ignored results
    for _gap, gi, win, lose in cands:
        if games_of[win] - removed[win] - 1 >= MIN_KEPT_RESULTS:
            ignored.add(gi)
            removed[win] += 1
            removed[lose] += 1
    return ignored


def _rate(gs, n, warm):
    """Converge ratings and the blowout-ignore set to a joint fixed point."""
    r = warm[:] if warm is not None else [BASE] * n
    ignored: set = set()
    for _ in range(10):
        r = _converge(gs, n, r, ignored)
        new = _blowout_set(gs, r)
        if new == ignored:
            break
        ignored = new
    return r


def walkforward(pool: SeasonPool, season: int, division: str) -> list:
    """Predict every game from ratings over strictly earlier dates.

    Returns (season, division, date, rating_diff, outcome) rows; the diff
    becomes a probability once the logistic scale is fit.
    """
    recs = []
    r = [BASE] * pool.n_teams
    gs = pool.games
    i = 0
    while i < len(gs):
        d = gs[i][7]
        j = i
        while j < len(gs) and gs[j][7] == d:
            j += 1
        for hi, ai, _x, _wgt, sign, _w, _l, _d in gs[i:j]:
            outcome = 0.5 if sign == 0 else (1.0 if sign > 0 else 0.0)
            recs.append((season, division, d, r[hi] - r[ai], outcome))
        r = _rate(gs[:j], pool.n_teams, warm=r)
        i = j
    return recs


def _prob(diff: float, scale: float) -> float:
    return 1.0 / (1.0 + 10 ** (-diff / scale))


def fit_scale(pairs: list) -> float:
    """Logistic scale minimizing log-loss of P(win) = 1/(1+10^(-diff/s))."""
    eps = 1e-12

    def logloss(s):
        tot = 0.0
        for d, o in pairs:
            e = _prob(d, s)
            tot -= o * math.log(max(e, eps)) + (1 - o) * math.log(max(1 - e, eps))
        return tot / len(pairs)

    coarse = min(range(50, 1501, 25), key=logloss)
    return min(range(max(30, coarse - 24), coarse + 25, 5), key=logloss)


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    games = load_games(con)
    _rosters, clubs = load_maps(con)
    con.close()
    pools: dict = {}
    for g in games:
        pools.setdefault((g["division"], g["season"]), []).append(g)
    raw = []
    for (division, season), gg in sorted(pools.items()):
        pool = SeasonPool(gg, clubs)
        raw += walkforward(pool, season, division)
        print(f"  rated {division} {season}: {len(gg)} games, {pool.n_teams} teams",
              flush=True)
    train = [(d, o) for s, dv, _dt, d, o in raw
             if s in TRAIN_SEASONS and dv == "club"]
    scale = fit_scale(train)
    print(f"\nlogistic scale fit on {TRAIN_SEASONS} club: {scale}")
    records = [(s, dv, dt, _prob(d, scale), o) for s, dv, dt, d, o in raw]

    header = f"{'slice':<26}{'n':>7}{'accuracy':>10}{'brier':>8}{'logloss':>9}"
    print("\nUSAU algorithm baseline\n" + header + "\n" + "-" * len(header))
    slices = [
        ("club 2024-25 (holdout)", {2024, 2025}, {"club"}),
        ("club 2024", {2024}, {"club"}),
        ("club 2025", {2025}, {"club"}),
        ("club 2021-23 (train)", set(TRAIN_SEASONS), {"club"}),
        ("college 2024-25*", {2024, 2025}, {"college"}),
    ]
    for label, ss, dd in slices:
        m = metrics(records, ss, dd)
        if m["n"]:
            print(f"{label:<26}{m['n']:>7}{m['accuracy']:>10.4f}"
                  f"{m['brier']:>8.4f}{m['logloss']:>9.4f}")
    print("* college rows reflect whatever college games are in the DB "
          "(scrape may be partial)")
    return records


if __name__ == "__main__":
    main()
