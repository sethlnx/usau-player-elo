"""Coordinate descent over the engine's knobs, on the three-way split.

    FIT   2017-2021   ratings accumulate here
    VAL   2022-2023   every parameter is CHOSEN here
    TEST  2024-2025   reported once, never selected on

Selection scores the tier-weighted mean logloss across every scored division,
not club men's alone. Per-division bases and scales are still scored on their
own division's games — the D-III lesson in analysis/rankings.py — since a base
only governs the pool it initialises. Division K multipliers are scored across
the whole corpus because their updates transfer through shared players.

Two knobs are deliberately NOT axes:
  low_info_anchor  It costs logloss on purpose (see EloConfig); a logloss
                   descent would drive it to zero and re-strand the ~340
                   players it exists to rescue. Pinned, and the pathology
                   counts are re-checked against the selected config.
  home_advantage   Swept as a diagnostic and reported, never auto-adopted: it
                   imports USAU's seeding into a results-only model. See the
                   note in analysis/rankings.py.

Every surviving move is then dropped one at a time, cheapest first, while the
CUMULATIVE VAL logloss handed back stays within PRUNE_EPS. That is what stops
the descent publishing a tail of moves that are really fitting VAL, without
the failure the per-move form had: eighteen individually-marginal moves each
under the threshold once added up to five times it, and the prune returned
the published config while discarding a gain a paired bootstrap over VAL put
outside noise. See prune().

Selection is scored on tier shares (TIER_SHARE), not raw game counts: each
tier owns a fixed fraction of the objective regardless of how many rows it
has. See the comment above TIER_SHARE for why n-weighting, and n scaled by a
per-tier multiplier, both failed.

Usage: python -m analysis.descent [--passes N] [--jobs N] [--quick]
                                           [--division-k-only]
"""

import itertools
import math
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import (
    DB_PATH, build_ufa_stat_events, load_games, load_maps,
    load_stat_events, load_ufa_games, load_ufa_stat_data,
    load_ufa_stat_events, metrics, replay,
)
from analysis.rankings import PUBLISHED
from womens_pro.ratings import load_womens_pro_inputs
from elo.engine import EloConfig

