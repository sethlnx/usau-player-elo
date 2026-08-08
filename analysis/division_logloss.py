"""Walk-forward log-loss, per division, over the full corpus.

Quarantine, in this evaluation, means what analysis.backtest.metrics already
enforces: `division` scopes every reported statistic to games from that one
division's own prediction stream. Nothing here lets one division's games
score another's numbers — ratings still transfer PLAYERS across divisions
(that's the model's whole premise: a college player carries an identity into
club), but the win-probability curve, its scale, and its debut base are read
per-game off `g["division"]`, and the metric below is filtered the same way.

Reports, for every division with games in the corpus:
  n            games scored
  logloss      the model's out-of-sample log-loss
  vs coin      logloss at the constant p=0.5 (ln 2 = 0.69315) — beating this
               is the bar for "the model knows anything"
  vs base rate the corpus's own home-win rate as a constant prediction —
               logloss cannot be beaten by an unconditional constant unless
               the model uses information beyond the marginal outcome rate
  brier, accuracy
  stat evts    count of that division's events carrying a real box score
               (Goals/Assists/Turns/Blocks) — the subset analysis.backtest's
               involvement_credit / stat_transfer_beta mechanisms can see

Usage: python -m analysis.division_logloss [--all-seasons]
"""

import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH, load_games, load_maps, load_stat_events, metrics, replay
from analysis.rankings import PUBLISHED
from elo.engine import EloConfig


def stat_event_counts(con) -> dict[str, int]:
    rows = con.execute("""
        SELECT ev.division, count(DISTINCT ev.event_id)
        FROM events ev JOIN event_teams et USING(event_id)
                       JOIN roster_entries re USING(event_team_id)
        WHERE re.points<>'' OR re.assists<>'' OR re.ds<>'' OR re.turns<>''
        GROUP BY 1
    """).fetchall()
    return dict(rows)


def const_logloss(p: float) -> float:
    """Log-loss of always predicting the constant p (for outcome in {0,0.5,1})."""
    eps = 1e-12
    p = min(max(p, eps), 1 - eps)
    # Symmetric under 0.5 outcomes: -[o ln p + (1-o) ln(1-p)] at o=0.5 is the
    # same expression as at o=0/1 once averaged, so this closed form is exact
    # only for a 0/1-only corpus; ties are rare and handled by the caller
    # falling back to the full sum when any exist.
    return -(p * math.log(p) + (1 - p) * math.log(1 - p))


def baseline_metrics(records, division: str) -> dict:
    """Constant-p log-loss baselines scored on the SAME rows metrics() sees."""
    rs = [o for s, d, date, e, o in records if d == division]
    n = len(rs)
    if n == 0:
        return {}
    eps = 1e-12
    home_rate = sum(rs) / n
    coin = -sum(o * math.log(0.5) + (1 - o) * math.log(0.5) for o in rs) / n
    base = -sum(o * math.log(max(home_rate, eps)) +
               (1 - o) * math.log(max(1 - home_rate, eps)) for o in rs) / n
    return {"coin": coin, "base_rate": home_rate, "base_logloss": base}


def main(all_seasons: bool = False):
    con = sqlite3.connect(DB_PATH)
    games = load_games(con)
    rosters, clubs = load_maps(con)
    stat_events = load_stat_events(con)
    stat_counts = stat_event_counts(con)

    cfg = EloConfig(**PUBLISHED)
    records, _ = replay("player", games, rosters, clubs, cfg, stat_events)

    divisions = sorted({d for _, d, _, _, _ in records})
    eval_seasons = None if all_seasons else (2024, 2025)

    print(f"{'division':<28}{'n':>7}{'logloss':>9}{'vs coin':>9}{'vs base':>9}"
          f"{'brier':>8}{'acc':>7}{'stat evts':>10}")
    rows = []
    for d in divisions:
        m = metrics(records, seasons=eval_seasons, divisions=(d,))
        if m["n"] == 0:
            continue
        b = baseline_metrics(
            [(s, dd, dt, e, o) for s, dd, dt, e, o in records
             if dd == d and (eval_seasons is None or s in eval_seasons)], d)
        rows.append((m["n"], d, m, b))
    rows.sort(reverse=True)
    for n, d, m, b in rows:
        d_coin = m["logloss"] - b["coin"]
        d_base = m["logloss"] - b["base_logloss"]
        print(f"{d:<28}{n:>7}{m['logloss']:>9.4f}{d_coin:>+9.4f}{d_base:>+9.4f}"
              f"{m['brier']:>8.4f}{m['accuracy']:>7.3f}{stat_counts.get(d, 0):>10}")
    con.close()


if __name__ == "__main__":
    main("--all-seasons" in sys.argv)
