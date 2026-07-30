"""Per-season team-rating trajectories for the top-N teams, median-normalized.

Replays player Elo season by season, snapshots each canonical club's rating
from its LAST event roster that season, then expresses it as points above the
season's median club (which strips the model's year-over-year scale drift).

Canonical club names merge cross-season aliases (see identity.CLUB_ALIASES),
so e.g. "Rhino" (2025) and "Rhino Slam!" (other years) form one continuous line.

Usage: python -m analysis.team_timeseries  ->  data/top10_timeseries.json
"""

import json
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import load_games, load_maps
from elo.engine import EloConfig, PlayerElo
from identity.resolve import canonical_club

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "usau_final.db"
OUT = DB_PATH.parent / "top10_timeseries.json"
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]
CFG = EloConfig(k=60, tau=150, offseason_regression=0.0)


def club_last_roster(con, season):
    """canonical club -> event_team_id of its latest event that season."""
    rows = con.execute("""
        SELECT et.full_name, et.display_name, ev.start_date, et.event_team_id
        FROM event_teams et JOIN events ev ON ev.event_id = et.event_id
        WHERE ev.season = ? ORDER BY ev.start_date
    """, (season,)).fetchall()
    last = {}
    for full, disp, _, etid in rows:
        last[canonical_club(full, disp)] = etid
    return last


def main():
    con = sqlite3.connect(DB_PATH)
    games = load_games(con)
    rosters, clubs = load_maps(con)

    by_season = {s: [] for s in SEASONS}
    for g in games:
        by_season[g["season"]].append(g)

    model = PlayerElo(CFG)
    snapshots = {}
    for i, s in enumerate(SEASONS):
        if i > 0:
            model.new_season()
        for g in by_season[s]:
            home = rosters.get(g["home_id"]) or [f"ghost:{clubs.get(g['home_id'])}:{s}"]
            away = rosters.get(g["away_id"]) or [f"ghost:{clubs.get(g['away_id'])}:{s}"]
            model.play_game(home, away, g["home_score"], g["away_score"])
        snap = {}
        for club, etid in club_last_roster(con, s).items():
            pids = rosters.get(etid)
            if pids:
                snap[club] = model.team_rating(pids)
        snapshots[s] = snap

    final = sorted(snapshots[SEASONS[-1]].items(), key=lambda x: -x[1])
    top10 = [c for c, _ in final[:10]]

    series = {}
    for c in top10:
        pts = []
        for s in SEASONS:
            snap = snapshots[s]
            med = statistics.median(snap.values()) if snap else 0
            pts.append(round(snap[c] - med, 1) if c in snap else None)
        series[c] = pts

    OUT.write_text(json.dumps({
        "seasons": SEASONS, "top10": top10, "series": series,
        "final": [[c, round(r, 1)] for c, r in final[:20]],
    }, indent=1))

    print(f"wrote {OUT}")
    for c in top10:
        print(f"  {c:<22} {series[c]}")
    con.close()


if __name__ == "__main__":
    main()