# FIT reaches back to 2014 with the corpus. It used to start at 2017 because
# that was where the corpus started; leaving it there would accumulate ratings
# from a cold start three seasons later than the data allows and hand VAL a
# worse model than the engine can actually build.
FIT = (2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021)
VAL = (2022, 2023)
TEST = (2024, 2025)
# Every CONTESTED division, weighted by IMPORTANCE, not by row count. Pure
# n-weighting answers "which model predicts the most games", which is not the
# question the site asks. The 2026 corpus made the gap concrete: high school
# arrived as 15,276 games and carried ZERO weight here, so the descent that
# improved the scored set by 0.00139 VAL simultaneously degraded the unscored
# divisions by 0.00624 VAL / 0.00997 TEST and had no way to see it. Twelve
# divisions regressed on TEST, hs-boys and hs-girls worst.
#
# Scaling n by a per-tier multiplier does not fix that. It was tried: at a
# 0.3 multiplier high school is third in name and 4.0% of the objective in
# fact, because n_d swamps the multiplier — most high school games postdate
# the VAL window. Tuning the multiplier until it changed the answer would be
# choosing the objective to produce a result already decided on, the same
# error as selecting on TEST.
#
# So importance is stated directly. Each TIER owns a fixed share of the
# objective, divided evenly across that tier's games in whatever window is
# being scored. A tier's influence is then independent of how many rows it
# happens to have, which is the point: club is half the objective whether it
# played 12,000 games or 2,000. Shares are normalised over the tiers actually
# present in the window, so a tier with no games costs nothing.
#
# PUBLISHED assigns a scale/base to 53 divisions total; euf-open/euf-mixed/
# euf-women are excluded on purpose (joined after tuning — see the note above
# PUBLISHED in rankings.py), and greatgrandmasters-mixed has never been played.
# Every other PUBLISHED division is covered below even where it currently has
# zero rows in the corpus (league-men/league-mixed, PUL/WUL, several beach
# brackets, ms-girls, ycc-u15-girls): a division that is zero-weighted the day
# it gets its first game reproduces the hs-* bug this comment describes. UFA is
# included because its game updates feed the same player ratings and its K
# multiplier would otherwise be unidentifiable.
TIER_SHARE = {"club": 0.50, "college": 0.30, "hs": 0.15, "other": 0.05}
_TIERS = (
    ("club", ("club-men", "club-mixed", "club-women", "pul", "wul")),
    ("college", ("college", "college-d3", "college-women",
                 "college-women-d3", "college-mixed")),
    ("hs", ("hs-boys", "hs-girls", "hs-mixed", "ms-boys", "ms-girls",
            "ms-mixed", "ycc-u15-boys", "ycc-u15-girls", "ycc-u15-mixed",
            "ycc-u17-boys", "ycc-u17-girls", "ycc-u17-mixed",
            "ycc-u20-boys", "ycc-u20-girls", "ycc-u20-mixed")),
    ("other", ("masters-men", "masters-women", "masters-mixed",
               "grandmasters-men", "grandmasters-women", "grandmasters-mixed",
               "greatgrandmasters-men", "greatgrandmasters-women",
               "beach-men", "beach-women", "beach-mixed",
               "beach-masters-men", "beach-masters-women",
               "beach-masters-mixed",
               "beach-grandmasters-men", "beach-grandmasters-women",
               "beach-grandmasters-mixed",
               "beach-greatgrandmasters-men", "beach-greatgrandmasters-women",
               "beach-greatgrandmasters-mixed", "beach-legends-mixed",
               "league-men", "league-mixed", "ufa")),
)
DIVISION_TIER = {d: t for t, ds in _TIERS for d in ds}
DIVISIONS = tuple(DIVISION_TIER)
assert set(TIER_SHARE) == {t for t, _ds in _TIERS}, "tier tables disagree"
# Great grand masters mixed is absent because it has never been played.
#
# Widening the SCORED set is not the same as widening the AXES. The thin
# divisions still get no scale or base axis of their own: a division the
# descent barely touches is unidentifiable and the search reports a confident,
# wrong answer (the college-women lesson). They vote on the global knobs,
# weighted, and inherit their by-analogy priors from rankings.PUBLISHED.
PRUNE_EPS = 0.0003

# One K multiplier per competition family. Individual beach, masters, and
# youth brackets are too thin to identify independently; grouping lets their
# games answer one shared question without pretending that 10-40 VAL games can
# fit a stable constant. Club remains the 1.0 gauge. Every grid includes values
# above 1.0 so the user's lower-weight hypothesis can lose on evidence.
K_SCALE_GROUPS = {
    "college": tuple(d for d in DIVISIONS if d.startswith("college")),
    "masters": (
        "masters-men", "masters-women", "masters-mixed",
        "grandmasters-men", "grandmasters-women", "grandmasters-mixed",
        "greatgrandmasters-men", "greatgrandmasters-women",
    ),
    "school": tuple(d for d in DIVISIONS if d.startswith(("hs-", "ms-"))),
    "ycc": tuple(d for d in DIVISIONS if d.startswith("ycc-")),
    "beach": tuple(d for d in DIVISIONS if d.startswith("beach-")),
    "ufa": ("ufa",),
}
K_SCALE_AXES = [
    ("k_scale_group.college", [0.5, 0.75, 1.0, 1.25, 1.5]),
    ("k_scale_group.masters", [0.25, 0.5, 0.75, 1.0, 1.25, 1.5]),
    ("k_scale_group.school",  [0.25, 0.5, 0.75, 1.0, 1.25, 1.5]),
    ("k_scale_group.ycc",     [0.25, 0.5, 0.75, 1.0, 1.25, 1.5]),
    ("k_scale_group.beach",   [0.1, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5]),
    ("k_scale_group.ufa",     [0.1, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5]),
]

