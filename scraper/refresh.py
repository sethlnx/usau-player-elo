"""Bring finished-but-incomplete tournaments up to date from the GraphQL mirror.

An event scraped WHILE it is being played keeps whatever was on the schedule
page at the time, and nothing ever goes back for the rest. The 2026 U.S. Open
sat at 26 of 36 games for two days after it finished, so the tracker was
simulating a final that had already been played; the 2026 Lehigh Valley Invite
had 42 of 93. Both were fixed by hand, one event at a time, which does not
scale and does not remember.

This is that fix as a function, and it reads from the mirror rather than the
WAF-guarded HTML: one request per event instead of a schedule fetch plus a
roster fetch per team, and no rotation budget to spend.

WHICH DB GETS WRITTEN
---------------------
Not always data/usau.db. `scraper/merge_divisions.py` DROPS four divisions and
re-imports them from their own source files, so a refresh written into the main
DB for club-mixed, club-women, college-women or college-women-d3 survives
exactly until the next merge. Each division is therefore refreshed in the file
that OWNS it, and the merge is re-run afterwards -- `main` says so on exit.

WHAT COUNTS AS STALE
--------------------
The event has ended, and either holds no games at all or holds one that is not
Final. That is a cheap filter over the DB; it decides only what to ASK about.
Whether an event is actually replaced is decided on the answer: the mirror has
to hold more played games than we do. An event that merely ended with a
cancelled fixture on the books is left alone.

Replacement is wholesale, via scraper/graphql.ingest_event, because the mirror's
keys are a different namespace -- its team id is season-scoped, its game id a
content hash -- and a partial overlay would leave the event holding both. That
is also why this cannot be a score-patching pass: for a game we have no result
for, our row and the mirror's share no score to match on, and matching on the
team pair alone cannot separate a pool meeting from a bracket rematch.

Usage: python -m scraper.refresh [--division D] [--dry-run] [SEASON ...]
"""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from .build_db import DB_PATH, SCHEMA, _ensure_columns, connect
from .graphql import (API_DIVISION, WORKERS, fetch_event, ingest_event,
                      list_events, upsert_event)
from .merge_divisions import SOURCES


def owner_db(division: str):
    """The file whose rows for this division are authoritative."""
    for path, div, _ in SOURCES:
        if div == division:
            return path
    return DB_PATH


def stale_events(con, division: str, seasons: list[int] | None, today: str):
    """Ended events that look unfinished: url -> (event_id, name, played)."""
    q = """SELECT e.event_id, e.url, e.name, e.season,
                  COUNT(g.rowid),
                  COALESCE(SUM(g.status = 'Final' AND g.home_score IS NOT NULL
                               AND g.away_score IS NOT NULL
                               AND g.home_score + g.away_score > 0), 0)
           FROM events e LEFT JOIN games g ON g.event_id = e.event_id
           WHERE e.division = ? AND e.end_date IS NOT NULL AND e.end_date < ?
           GROUP BY e.event_id"""
    out = {}
    for eid, url, name, season, total, played in con.execute(q, (division, today)):
        if seasons and season not in seasons:
            continue
        # Nothing at all, or something still unplayed. Both are worth asking
        # about; neither is proof on its own.
        if total == 0 or played < total:
            out[(url or "").rstrip("/")] = (eid, name, season, played)
    return out


def refresh_division(division: str, seasons: list[int] | None, workers: int,
                     dry_run: bool) -> tuple[int, int, int]:
    """(examined, replaced, games_gained) for one division."""
    path = owner_db(division)
    if not path.exists():
        print(f"{division}: {path.name} does not exist, skipping", flush=True)
        return 0, 0, 0
    con = connect(path)
    con.executescript(SCHEMA)
    _ensure_columns(con)
    con.commit()

    today = date.today().isoformat()
    want = stale_events(con, division, seasons, today)
    if not want:
        print(f"{division}: nothing stale in {path.name}", flush=True)
        con.close()
        return 0, 0, 0

    # Only the mirror's view of the seasons actually in play is needed, and its
    # event list is the thing that maps a url onto an id we can query.
    years = seasons or sorted({s for _, _, s, _ in want.values()})
    todo = []
    for season in years:
        for ev in list_events(division, season):
            key = (ev.get("url") or "").rstrip("/")
            if key in want:
                todo.append((want[key], ev))

    replaced = gained = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_event, ev["id"], division): (meta, ev)
                for meta, ev in todo}
        for fut in list(futs):
            (eid, name, season, played), ev = futs[fut]
            try:
                data = fut.result()
            except Exception as e:
                print(f"  ! {name[:44]}: {type(e).__name__}: {e}", flush=True)
                continue
            if not data:
                continue
            theirs = sum(1 for g in data["games"]
                         if g["status"] == "Final" and g["home_id"] and g["away_id"]
                         and g["home_score"] is not None
                         and g["away_score"] is not None
                         and g["home_score"] + g["away_score"] > 0)
            if theirs <= played:
                continue          # the mirror knows no more than we do
            if dry_run:
                print(f"  would refresh {ev['startDate']} {name[:42]:44} "
                      f"{played} -> {theirs} played", flush=True)
            else:
                event_id = upsert_event(con, season, ev, division)
                ingest_event(con, event_id, data)
                con.commit()
                print(f"  {ev['startDate']} {name[:42]:44} "
                      f"{played} -> {theirs} played", flush=True)
            replaced += 1
            gained += theirs - played
    con.close()
    print(f"{division}: {len(want)} stale, {replaced} refreshed, "
          f"+{gained} played games ({path.name})", flush=True)
    return len(want), replaced, gained


def main(seasons: list[int] | None, division: str | None = None,
         workers: int = WORKERS, dry_run: bool = False):
    divisions = [division] if division else sorted(API_DIVISION)
    touched, total_replaced, total_gained = set(), 0, 0
    for div in divisions:
        _, replaced, gained = refresh_division(div, seasons, workers, dry_run)
        if replaced:
            touched.add(owner_db(div).name)
            total_replaced += replaced
            total_gained += gained
    print(f"\n{total_replaced} events refreshed, +{total_gained} played games")
    if dry_run or not total_replaced:
        return
    # The refreshed rows are only half the job: bracket structure has to be
    # re-attached, and any division living in its own file has to be merged
    # before the main DB sees it.
    merged = touched - {DB_PATH.name}
    if merged:
        print("re-run `python -m scraper.merge_divisions` "
              f"to fold in {', '.join(sorted(merged))}")
    print("then `python -m scraper.structure` and the analysis pipeline")


if __name__ == "__main__":
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    if dry:
        argv.remove("--dry-run")
    div = None
    if "--division" in argv:
        i = argv.index("--division")
        div = argv[i + 1]
        del argv[i:i + 2]
    if div and div not in API_DIVISION:
        raise SystemExit(f"unknown division {div!r}; "
                         f"choose from {', '.join(sorted(API_DIVISION))}")
    main([int(a) for a in argv] or None, div, dry_run=dry)
