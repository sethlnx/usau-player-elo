"""Orchestrate the full scrape: seasons -> events -> games -> rosters -> SQLite.

Idempotent: raw HTML is disk-cached by fetch.py, rows are UPSERTed, so
re-running after an interruption only fetches what's missing.

Usage: python -m scraper.build_db [--division club|college] [season ...]
       (defaults: club, 2021-2025)

College notes: D-I vs D-III is not a data-level division — only the event NAME
distinguishes the championship series ("D-I ... Conferences" vs "D-III ...").
We scrape all College-Men events except pure D-III/Dev series events; open
regular-season invites (mixed fields) are kept.
"""

import os
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import requests

from . import event_detail, fetch, rosters
from .events import list_events
from .fetch import SiteBlocked

# Override with USAU_DB so parallel cache-warmers can each use a throwaway DB
# while sharing the one content-addressed HTML cache (data/raw/cache/).
DB_PATH = Path(os.environ.get(
    "USAU_DB", Path(__file__).resolve().parent.parent / "data" / "usau.db"))

DIVISIONS = {
    "club": {"level": "Club-Men", "group": "Club - Men"},
    "club-women": {"level": "Club-Women", "group": "Club - Women"},
    "club-mixed": {"level": "Club-Mixed", "group": "Club - Mixed"},
    "college": {"level": "College-Men", "group": "College - Men"},
    "college-d3": {"level": "College-Men", "group": "College - Men"},
}
# club-women / club-mixed are scraped into their own DBs (data/usau_mixed.db,
# data/usau_women.db) and folded in afterwards by scraper/merge_divisions.py,
# which re-keys event_id and imports them under their own division. Keep doing
# that: it is the merge, not the scrape, that resolves the cross-listing.
# events is keyed UNIQUE(url, division), so a tournament cross-listed across
# divisions ("Cooler Classic 37 (Men & Women)") holds one row PER division
# instead of one row whose division is whichever scrape ran last. Scraping two
# divisions into one DB is therefore no longer silently lossy, but the caches
# and rotation budget are per-run, so the split DBs remain the way to run it.
# D-I and D-III share the College-Men competition level and the same schedule
# URL, so the ONLY thing separating them is the event name. `college` takes
# everything that is not D-III; `college-d3` takes exactly the D-III events.
# The two sets are disjoint by construction, which matters because events.url
# is UNIQUE and events.division is one column — overlapping sets would have
# each division silently overwriting the other's rows.
#
# Matching is deliberately loose on the hyphen. USAU writes "D-III" for the
# series but organisers write "DIII", "D3" and "D-3" for invites, and the old
# strict r"D-III" let 279 D-III games leak into the D-I pool.
_D3_MATCH = re.compile(r"D-?III\b|\bD-?3\b", re.I)
_COLLEGE_EXCLUDE = re.compile(r"\bDev\b|Developmental", re.I)
# Age-restricted club series. USAU files Masters (33+), Grandmasters (40+) and
# Great Grand Masters (50+) under the Club-Men competition level, so the event
# enumerator returns them alongside open club. They are a separate population
# playing a separate series, and rating them on the open scale is a category
# error — a masters roster's games only ever price it against other masters
# teams, so it floats free of the open pool that anchors the scale. Seen in the
# 2019 backfill ("2019 USA Ultimate North Central Masters Men's Regionals");
# expect more in 2017-2018.
_CLUB_EXCLUDE = re.compile(r"\bMasters\b|\bGrandmasters?\b|Great Grand", re.I)


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")     # concurrent readers + one writer
    con.execute("PRAGMA busy_timeout=30000")   # wait, don't error, on lock
    return con

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    season INTEGER NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    city TEXT, state TEXT,
    start_date TEXT, end_date TEXT,
    club_men_teams INTEGER,
    has_schedule INTEGER,
    division TEXT NOT NULL DEFAULT 'club',
    complete INTEGER NOT NULL DEFAULT 0,
    UNIQUE (url, division)
);
CREATE TABLE IF NOT EXISTS event_teams (
    event_team_id TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(event_id),
    display_name TEXT,
    full_name TEXT,
    city TEXT,
    roster_fetched INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS games (
    event_id INTEGER NOT NULL REFERENCES events(event_id),
    game_key TEXT NOT NULL,
    slot TEXT,
    stage TEXT, date TEXT, time TEXT,
    home_id TEXT, away_id TEXT,
    home_score INTEGER, away_score INTEGER,
    status TEXT,
    PRIMARY KEY (event_id, game_key)
);
CREATE TABLE IF NOT EXISTS roster_entries (
    event_team_id TEXT NOT NULL REFERENCES event_teams(event_team_id),
    number TEXT, name TEXT NOT NULL,
    pronouns TEXT, position TEXT, height TEXT,
    points TEXT, assists TEXT, ds TEXT, turns TEXT,
    PRIMARY KEY (event_team_id, name, number)
);
"""


def parse_dates(dates_text: str) -> tuple[str | None, str | None]:
    """'Jul 13, 2024 - Jul 14, 2024' -> ('2024-07-13', '2024-07-14')."""
    found = re.findall(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", dates_text)
    iso = []
    for mon, day, year in found[:2]:
        try:
            iso.append(datetime.strptime(f"{mon} {day} {year}", "%b %d %Y").date().isoformat())
        except ValueError:
            iso.append(None)
    iso += [None] * (2 - len(iso))
    return iso[0], iso[1]


def upsert_event(con, season: int, ev: dict, division: str = "club") -> int:
    start, end = parse_dates(ev["dates"])
    group_name = DIVISIONS[division]["group"]
    group = next((g for g in ev["groups"] if group_name in g), "")
    m = re.search(r"\[(\d+)\]", group)
    con.execute(
        """INSERT INTO events (season, name, url, city, state, start_date, end_date,
                               club_men_teams, division)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(url, division) DO UPDATE SET season=excluded.season, name=excluded.name,
             city=excluded.city, state=excluded.state, start_date=excluded.start_date,
             end_date=excluded.end_date, club_men_teams=excluded.club_men_teams,
             division=excluded.division""",
        (season, ev["name"], ev["url"], ev["city"], ev["state"], start, end,
         int(m.group(1)) if m else None, division))
    return con.execute("SELECT event_id FROM events WHERE url=? AND division=?",
                       (ev["url"], division)).fetchone()[0]


def scrape_event(con, event_id: int, ev: dict, season: int, division: str = "club",
                 session=None) -> str:
    start, end = parse_dates(ev["dates"])
    # A schedule cached on or before the event's last day may be missing final
    # scores (fetched mid-event) — refetch it live.
    cached = fetch.cached_date(event_detail.schedule_url(ev["url"], division))
    stale = cached is not None and end is not None and cached <= end
    html = event_detail.fetch_schedule(ev["url"], division, session=session, refresh=stale)
    if html is None:
        con.execute("UPDATE events SET has_schedule=0 WHERE event_id=?", (event_id,))
        return "no schedule"

    year = int(start[:4]) if start else season
    games, teams = event_detail.parse_games(html, year)
    con.execute("UPDATE events SET has_schedule=1 WHERE event_id=?", (event_id,))

    for tid, name in teams.items():
        con.execute(
            """INSERT INTO event_teams (event_team_id, event_id, display_name)
               VALUES (?,?,?)
               ON CONFLICT(event_team_id) DO UPDATE SET display_name=excluded.display_name""",
            (tid, event_id, name))
    for g in games:
        con.execute(
            """INSERT OR REPLACE INTO games
               (event_id, game_key, slot, stage, date, time, home_id, away_id,
                home_score, away_score, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, g["game_key"], g["slot"], g["stage"], g["date"], g["time"],
             g["home_id"], g["away_id"], g["home_score"], g["away_score"], g["status"]))
    _drop_reseeded(con, event_id, games)

    # Commit game/team rows now and after each roster: fetches below can sleep
    # through long probe ladders, and an open write txn would lock the DB out
    # for other writers (e.g. the UFA scraper) the whole time.
    con.commit()
    fetched = 0
    for tid in teams:
        done = con.execute(
            "SELECT roster_fetched FROM event_teams WHERE event_team_id=?", (tid,)).fetchone()
        if done and done[0]:
            continue
        try:
            page = rosters.fetch_team_page(tid, session=session)
        except SiteBlocked:
            raise  # sustained block: propagate so the run pauses cleanly
        except Exception as e:  # keep going; report at the end
            print(f"    ! roster fetch failed for {teams[tid]}: {e}", flush=True)
            continue
        parsed = rosters.parse_team_page(page)
        con.execute(
            "UPDATE event_teams SET full_name=?, city=?, roster_fetched=1 WHERE event_team_id=?",
            (parsed["team"].get("name"), parsed["team"].get("city"), tid))
        for p in parsed["players"]:
            con.execute(
                """INSERT OR REPLACE INTO roster_entries
                   (event_team_id, number, name, pronouns, position, height,
                    points, assists, ds, turns)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (tid, p["number"], p["name"], p["pronouns"], p["position"],
                 p["height"], p["points"], p["assists"], p["ds"], p["turns"]))
        con.commit()
        fetched += 1
    return f"{len(games)} games, {len(teams)} teams, {fetched} rosters fetched"


def _ensure_columns(con):
    """Older DBs predate some columns; add them with backfill-safe defaults."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
    if "division" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN division TEXT NOT NULL DEFAULT 'club'")
    if "complete" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN complete INTEGER NOT NULL DEFAULT 0")
    if "slot" not in [r[1] for r in con.execute("PRAGMA table_info(games)")]:
        con.execute("ALTER TABLE games ADD COLUMN slot TEXT")


def _drop_reseeded(con, event_id: int, games: list[dict]):
    """Delete rows left behind when USAU seeds a slot that was TBD.

    An unseeded fixture has no EventGameId, so parse_games keys it on the page
    slot ("bracket-game411456"); once the teams are known the same slot carries
    a real game id and inserts under a different key. Without this the event
    accumulates a teamless twin of every bracket game as the tournament runs —
    invisible to the model (it filters NULL teams) but not to anything reading
    the schedule: the U.S. Open's four prequarterfinals became eight.

    Matching is on the slot, so a row is only ever dropped by the fixture that
    replaced it. Slots predating the column are NULL, hence the second pass on
    the synthetic key form.
    """
    slots = [g["slot"] for g in games if g["slot"]]
    if not slots:
        return
    keys = [g["game_key"] for g in games]
    kq = ",".join("?" * len(keys))
    con.execute(
        f"""DELETE FROM games WHERE event_id=? AND slot IN ({",".join("?" * len(slots))})
            AND game_key NOT IN ({kq})""", [event_id, *slots, *keys])
    legacy = [f"{p}-{s}" for s in slots for p in ("pool", "bracket")]
    con.execute(
        f"""DELETE FROM games WHERE event_id=? AND slot IS NULL
            AND game_key IN ({",".join("?" * len(legacy))})
            AND game_key NOT IN ({kq})""", [event_id, *legacy, *keys])


def _mark_if_complete(con, event_id: int):
    """Flag an event as done so resume runs skip it without parsing.

    Complete = the event has ended and either has no schedule page or has
    every roster fetched. Failed roster fetches keep it incomplete, so they
    retry on the next run.
    """
    end_date, has_schedule, missing = con.execute(
        """SELECT e.end_date, e.has_schedule,
                  (SELECT COUNT(*) FROM event_teams t
                   WHERE t.event_id = e.event_id AND t.roster_fetched = 0)
           FROM events e WHERE e.event_id = ?""", (event_id,)).fetchone()
    if (end_date and end_date < date.today().isoformat()
            and (has_schedule == 0 or missing == 0)):
        con.execute("UPDATE events SET complete=1 WHERE event_id=?", (event_id,))


def main(seasons: list[int], division: str = "club"):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = connect(DB_PATH)
    con.executescript(SCHEMA)
    _ensure_columns(con)

    group_name = DIVISIONS[division]["group"]
    level = DIVISIONS[division]["level"]
    session = requests.Session()
    for season in seasons:
        try:
            season_events = list_events(season, level, session=session,
                                        refresh=season >= date.today().year)
        except SiteBlocked as e:
            con.commit()
            print(f"\n!! BLOCKED during season {season} enumeration: {e}", flush=True)
            con.close()
            sys.exit(3)
        events = [e for e in season_events
                  if any(group_name in g for g in e["groups"])
                  and "cancel" not in e["name"].lower()]
        if division == "college":
            events = [e for e in events
                      if not _COLLEGE_EXCLUDE.search(e["name"])
                      and not _D3_MATCH.search(e["name"])]
        elif division == "college-d3":
            events = [e for e in events
                      if not _COLLEGE_EXCLUDE.search(e["name"])
                      and _D3_MATCH.search(e["name"])]
        elif division.startswith("club"):
            events = [e for e in events if not _CLUB_EXCLUDE.search(e["name"])]
        print(f"== season {season}: {len(events)} {level} events", flush=True)
        skipped = 0
        for ev in sorted(events, key=lambda e: e["dates"]):
            # Complete only counts for the division it was scraped under: each
            # division has its own schedule page, and events is keyed
            # (url, division) so a cross-listed tournament holds a row per
            # division rather than one row they overwrite in turn.
            prior = con.execute(
                "SELECT complete FROM events WHERE url=? AND division=?",
                (ev["url"], division)).fetchone()
            event_id = upsert_event(con, season, ev, division)
            if prior and prior[0]:
                skipped += 1
                continue
            try:
                result = scrape_event(con, event_id, ev, season, division, session)
            except SiteBlocked as e:
                con.commit()
                print(f"\n!! BLOCKED at {ev['name']}: {e}", flush=True)
                print("!! Progress saved. Switch VPN and re-run to resume.", flush=True)
                con.close()
                sys.exit(3)
            except Exception as e:
                print(f"  {ev['name']}: FAILED {e}", flush=True)
                continue
            _mark_if_complete(con, event_id)
            con.commit()
            print(f"  {ev['name']}: {result}", flush=True)
        if skipped:
            print(f"  ({skipped} already-complete events skipped)", flush=True)
        con.commit()

    session.close()
    con.close()


if __name__ == "__main__":
    argv = sys.argv[1:]
    division = "club"
    if "--division" in argv:
        i = argv.index("--division")
        division = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    elif any(a.startswith("--division=") for a in argv):
        division = next(a.split("=", 1)[1] for a in argv if a.startswith("--division="))
        argv = [a for a in argv if not a.startswith("--division=")]
    if division not in DIVISIONS:
        sys.exit(f"unknown division {division!r}; choose from {list(DIVISIONS)}")
    seasons = [int(a) for a in argv] or [2021, 2022, 2023, 2024, 2025]
    main(seasons, division)