# Grids. Each axis is (name, values); dict-valued knobs address one key.
AXES = [
    ("roster_shrink",          [0.0, 0.003, 0.006, 0.01, 0.015, 0.025]),
    ("k",                      [32.0, 40.0, 44.0, 48.0, 56.0, 64.0]),
    *K_SCALE_AXES,
    ("tau",                    [400.0, 500.0, 600.0, 700.0, 900.0, math.inf]),
    ("provisional_multiplier", [1.0, 2.0, 3.0, 4.0, 6.0, 8.0]),
    ("provisional_games",      [4, 6, 10, 14, 20, 30]),
    ("provisional_shape",      ["cliff", "linear", "exponential", "hyperbolic"]),
    ("offseason_regression",   [0.0, 0.03, 0.07, 0.12, 0.20]),
    ("mov_norm",               [5.0, 6.0, 7.0, 8.0, 10.0]),
    ("use_mov",                [True, False]),
    ("stat_transfer_beta",     [0.0, 2.0, 3.0, 5.0, 8.0, 12.0]),
    # UFA-only feature weights. Y is represented by its TY and RY components
    # rather than counted again as TY + RY + total Y. Completion percentage is
    # already normalized within the team-season before these weights apply.
    ("ufa_completion_usage_weight", [0.0, 0.25, 0.5, 1.0, 2.0]),
    ("ufa_completion_pct_weight",   [0.0, 0.5, 1.0, 2.0, 4.0]),
    ("ufa_throwing_yards_weight",   [0.0, 0.25, 0.5, 1.0, 2.0]),
    ("ufa_receiving_yards_weight",  [0.0, 0.25, 0.5, 1.0, 2.0]),
    ("ufa_hockey_assists_weight",   [0.0, 0.5, 1.0, 2.0, 4.0]),
    ("stat_transfer_clamp",    [40.0, 60.0, 90.0]),
    ("involvement_credit",     [False, True]),
    ("involvement_shrink",     [1.0, 4.0]),
    ("division_scale.club-men",   [220.0, 240.0, 260.0, 290.0, 320.0]),
    ("division_scale.club-mixed", [220.0, 240.0, 260.0, 290.0, 320.0]),
    ("division_scale.club-women", [160.0, 180.0, 200.0, 220.0, 260.0]),
    ("division_scale.college",    [220.0, 240.0, 260.0, 290.0, 320.0]),
    ("division_scale.college-d3", [220.0, 260.0, 300.0, 340.0]),
    ("division_bases.college",    [1250.0, 1300.0, 1350.0, 1400.0, 1500.0]),
    ("division_bases.college-d3", [1100.0, 1250.0, 1350.0, 1450.0]),
    ("division_bases.club-mixed", [1400.0, 1450.0, 1500.0, 1550.0, 1600.0]),
    ("division_bases.club-women", [1450.0, 1500.0, 1550.0, 1600.0, 1700.0]),
    # Untuned in PUBLISHED, set by analogy with the men's college offset and
    # the club women's scale. First sweep that runs should settle them.
    ("division_scale.college-women",    [160.0, 180.0, 200.0, 220.0, 260.0]),
    ("division_scale.college-women-d3", [180.0, 220.0, 260.0, 300.0]),
    ("division_bases.college-women",    [1250.0, 1350.0, 1450.0, 1550.0]),
    ("division_bases.college-women-d3", [1200.0, 1350.0, 1500.0]),
    # Masters, and only the four brackets that can carry a grid search. A
    # scale or base here is scored on its OWN division's VAL games
    # (axis_divisions below), so the question is how many that division has:
    #
    #   masters-mixed          292 VAL     grandmasters-women    78 VAL
    #   masters-men            235         grandmasters-mixed    24  (0 FIT)
    #   grandmasters-men       214         greatgrandmasters-w   17  (0 FIT)
    #   greatgrandmasters-men  169         masters-women        111
    #
    # The right-hand four are left on their by-analogy priors deliberately.
    # Two of them have NO fit games at all — they were first contested in
    # 2022 — and selecting a scale off 17 or 24 games is fitting noise, which
    # PRUNE_EPS cannot catch either: at that n the sampling error dwarfs
    # 0.0003, so a spurious move looks like a real one. A wrong prior costs
    # that division alone; a wrong "tuned" value costs it AND claims to be
    # measured.
    #
    # Grids are tight and centred on the analogy prior for the same reason:
    # even 200-300 games is thin, so the sweep is allowed to correct the
    # analogy, not to search freely.
    ("division_scale.masters-men",           [220.0, 240.0, 260.0, 290.0]),
    ("division_scale.masters-mixed",         [180.0, 200.0, 220.0, 260.0]),
    ("division_scale.grandmasters-men",      [220.0, 240.0, 260.0, 290.0]),
    ("division_scale.greatgrandmasters-men", [220.0, 260.0, 300.0]),
    ("division_bases.masters-men",           [1400.0, 1500.0, 1600.0]),
    ("division_bases.masters-mixed",         [1400.0, 1500.0, 1600.0]),
    ("division_bases.grandmasters-men",      [1400.0, 1500.0, 1600.0]),
    ("division_bases.greatgrandmasters-men", [1350.0, 1450.0, 1550.0]),
]
UFA_STAT_AXES = [item for item in AXES if item[0].startswith("ufa_")]
# division_bases["club-men"] is the gauge: every other base is a rating
# DIFFERENCE against it, and moving both is the same model twice.
DIAGNOSTIC_AXES = [("home_advantage", [0.0, 15.0, 25.0, 35.0])]

