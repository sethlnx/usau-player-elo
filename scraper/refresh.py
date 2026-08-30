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
    """Ended events needing more results or their first coach metadata pull.

    Missing coach metadata ignores the requested season on purpose. The column
    is added at zero to an existing persistent database, so the first refresh
    after this feature lands must backfill history rather than publish coaches
    from the current season only.
    """
    q = """SELECT e.event_id, e.url, e.name, e.season,
                  COUNT(g.rowid),
                  COALESCE(SUM(g.status = 'Final' AND g.home_score IS NOT NULL
                               AND g.away_score IS NOT NULL
                               AND g.home_score + g.away_score > 0), 0),
                  e.coach_data_fetched
           FROM events e LEFT JOIN games g ON g.event_id = e.event_id
           WHERE e.division = ?
             AND COALESCE(e.end_date, e.start_date) IS NOT NULL
             AND COALESCE(e.end_date, e.start_date) < ?
           GROUP BY e.event_id"""
    out = {}
    for eid, url, name, season, total, played, coaches_fetched in con.execute(
        q, (division, today)
    ):
        missing_coaches = not coaches_fetched
        if seasons and season not in seasons and not missing_coaches:
            continue
        if missing_coaches or total == 0 or played < total:
            out[(url or "").rstrip("/")] = (
                eid, name, season, played, missing_coaches
            )
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

    # Missing coach metadata can reach beyond a requested current season during
    # the one-time migration of a persistent database.
    wanted_years = {meta[2] for meta in want.values()}
    years = sorted(set(seasons or ()) | wanted_years)
    if not years:
        years = sorted({s for (s,) in con.execute(
            "SELECT DISTINCT season FROM events WHERE division=?", (division,)
        )})
    todo = []
    listed = set()
    verified_empty = 0
    for season in years:
        for ev in list_events(division, season):
            key = (ev.get("url") or "").rstrip("/")
            listed.add(key)
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
                    todo.append(((None, ev["name"], season, 0, True), ev))

    # Some HTML-era rows describe a division the mirror does not list at all.
    # If the local row also has no teams or games, there is no staff assignment
    # to import; record that explicit negative lookup instead of retrying it
    # forever.
    for key, (eid, name, _season, _played, missing_coaches) in want.items():
        if not missing_coaches or eid is None or key in listed:
            continue
        counts = con.execute(
            """SELECT (SELECT COUNT(*) FROM games WHERE event_id=?),
                      (SELECT COUNT(*) FROM event_teams WHERE event_id=?)""",
            (eid, eid),
        ).fetchone()
        if counts != (0, 0):
            continue
        verb = "would verify" if dry_run else "verified"
        print(f"  {verb} no mirror teams for {name[:44]}", flush=True)
        if not dry_run:
            con.execute(
                "UPDATE events SET coach_data_fetched=1 WHERE event_id=?", (eid,)
            )
        verified_empty += 1
    if verified_empty and not dry_run:
        con.commit()

    if not todo:
        print(f"{division}: nothing stale or missing in {path.name}; "
              f"{verified_empty} coach-empty events verified", flush=True)
        con.close()
        return len(want), 0, 0

    replaced = added = gained = verified = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_event, ev["id"], division): (meta, ev)
                for meta, ev in todo}
        for fut in list(futs):
            (eid, name, season, played, missing_coaches), ev = futs[fut]
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
            if theirs < played:
                # Do not replace a more complete local schedule. A successful
                # coach query with no source staff is still a complete negative
                # metadata result, so persist that fact without touching games.
                has_source_staff = any(
                    (team.get("coach_source") or "").strip()
                    for team in data["teams"].values()
                )
                if missing_coaches and not has_source_staff:
                    verb = "would verify" if dry_run else "verified"
                    print(f"  {verb} no published staff for {name[:44]}",
                          flush=True)
                    if not dry_run:
                        con.execute(
                            "UPDATE events SET coach_data_fetched=1 "
                            "WHERE event_id=?", (eid,)
                        )
                        con.commit()
                    verified += 1
                elif missing_coaches:
                    print(f"  ! coach metadata pending for {name[:36]}: "
                          f"mirror has {theirs} played, local has {played}",
                          flush=True)
                continue
            if theirs == played and not missing_coaches:
                continue
            verb = ("add" if eid is None else
                    "backfill" if missing_coaches and theirs == played else "refresh")
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
            gained += max(0, theirs - played)
    con.close()
    print(f"{division}: {len(want)} stale or coach-unfetched, "
          f"{replaced} refreshed, {added} added, "
          f"{verified + verified_empty} coach-empty verified, "
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
