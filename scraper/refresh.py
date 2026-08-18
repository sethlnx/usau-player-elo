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

THE AUTHORITATIVE DB
--------------------
The default GraphQL build stores every division in ``data/usau.db``. Refreshes
therefore update that unified database directly. The split per-division files
belong only to the retired HTML ingest; routing fresh rows through them would
let a later legacy merge overwrite the unified corpus with stale data.

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




def stale_events(con, division: str, seasons: list[int] | None, today: str):
    """Ended events that look unfinished: url -> (event_id, name, played).

    The end is COALESCE(end_date, start_date), not end_date. A one-day event
    prints one date, so `build_db.parse_dates` leaves end_date NULL, and 307
    events in the corpus have no end at all -- requiring one skipped every one
    of them. MOB Invite 2026 and Twin Cities Rollaround 2026 both sat at zero
    played games with the mirror holding twelve because of exactly that.
    """
    q = """SELECT e.event_id, e.url, e.name, e.season,
                  COUNT(g.rowid),
                  COALESCE(SUM(g.status = 'Final' AND g.home_score IS NOT NULL
                               AND g.away_score IS NOT NULL
                               AND g.home_score + g.away_score > 0), 0)
           FROM events e LEFT JOIN games g ON g.event_id = e.event_id
           WHERE e.division = ?
             AND COALESCE(e.end_date, e.start_date) IS NOT NULL
             AND COALESCE(e.end_date, e.start_date) < ?
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
    path = DB_PATH
    if not path.exists():
        print(f"{division}: {path.name} does not exist, skipping", flush=True)
        return 0, 0, 0
    con = connect(path)
    con.executescript(SCHEMA)
    _ensure_columns(con)
    con.commit()

    today = date.today().isoformat()
    want = stale_events(con, division, seasons, today)
    have = {(u or "").rstrip("/") for (u,) in con.execute(
        "SELECT url FROM events WHERE division=?", (division,))}

    # Only the mirror's view of the seasons actually in play is needed, and its
    # event list is the thing that maps a url onto an id we can query.
    years = seasons or sorted(
        {s for _, _, s, _ in want.values()} or
        {s for (s,) in con.execute(
            "SELECT DISTINCT season FROM events WHERE division=?", (division,))})
    todo = []
    for season in years:
        for ev in list_events(division, season):
            key = (ev.get("url") or "").rstrip("/")
            if key in want:
                todo.append((want[key], ev))
            elif key not in have:
                # An event we never enumerated at all. USAU's own search
                # postback drops some -- the 2026 Western NY D-III Women's
                # Conferences is in the mirror and in no scrape of ours -- so a
                # PAST one with no row here is a gap, not a fixture list we are
                # early for. Future events are left to the ordinary scrape.
                ends = ev.get("endDate") or ev.get("startDate")
                if ends and ends < today and "cancel" not in (ev["name"] or "").lower():
                    todo.append(((None, ev["name"], season, 0), ev))

    if not todo:
        print(f"{division}: nothing stale or missing in {path.name}", flush=True)
        con.close()
        return 0, 0, 0

    replaced = added = gained = 0
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
            verb = "add" if eid is None else "refresh"
            if dry_run:
                print(f"  would {verb} {ev['startDate']} {name[:40]:42} "
                      f"{played} -> {theirs} played", flush=True)
            else:
                event_id = upsert_event(con, season, ev, division)
                ingest_event(con, event_id, data)
                con.commit()
                print(f"  {verb:7} {ev['startDate']} {name[:40]:42} "
                      f"{played} -> {theirs} played", flush=True)
            if eid is None:
                added += 1
            else:
                replaced += 1
            gained += theirs - played
    con.close()
    print(f"{division}: {len(want)} stale, {replaced} refreshed, {added} added, "
          f"+{gained} played games ({path.name})", flush=True)
    return len(want), replaced + added, gained


def main(seasons: list[int] | None, division: str | None = None,
         workers: int = WORKERS, dry_run: bool = False):
    divisions = [division] if division else sorted(API_DIVISION)
    total_replaced, total_gained = 0, 0
    for div in divisions:
        _, replaced, gained = refresh_division(div, seasons, workers, dry_run)
        total_replaced += replaced
        total_gained += gained
    print(f"\n{total_replaced} events refreshed, +{total_gained} played games")
    if dry_run or not total_replaced:
        return
    # The refreshed rows are only half the job: bracket structure and every
    # derived publication artifact must be rebuilt before the result is used.
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