# Which games score which axis. A base or scale is judged on its own division;
# everything else on the whole corpus.
def axis_divisions(axis):
    for prefix in ("division_scale.", "division_bases."):
        if axis.startswith(prefix):
            return (axis.split(".", 1)[1],)
    return DIVISIONS


_G = {}


def _init():
    """Load the corpus once per worker process."""
    import sqlite3
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    games = load_games(con)
    rosters, clubs = load_maps(con)
    womens_pro = load_womens_pro_inputs(con)
    ufa_games, ufa_rosters, ufa_clubs = load_ufa_games(con)
    _G["games"] = sorted(
        games + womens_pro.games + ufa_games, key=lambda g: g["sort"]
    )
    _G["rosters"] = {**rosters, **womens_pro.rosters, **ufa_rosters}
    _G["clubs"] = {**clubs, **womens_pro.clubs, **ufa_clubs}
    _G["stat_events"] = load_stat_events(con)
    _G["ufa_data"] = load_ufa_stat_data(con)
    _G["stats"] = sorted(
        _G["stat_events"] + load_ufa_stat_events(con, as_config({})),
        key=lambda e: e[0],
    )
    con.close()


def as_config(overrides: dict) -> EloConfig:
    cfg = dict(PUBLISHED)
    cfg["division_scale"] = dict(PUBLISHED["division_scale"])
    cfg["division_bases"] = dict(PUBLISHED["division_bases"])
    cfg["k_scale"] = dict(PUBLISHED.get("k_scale", {}))
    for key, value in overrides.items():
        if key.startswith("k_scale_group."):
            group = key.split(".", 1)[1]
            for division in K_SCALE_GROUPS[group]:
                cfg["k_scale"][division] = value
        elif "." in key:
            group, sub = key.split(".", 1)
            cfg[group][sub] = value
    return EloConfig(**cfg)


