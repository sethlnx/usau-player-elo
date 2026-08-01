"""Divisional rankings: Men's Club, College, UFA — plus overall top players.

Replays the published model once (same config as analysis.rankings), then
rates each division's teams from their latest rosters:
  club    — latest event roster per club in the latest club season (USAU)
  college — latest event roster per school in the latest college season
  ufa     — latest UFA season rosters, mapped to USAU player identities via
            data/ufa_links.csv (unlinked UFA players are unrated; the rating
            is over the linked subset and coverage is shown as n/total)

Print-only: writes no files.
Usage: python -m analysis.division_rankings [teams_per_division] [top_players]
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH, load_games, load_maps, load_stat_events, replay
from analysis.rankings import PUBLISHED, last_appearance
from elo.engine import EloConfig
from ufa.link import resolve_links


def latest_event_rosters(con, season: int, division: str):
    """club -> player_ids from that club's most recent event roster."""
    rows = con.execute("""
        SELECT COALESCE(et.full_name, et.display_name) AS club,
               ev.start_date, et.event_team_id
        FROM event_teams et
        JOIN events ev ON ev.event_id = et.event_id
        WHERE ev.season = ? AND ev.division = ?
        ORDER BY ev.start_date
    """, (season, division)).fetchall()
    latest_etid = {}
    for club, _, etid in rows:
        latest_etid[club] = etid
    rosters, _ = load_maps(con)
    return {club: rosters.get(etid, []) for club, etid in latest_etid.items()}


def main(n_teams: int = 15, n_players: int = 25):
    con = sqlite3.connect(DB_PATH)
    games = load_games(con)
    rosters, clubs = load_maps(con)
    stat_events = load_stat_events(con)
    _, model = replay("player", games, rosters, clubs,
                      EloConfig(**PUBLISHED), stat_events)

    def usau_table(division: str, label: str):
        season = con.execute(
            "SELECT max(season) FROM events "
            "WHERE has_schedule=1 AND division=?", (division,)).fetchone()[0]
        rated = [(model.team_rating(pids), club, len(pids))
                 for club, pids in latest_event_rosters(con, season, division).items()
                 if pids]
        rated.sort(reverse=True)
        print(f"\n== {label} {season} — {len(rated)} teams ==")
        for i, (r, club, n) in enumerate(rated[:n_teams], 1):
            print(f"{i:>3}. {club:<34}{r:7.1f}  ({n} rostered)")

    usau_table("club-men", "MEN'S CLUB")
    usau_table("college", "COLLEGE")

    ufa_year = con.execute("SELECT max(year) FROM ufa_teams").fetchone()[0]
    links = resolve_links(con)
    rated = []
    for team_id, full_name, w, l in con.execute(
            "SELECT team_id, full_name, wins, losses FROM ufa_teams "
            "WHERE year=? AND (wins + losses) > 0", (ufa_year,)):
        roster = [pid for (pid,) in con.execute(
            "SELECT player_id FROM ufa_players WHERE team_id=? AND year=?",
            (team_id, ufa_year))]
        pids = [links[p] for p in roster if p in links]
        if pids:
            rated.append((model.team_rating(pids), full_name, len(pids),
                          len(roster), w, l))
    rated.sort(reverse=True)
    print(f"\n== UFA {ufa_year} — {len(rated)} teams "
          f"(rating over linked players only) ==")
    for i, (r, name, n, tot, w, l) in enumerate(rated[:n_teams], 1):
        print(f"{i:>3}. {name:<34}{r:7.1f}  ({n}/{tot} linked, {w}-{l})")

    latest = last_appearance(con)
    ranked = sorted(
        ((st.rating, pid, st.games) for pid, st in model.players.items()
         if not str(pid).startswith("ghost:") and st.games >= 5),
        reverse=True)
    print(f"\n== TOP PLAYERS OVERALL — {len(ranked)} rated with 5+ games ==")
    for i, (r, pid, ngames) in enumerate(ranked[:n_players], 1):
        name, club, season = latest.get(pid, ("?", "?", "?"))
        print(f"{i:>3}. {name:<28}{r:7.1f}  {ngames:>4}g  {club} ({season})")
    con.close()


if __name__ == "__main__":
    main(*[int(a) for a in sys.argv[1:]])
