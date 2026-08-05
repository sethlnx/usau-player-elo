"""Coordinate descent over the engine's knobs, on the three-way split.

    FIT   2017-2021   ratings accumulate here
    VAL   2022-2023   every parameter is CHOSEN here
    TEST  2024-2025   reported once, never selected on

Selection scores the n-weighted mean logloss across ALL FIVE divisions, not
club men's alone. That is the change from the previous descent: club men's is
now 19,682 of 90,284 games, so tuning global knobs on it alone would let 78%
of the corpus be collateral. Per-division bases and scales are still scored on
their own division's games — the D-III lesson in analysis/rankings.py — since
a base only governs the pool it initialises.

Two knobs are deliberately NOT axes:
  low_info_anchor  It costs logloss on purpose (see EloConfig); a logloss
                   descent would drive it to zero and re-strand the ~340
                   players it exists to rescue. Pinned, and the pathology
                   counts are re-checked against the selected config.
  home_advantage   Swept as a diagnostic and reported, never auto-adopted: it
                   imports USAU's seeding into a results-only model. See the
                   note in analysis/rankings.py.

Every surviving move is then dropped one at a time and kept only if reverting
it costs more than PRUNE_EPS VAL logloss, which is what stops the descent
publishing a tail of moves that are really fitting VAL.

Usage: python -m analysis.descent [--passes N] [--jobs N] [--quick]
"""

import itertools
import math
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH, load_games, load_maps, load_stat_events, \
    load_ufa_stat_events, metrics, replay
from analysis.rankings import PUBLISHED
from elo.engine import EloConfig

# FIT reaches back to 2014 with the corpus. It used to start at 2017 because
# that was where the corpus started; leaving it there would accumulate ratings
# from a cold start three seasons later than the data allows and hand VAL a
# worse model than the engine can actually build.
FIT = (2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021)
VAL = (2022, 2023)
TEST = (2024, 2025)
# Every CONTESTED division, n-weighted. This is what scores the global axes,
# and it was five when there were five; leaving it there would tune k, tau and
# the provisional window against a third of the corpus and call it global.
# Great grand masters mixed is absent because it has never been played.
DIVISIONS = ("club-men", "club-mixed", "club-women", "college", "college-d3",
             "college-women", "college-women-d3",
             "masters-men", "masters-women", "masters-mixed",
             "grandmasters-men", "grandmasters-women", "grandmasters-mixed",
             "greatgrandmasters-men", "greatgrandmasters-women")
PRUNE_EPS = 0.0003

# Grids. Each axis is (name, values); dict-valued knobs address one key.
AXES = [
    ("roster_shrink",          [0.0, 0.003, 0.006, 0.01, 0.015, 0.025]),
    ("k",                      [32.0, 40.0, 44.0, 48.0, 56.0, 64.0]),
    ("tau",                    [400.0, 500.0, 600.0, 700.0, 900.0, math.inf]),
    ("provisional_multiplier", [1.0, 2.0, 3.0, 4.0, 6.0, 8.0]),
    ("provisional_games",      [4, 6, 10, 14, 20, 30]),
    ("provisional_shape",      ["cliff", "linear", "exponential", "hyperbolic"]),
    ("offseason_regression",   [0.0, 0.03, 0.07, 0.12, 0.20]),
    ("mov_norm",               [5.0, 6.0, 7.0, 8.0, 10.0]),
    ("use_mov",                [True, False]),
    ("stat_transfer_beta",     [0.0, 2.0, 3.0, 5.0, 8.0, 12.0]),
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
    _G["games"] = load_games(con)
    _G["rosters"], _G["clubs"] = load_maps(con)
    _G["stats"] = sorted(load_stat_events(con) + load_ufa_stat_events(con),
                         key=lambda e: e[0])
    con.close()


def as_config(overrides: dict) -> EloConfig:
    cfg = dict(PUBLISHED)
    cfg["division_scale"] = dict(PUBLISHED["division_scale"])
    cfg["division_bases"] = dict(PUBLISHED["division_bases"])
    for key, value in overrides.items():
        if "." in key:
            group, sub = key.split(".", 1)
            cfg[group][sub] = value
        else:
            cfg[key] = value
    return EloConfig(**cfg)


def _score(args):
    """(overrides, divisions) -> (val, test, per-division val, records)."""
    overrides, divs = args
    if not _G:
        _init()
    recs, _model = replay("player", _G["games"], _G["rosters"], _G["clubs"],
                          as_config(overrides), _G["stats"])
    out = {}
    for seasons, tag in ((VAL, "val"), (TEST, "test")):
        num = den = 0.0
        for d in divs:
            m = metrics(recs, seasons=set(seasons), divisions={d})
            if m["n"]:
                out[f"{tag}.{d}"] = m["logloss"]
                num += m["logloss"] * m["n"]
                den += m["n"]
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
    """Drop each move whose reversion costs <= PRUNE_EPS on VAL."""
    kept = dict(base)
    full = evaluate(pool, [("full", kept, DIVISIONS)])["full"]["val"]
    print(f"\n--- prune (keep a move only if reverting costs > {PRUNE_EPS})")
    for axis, value, _g in sorted(history, key=lambda h: h[2]):
        if axis not in kept:
            continue
        trial = {k: v for k, v in kept.items() if k != axis}
        cost = evaluate(pool, [("t", trial, DIVISIONS)])["t"]["val"] - full
        verdict = "keep" if cost > PRUNE_EPS else "DROP"
        print(f"  {axis:<28} {str(value):<12} reverting costs {cost:+.5f}  {verdict}")
        if verdict == "DROP":
            kept = trial
            full = evaluate(pool, [("f", kept, DIVISIONS)])["f"]["val"]
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


def main(passes=3, jobs=8, quick=False):
    axes = AXES[:6] if quick else AXES
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
    print(f"\n{'division':<14}{'published VAL':>14}{'new VAL':>10}"
          f"{'published TEST':>15}{'new TEST':>10}")
    for d in DIVISIONS:
        print(f"{d:<14}{pub['val.'+d]:>14.5f}{final['val.'+d]:>10.5f}"
              f"{pub['test.'+d]:>15.5f}{final['test.'+d]:>10.5f}")
    print(f"{'weighted':<14}{pub['val']:>14.5f}{final['val']:>10.5f}"
          f"{pub['test']:>15.5f}{final['test']:>10.5f}")
    return kept


if __name__ == "__main__":
    argv = sys.argv[1:]
    def opt(name, default):
        return int(argv[argv.index(name) + 1]) if name in argv else default
    main(passes=opt("--passes", 3), jobs=opt("--jobs", 8),
         quick="--quick" in argv)