def _score(args):
    """(overrides, divisions) -> (val, test, per-division val, records)."""
    overrides, divs = args
    if not _G:
        _init()
    cfg = as_config(overrides)
    if "ufa_data" in _G:
        stats = sorted(
            _G["stat_events"] + build_ufa_stat_events(_G["ufa_data"], cfg),
            key=lambda e: e[0],
        )
    else:
        stats = _G["stats"]
    recs, _model = replay(
        "player", _G["games"], _G["rosters"], _G["clubs"], cfg, stats,
    )
    out = {}
    for seasons, tag in ((VAL, "val"), (TEST, "test")):
        per_div = {}
        for d in divs:
            m = metrics(recs, seasons=set(seasons), divisions={d})
            if m["n"]:
                out[f"{tag}.{d}"] = m["logloss"]
                per_div[d] = (m["n"], m["logloss"])
        # Each tier gets TIER_SHARE[tier] of the objective, split across that
        # tier's own games; shares renormalise over tiers actually present
        # (a tier with zero games here costs nothing, e.g. a single-division
        # axis call, or a window a tier hasn't reached yet).
        tier_n = {}
        for d, (n, _ll) in per_div.items():
            t = DIVISION_TIER[d]
            tier_n[t] = tier_n.get(t, 0) + n
        present_share = sum(TIER_SHARE[t] for t in tier_n)
        num = den = 0.0
        for d, (n, ll) in per_div.items():
            t = DIVISION_TIER[d]
            w = (TIER_SHARE[t] / present_share) * (n / tier_n[t])
            num += ll * w
            den += w
        out[tag] = num / den if den else float("inf")
    return out


def evaluate(pool, jobs):
    """jobs: [(label, overrides, divisions)] -> {label: scores}."""
    args = [(o, d) for _l, o, d in jobs]
    results = pool.map(_score, args) if pool else map(_score, args)
    return {label: r for (label, _o, _d), r in zip(jobs, results)}


def descend(pool, passes, axes):
    base = {}
    history = []
    cur = evaluate(pool, [("base", base, DIVISIONS)])["base"]
    print(f"start: VAL {cur['val']:.5f}  TEST {cur['test']:.5f}\n")
    for p in range(1, passes + 1):
        moved = False
        print(f"--- pass {p}")
        for axis, values in axes:
            divs = axis_divisions(axis)
            here = evaluate(pool, [("cur", dict(base), divs)])["cur"]
            jobs = []
            for v in values:
                trial = dict(base)
                trial[axis] = v
                jobs.append((repr(v), trial, divs))
            scored = evaluate(pool, jobs)
            best_v, best = min(scored.items(), key=lambda kv: kv[1]["val"])
            gain = here["val"] - best["val"]
            if gain > 1e-6:
                literal = next(v for v in values if repr(v) == best_v)
                base[axis] = literal
                history.append((axis, literal, gain))
                moved = True
                print(f"  {axis:<28} -> {best_v:<12} "
                      f"{'/'.join(d[:9] for d in divs) if len(divs) == 1 else 'all'}"
                      f" VAL {here['val']:.5f} -> {best['val']:.5f}  ({gain:+.5f})")
        if not moved:
            print("  no move improved VAL; converged")
            break
    return base, history


