"""Pull UFA teams, players, season stats, and games into data/usau.db.

Usage: python -m ufa.scrape [season ...]     (default 2012-current year)
Idempotent: rows are UPSERTed; the current season bypasses the JSON cache.
"""

from datetime import date
import sys

import requests

from scraper.build_db import DB_PATH, connect

from . import api

AUDL_START_YEAR = 2012


STAT_COLUMNS = [
    "assists", "goals", "hockeyAssists", "completions", "throwAttempts",
    "throwaways", "stalls", "drops", "blocks", "catches", "callahans",
    "oPointsPlayed", "oPointsScored", "dPointsPlayed", "dPointsScored",
    "secondsPlayed", "oOpportunities", "oOpportunityScores",
    "dOpportunities", "dOpportunityStops", "yardsThrown", "yardsReceived",
    "hucksAttempted", "hucksCompleted",
]

GAME_STAT_COLUMNS = [
    ("oPointsPlayed", "o_points_played"),
    ("dPointsPlayed", "d_points_played"),
    ("secondsPlayed", "seconds_played"),
    ("goals", "goals"),
    ("assists", "assists"),
    ("blocks", "blocks"),
    ("throwaways", "throwaways"),
    ("drops", "drops"),
    ("hockeyAssists", "hockey_assists"),
    ("completions", "completions"),
    ("throwAttempts", "throw_attempts"),
    ("stalls", "stalls"),
    ("catches", "catches"),
    ("callahans", "callahans"),
    ("yardsReceived", "yards_received"),
    ("yardsThrown", "yards_thrown"),
    ("hucksAttempted", "hucks_attempted"),
    ("hucksCompleted", "hucks_completed"),
    ("oPointsScored", "o_points_scored"),
    ("dPointsScored", "d_points_scored"),
    ("oOpportunities", "o_opportunities"),
    ("oOpportunityScores", "o_opportunity_scores"),
    ("dOpportunities", "d_opportunities"),
    ("dOpportunityStops", "d_opportunity_stops"),
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
    {", ".join(column + " INTEGER" for _, column in GAME_STAT_COLUMNS)},
    PRIMARY KEY (game_id, player_id)
);
"""


def ensure_game_stat_columns(con) -> None:
    """Migrate the old narrow game table before cached rows are backfilled."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(ufa_game_stats)")}
    for _api_name, column in GAME_STAT_COLUMNS:
        if column not in existing:
            con.execute(f"ALTER TABLE ufa_game_stats ADD COLUMN {column} INTEGER")


def scrape_game_stats(con, session, refresh_years=()) -> int:
    """Per-game rich player lines for every final game.

    Existing narrow rows are rebuilt from the disk cache without forcing live
    requests. ``refresh_years`` still bypasses the cache for genuinely mutable
    seasons.
    """
    ensure_game_stat_columns(con)
    refresh_years = set(refresh_years)
    done = {r[0] for r in con.execute(
        "SELECT DISTINCT game_id FROM ufa_game_stats")}
    rich_done = {r[0] for r in con.execute("""
        SELECT DISTINCT game_id FROM ufa_game_stats
        WHERE yards_thrown IS NOT NULL AND yards_received IS NOT NULL
          AND stalls IS NOT NULL AND callahans IS NOT NULL
    """)}
    games = con.execute(
        "SELECT game_id, year FROM ufa_games "
        "WHERE status='Final' ORDER BY year, game_id"
    ).fetchall()
    columns = ["game_id", "player_id", "team_id",
               *[column for _, column in GAME_STAT_COLUMNS]]
    placeholders = ",".join("?" for _ in columns)
    insert = (
        f"INSERT OR REPLACE INTO ufa_game_stats ({','.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    n = 0
    for gid, year in games:
        refresh = year in refresh_years
        backfill = gid not in rich_done
        if gid in done and not refresh and not backfill:
            continue
        rows = api.get(
            "playerGameStats", {"gameID": gid}, session, refresh=refresh
        )
        if not rows:
            continue
        con.execute("DELETE FROM ufa_game_stats WHERE game_id=?", (gid,))
        for row in rows:
            con.execute(
                insert,
                (gid, row["player"]["playerID"], row.get("teamID"),
                 *[row.get(api_name) for api_name, _ in GAME_STAT_COLUMNS]),
            )
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
    ensure_game_stat_columns(con)
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
    main([int(a) for a in sys.argv[1:]]
         or list(range(AUDL_START_YEAR, date.today().year + 1)))
