"""Fold the per-division scrape DBs into the main analysis DB.

club-women and club-mixed are scraped into their own SQLite files because
events.url is UNIQUE and events.division is a single column: a tournament
cross-listed across divisions ("Cooler Classic 37 (Men & Women)") is ONE url,
so scraping two divisions into one DB has each overwrite the other's division
tag. 260 club/club-mixed urls collide that way.

This merges them properly:

  * events.url loses its bare UNIQUE and gains UNIQUE(url, division), which is
    the real key — one row per division per tournament. build_db.upsert_event
    conflicts on the pair to match.
  * event_id is re-keyed. It is a local autoincrement, not USAU's id, so the
    three DBs number unrelated events identically (all 822 mixed ids collide
    with main). Each source gets a fixed offset so a re-run lands on the same
    ids and the merge stays idempotent.
  * event_team_id is USAU's own opaque key and IS globally unique (verified:
    zero collisions across the three DBs), so team rows, games and roster
    entries carry over untouched apart from the event_id remap.

Idempotent: every re-run drops the divisions it is about to import.

Usage: python -m scraper.merge_divisions
"""

import sqlite3
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
MAIN = DATA / "usau.db"

# (source db, division, event_id offset). Offsets are far above any real
# autoincrement so a merged id is readable at a glance and never collides.
SOURCES = [
    (DATA / "usau_mixed.db", "club-mixed", 1_000_000),
    (DATA / "usau_women.db", "club-women", 2_000_000),
    (DATA / "usau_college_women.db", "college-women", 3_000_000),
    (DATA / "usau_college_women_d3.db", "college-women-d3", 4_000_000),
]

# Schema and migration live in build_db, which owns them: every DB it writes
# needs the (url, division) key, not just the one this script merges into.
from .build_db import EVENT_COLS, migrate_url_key  # noqa: E402


def drop_division(con, division):
    con.execute("""DELETE FROM roster_entries WHERE event_team_id IN (
                     SELECT et.event_team_id FROM event_teams et
                     JOIN events ev USING (event_id) WHERE ev.division = ?)""",
                (division,))
    con.execute("""DELETE FROM event_teams WHERE event_id IN (
                     SELECT event_id FROM events WHERE division = ?)""",
                (division,))
    con.execute("""DELETE FROM games WHERE event_id IN (
                     SELECT event_id FROM events WHERE division = ?)""",
                (division,))
    con.execute("DELETE FROM events WHERE division = ?", (division,))


GAME_COLS = ["event_id", "game_key", "slot", "stage", "date", "time",
             "home_id", "away_id", "home_score", "away_score", "status"]
TEAM_COLS = ["event_team_id", "event_id", "display_name", "full_name", "city",
             "roster_fetched"]
ROSTER_COLS = ["event_team_id", "number", "name", "pronouns", "position",
               "height", "points", "assists", "ds", "turns"]


def copy(con, table, cols, offset=None):
    """Copy src.<table> into the main table, tolerating a missing column.

    The scrape DBs were created at different times and build_db only migrates
    new columns into whichever DB it is pointed at, so usau_mixed.db predates
    games.slot. A missing column reads as NULL rather than aborting the merge.
    """
    have = {r[1] for r in con.execute(f"PRAGMA src.table_info({table})")}
    missing = [c for c in cols if c not in have]
    if missing:
        print(f"  {table}: no {', '.join(missing)} in source, inserting NULL")
    src = []
    params = []
    for c in cols:
        if c == "event_id" and offset is not None:
            src.append("event_id + ?")
            params.append(offset)
        elif c in have:
            src.append(c)
        else:
            src.append("NULL")
    con.execute(f"INSERT INTO {table} ({', '.join(cols)}) "
                f"SELECT {', '.join(src)} FROM src.{table}", params)


def import_source(con, path, division, offset):
    if not path.exists():
        sys.exit(f"missing source db: {path}")
    con.execute("ATTACH DATABASE ? AS src", (str(path),))
    try:
        drop_division(con, division)
        cols = ", ".join(c for c in EVENT_COLS if c != "division")
        src = ", ".join("event_id + ?" if c == "event_id" else c
                        for c in EVENT_COLS if c != "division")
        con.execute(f"INSERT INTO events ({cols}, division) "
                    f"SELECT {src}, ? FROM src.events", (offset, division))
        copy(con, "event_teams", TEAM_COLS, offset)
        copy(con, "games", GAME_COLS, offset)
        copy(con, "roster_entries", ROSTER_COLS)
        con.commit()
    except Exception:
        con.rollback()   # an open transaction makes DETACH fail and mask this
        raise
    finally:
        con.execute("DETACH DATABASE src")


def main():
    con = sqlite3.connect(MAIN, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    if migrate_url_key(con):
        print("migrated events: UNIQUE(url) -> UNIQUE(url, division)")
    for path, division, offset in SOURCES:
        import_source(con, path, division, offset)
        n = con.execute("SELECT count(*) FROM events WHERE division=?",
                        (division,)).fetchone()[0]
        g = con.execute("""SELECT count(*) FROM games g JOIN events ev USING (event_id)
                           WHERE ev.division=?""", (division,)).fetchone()[0]
        r = con.execute("""SELECT count(*) FROM roster_entries re
                           JOIN event_teams et USING (event_team_id)
                           JOIN events ev USING (event_id)
                           WHERE ev.division=?""", (division,)).fetchone()[0]
        print(f"{division}: {n} events, {g} games, {r} roster entries")
    print("totals: " + ", ".join(
        f"{d} {n}" for d, n in con.execute(
            "SELECT division, count(*) FROM events GROUP BY division ORDER BY division")))
    con.close()


if __name__ == "__main__":
    main()