def prune(pool, base, history):
    """Drop marginal moves while the CUMULATIVE VAL cost stays within PRUNE_EPS.

    PRUNE_EPS was an absolute PER-MOVE threshold, which bounds nothing in
    aggregate. On the tier-weighted objective the 2026 corpus produced 18
    moves that each cost <= 0.0003 to revert and 0.00147 to revert together,
    so the prune handed back five times the tolerance it exists to protect
    and returned the published config unchanged. That 0.00147 is not noise:
    a paired bootstrap over the 28,570 VAL games puts it at 90% CI
    [-0.00149, -0.00025], excluding zero, and every tier moves significantly
    (club -0.00109, hs -0.00696, other -0.00538; college +0.00127 against).
    The search was right and the threshold was wrong, so the budget is now on
    the TOTAL given back. Cheapest moves are still considered first, so what
    survives is the smallest set carrying essentially all of the gain.

    A move whose reversion IMPROVES VAL (negative cost) is always dropped and
    consumes no budget.
    """
    kept = dict(base)
    full = evaluate(pool, [("full", kept, DIVISIONS)])["full"]["val"]
    spent = 0.0
    print(f"\n--- prune (drop while cumulative reverted VAL stays within "
          f"{PRUNE_EPS})")
    for axis, value, _g in sorted(history, key=lambda h: h[2]):
        if axis not in kept:
            continue
        trial = {k: v for k, v in kept.items() if k != axis}
        new_val = evaluate(pool, [("t", trial, DIVISIONS)])["t"]["val"]
        cost = new_val - full
        would_spend = spent + max(cost, 0.0)
        verdict = "DROP" if would_spend <= PRUNE_EPS else "keep"
        print(f"  {axis:<28} {str(value):<12} reverting costs {cost:+.5f}"
              f"  cumulative {would_spend:.5f}  {verdict}")
        if verdict == "DROP":
            kept = trial
            full = new_val
            spent = would_spend
    return kept


def paired_ci(a, b, n_boot=2000, seed=0):
    def ll(e, o):
        eps = 1e-12
        return -(o*math.log(max(e, eps)) + (1-o)*math.log(max(1-e, eps)))
    da, db = [ll(*x) for x in a], [ll(*x) for x in b]
    d = [y-x for x, y in zip(da, db)]
    random.seed(seed)
    n = len(d)
    bs = sorted(sum(random.choices(d, k=n))/n for _ in range(n_boot))
    return sum(d)/n, bs[int(.05*n_boot)], bs[int(.95*n_boot)]


def main(
    passes=3, jobs=8, quick=False, division_k_only=False, stats_only=False,
):
    axes = (
        UFA_STAT_AXES if stats_only
        else K_SCALE_AXES if division_k_only
        else AXES[:6] if quick else AXES
    )
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init) as pool:
        base, history = descend(pool, passes, axes)
        kept = prune(pool, base, history)
        final = evaluate(pool, [("f", kept, DIVISIONS)])["f"]
        pub = evaluate(pool, [("p", {}, DIVISIONS)])["p"]
        print("\n--- diagnostic only, not adopted")
        for axis, values in DIAGNOSTIC_AXES:
            for v in values:
                t = dict(kept); t[axis] = v
                s = evaluate(pool, [("d", t, DIVISIONS)])["d"]
                print(f"  {axis}={v:<6} VAL {s['val']:.5f}  TEST {s['test']:.5f}")
    print(f"\n=== selected ({time.time()-t0:.0f}s)")
    for k, v in sorted(kept.items()):
        print(f"  {k} = {v!r}")
    w = max(len(d) for d in DIVISIONS) + 2
    print(f"\n{'division':<{w}}{'published VAL':>14}{'new VAL':>10}"
          f"{'published TEST':>15}{'new TEST':>10}")
    for d in DIVISIONS:
        pv, fv = pub.get(f"val.{d}"), final.get(f"val.{d}")
        pt, ft = pub.get(f"test.{d}"), final.get(f"test.{d}")
        vs = f"{pv:>14.5f}{fv:>10.5f}" if pv is not None else f"{'-':>14}{'-':>10}"
        ts = f"{pt:>15.5f}{ft:>10.5f}" if pt is not None else f"{'-':>15}{'-':>10}"
        print(f"{d:<{w}}{vs}{ts}")
    print(f"{'weighted':<14}{pub['val']:>14.5f}{final['val']:>10.5f}"
          f"{pub['test']:>15.5f}{final['test']:>10.5f}")
    return kept


if __name__ == "__main__":
    argv = sys.argv[1:]
    def opt(name, default):
        return int(argv[argv.index(name) + 1]) if name in argv else default
    main(passes=opt("--passes", 3), jobs=opt("--jobs", 8),
         quick="--quick" in argv, division_k_only="--division-k-only" in argv,
         stats_only="--stats-only" in argv)
