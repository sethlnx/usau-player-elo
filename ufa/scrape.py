"""Pull UFA teams, players, season stats, and games into data/usau.db.

Usage: python -m ufa.scrape [season ...]     (default 2021-current year)
Idempotent: rows are UPSERTed; the current season bypasses the JSON cache.
"""

from datetime import date
import sys

import requests

from scraper.build_db import DB_PATH, connect

from . import api

STAT_COLUMNS = [
    "assists", "goals", "hockeyAssists", "completions", "throwAttempts",
    "throwaways", "stalls", "drops", "blocks", "catches", "callahans",
    "oPointsPlayed", "oPointsScored", "dPointsPlayed", "dPointsScored",
    "secondsPlayed", "oOpportunities", "oOpportunityScores",
    "dOpportunities", "dOpportunityStops", "yardsThrown", "yardsReceived",
    "hucksAttempted", "hucksCompleted",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ufa_teams (
    team_id TEXT NOT NULL, year INTEGER NOT NULL,
    division TEXT, city TEXT, name TEXT, full_name TEXT, abbrev TEXT,
    wins INTEGER, losses INTEGER,
    PRIMARY KEY (team_id, year)
);
CREATE TABLE IF NOT EXISTS ufa_players (
    player_id TEXT NOT NULL, year INTEGER NOT NULL, team_id TEXT NOT NULL,
    first_name TEXT, last_name TEXT, jersey TEXT, active INTEGER,
    PRIMARY KEY (player_id, year, team_id)
);
CREATE TABLE IF NOT EXISTS ufa_player_stats (
    player_id TEXT NOT NULL, year INTEGER NOT NULL,
    {", ".join(c.lower() + " INTEGER" for c in STAT_COLUMNS)},
    PRIMARY KEY (player_id, year)
);
CREATE TABLE IF NOT EXISTS ufa_games (
    game_id TEXT PRIMARY KEY, year INTEGER NOT NULL, date TEXT,
    home_team_id TEXT, away_team_id TEXT,
    home_score INTEGER, away_score INTEGER, status TEXT, week TEXT
);
CREATE TABLE IF NOT EXISTS ufa_game_stats (
    game_id TEXT NOT NULL, player_id TEXT NOT NULL, team_id TEXT,
    o_points_played INTEGER, d_points_played INTEGER, seconds_played INTEGER,
    goals INTEGER, assists INTEGER, blocks INTEGER,
    throwaways INTEGER, drops INTEGER,
    PRIMARY KEY (game_id, player_id)
);
"""


def scrape_game_stats(con, session, refresh_years=()) -> int:
    """Per-game player lines (real game rosters) for every final game."""
    refresh_years = set(refresh_years)
    done = {r[0] for r in con.execute(
        "SELECT DISTINCT game_id FROM ufa_game_stats")}
    games = con.execute(
        "SELECT game_id, year FROM ufa_games "
        "WHERE status='Final' ORDER BY year, game_id"
    ).fetchall()
    n = 0
    for gid, year in games:
        refresh = year in refresh_years
        if gid in done and not refresh:
            continue
        rows = api.get(
            "playerGameStats", {"gameID": gid}, session, refresh=refresh
        )
        if not rows:
            continue
        if refresh:
            con.execute("DELETE FROM ufa_game_stats WHERE game_id=?", (gid,))
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO ufa_game_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (gid, r["player"]["playerID"], r.get("teamID"),
                 r.get("oPointsPlayed"), r.get("dPointsPlayed"),
                 r.get("secondsPlayed"), r.get("goals"), r.get("assists"),
                 r.get("blocks"), r.get("throwaways"), r.get("drops")))
            n += 1
        con.commit()
    return n


def scrape_season(con, session, year: int, refresh: bool = False) -> str:
    for t in api.get("teams", {"years": year}, session, refresh=refresh):
        con.execute(
            "INSERT OR REPLACE INTO ufa_teams VALUES (?,?,?,?,?,?,?,?,?)",
            (t["teamID"], year, (t.get("division") or {}).get("name"),
             t.get("city"), t.get("name"), t.get("fullName"), t.get("abbrev"),
             t.get("wins"), t.get("losses")))

    players = api.get("players", {"years": year}, session, refresh=refresh)
    pids = []
    for p in players:
        pids.append(p["playerID"])
        for tm in p.get("teams", []):
            if tm.get("year") != year:
                continue
            con.execute(
                "INSERT OR REPLACE INTO ufa_players VALUES (?,?,?,?,?,?,?)",
                (p["playerID"], year, tm.get("teamID"), p.get("firstName"),
                 p.get("lastName"), tm.get("jerseyNumber"),
                 int(bool(tm.get("active")))))

    n_stats = 0
    for i in range(0, len(pids), 50):
        batch = ",".join(pids[i:i + 50])
        for r in api.get(
            "playerStats", {"playerIDs": batch, "years": year}, session,
            refresh=refresh,
        ):
            con.execute(
                "INSERT OR REPLACE INTO ufa_player_stats VALUES "
                f"(?,?{',?' * len(STAT_COLUMNS)})",
                (r["player"]["playerID"], r.get("year", year),
                 *[r.get(c) for c in STAT_COLUMNS]))
            n_stats += 1

    games = api.get("games", {"date": str(year)}, session, refresh=refresh)
    for g in games:
        con.execute(
            "INSERT OR REPLACE INTO ufa_games VALUES (?,?,?,?,?,?,?,?,?)",
            (g["gameID"], year, (g.get("startTimestamp") or "")[:10],
             g.get("homeTeamID"), g.get("awayTeamID"),
             g.get("homeScore"), g.get("awayScore"),
             g.get("status"), g.get("week")))

    con.commit()
    return f"{len(pids)} players, {n_stats} stat lines, {len(games)} games"


def main(seasons: list[int]):
    con = connect(DB_PATH)
    con.executescript(SCHEMA)
    current_year = date.today().year
    with requests.Session() as session:
        for year in seasons:
            print(
                f"== UFA {year}: "
                f"{scrape_season(con, session, year, refresh=year == current_year)}",
                flush=True,
            )
        print(
            "== game stats: "
            f"{scrape_game_stats(con, session, refresh_years={current_year})} lines",
            flush=True,
        )
    con.close()


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or list(range(2021, date.today().year + 1)))
