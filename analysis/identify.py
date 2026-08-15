"""How load-bearing is each player's rating? Leave-one-out, measured.

A player's Elo is only meaningful if the results are better explained WITH it
than without it. That is not guaranteed here: the game delta is applied to
every rostered player equally, so the only channels separating teammates are
the provisional window and stat transfers, and a rating can drift far from
anything the games pin down. Five of the top twenty do it — removing them
IMPROVES the prediction of their own teams' games.

The measurement is a true leave-one-out: drop the player from every roster,
replay the whole corpus, and compare logloss on the games their teams played.

    loo > 0   the rating carries information the rest of the roster does not
    loo <= 0  the results are better explained without it

Two cheaper proxies were tried and BOTH FAIL, which is why this is expensive:

  split-half   Replay alternating games twice and compare the two estimates.
               Flat at 101-110 across softmax share, gap over roster median
               and teammate count — it measures REPRODUCIBILITY, and both
               halves are reproducibly biased in the same direction. It is
               what rating_sigma is built on, and it cannot see this at all.
  static LOO   Recompute each game's prediction with and without the player,
               using final ratings instead of replaying. Correlates +0.237
               with the real thing and gets the SIGN wrong on the two worst
               cases: a snapshot cannot see a runaway that the replay built.

Cost is one full replay per player, so this covers the top TOP_N by rating
rather than all 39,000 — that is every row a reader is realistically looking
at, and the coverage is stated rather than papered over.

Usage: python -m analysis.identify [--top N] [--jobs N]
Writes data/player_loo.csv (player_id, loo, games_scored).
"""

import csv
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH, load_games, load_maps, load_stat_events, \
    load_ufa_stat_events, replay
from analysis.rankings import PUBLISHED
from elo.engine import EloConfig

TOP_N = 1000
OUT = DB_PATH.parent / "player_loo.csv"

_G = {}


def _init():
    import sqlite3
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    _G["games"] = load_games(con)
    _G["rosters"], _G["clubs"] = load_maps(con)
    _G["cfg"] = EloConfig(**PUBLISHED)
    _G["stats"] = sorted(
        load_stat_events(con) + load_ufa_stat_events(con, _G["cfg"]),
        key=lambda e: e[0],
    )
    con.close()


def _nll(expected, outcome):
    eps = 1e-12
    return -(outcome * math.log(max(expected, eps))
             + (1 - outcome) * math.log(max(1 - expected, eps)))


def _baseline():
    recs, _m = replay("player", _G["games"], _G["rosters"], _G["clubs"],
                      _G["cfg"], _G["stats"])
    return recs


def _loo(pid):
    if not _G:
        _init()
    if "base" not in _G:
        _G["base"] = _baseline()
    mine = {etid for etid, pl in _G["rosters"].items() if pid in pl}
    # An empty roster would fall back to the ghost path and change the game's
    # meaning entirely, so a one-player roster keeps its player.
    without = {etid: ([p for p in pl if p != pid] or pl)
               for etid, pl in _G["rosters"].items()}
    recs, _m = replay("player", _G["games"], without, _G["clubs"],
                      _G["cfg"], _G["stats"])
    with_ll = without_ll = 0.0
    n = 0
    for game, a, b in zip(_G["games"], _G["base"], recs):
        if game["home_id"] in mine or game["away_id"] in mine:
            with_ll += _nll(a[3], a[4])
            without_ll += _nll(b[3], b[4])
            n += 1
    return pid, ((without_ll - with_ll) / n if n else 0.0), n


def main(top_n=TOP_N, jobs=8):
    players = [r for r in csv.DictReader(open(DB_PATH.parent / "player_elo.csv"))]
    players.sort(key=lambda r: -float(r["elo"]))
    targets = [int(r["player_id"]) for r in players[:top_n]]
    name_of = {int(r["player_id"]): r["player"] for r in players}
    t0 = time.time()
    out = []
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init) as pool:
        for i, (pid, loo, n) in enumerate(pool.map(_loo, targets, chunksize=4), 1):
            out.append((pid, round(loo, 6), n))
            if i % 100 == 0:
                print(f"  {i}/{len(targets)}  {time.time()-t0:.0f}s", flush=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["player_id", "loo", "games_scored"])
        w.writerows(out)
    hurt = [o for o in out if o[1] <= 0]
    print(f"wrote {OUT} ({len(out)} players, {time.time()-t0:.0f}s)")
    print(f"{len(hurt)} of {len(out)} ratings are NOT load-bearing "
          f"(removing them does not hurt, or helps)")
    worst = sorted(out, key=lambda o: o[1])[:8]
    for pid, loo, n in worst:
        print(f"   {name_of.get(pid, pid):<26} loo {loo:+.4f} over {n} games")


if __name__ == "__main__":
    argv = sys.argv[1:]
    def opt(name, default):
        return int(argv[argv.index(name) + 1]) if name in argv else default
    main(top_n=opt("--top", TOP_N), jobs=opt("--jobs", 8))
