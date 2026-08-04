"""Pull events, games, teams and rosters from the usau-rankings GraphQL mirror.

play.usaultimate.org is WAF-guarded, so the HTML path in fetch.py runs at one
request every 1.5s and needs a request per event page PLUS one per team roster
— roughly seven per event, tens of thousands for a full corpus, punctuated by
sustained blocks that need a VPN switch. The mirror at usau-rankings.fly.dev
answers the same data in ONE request per event (teams, rosters and games
together) with no rate limiting observed at 32-way concurrency.

Fidelity was checked against the HTML scrape on the 2024 D-III College
Championships women's division: 47/47 games with identical score multisets,
16/16 teams, 320/320 roster lines. See `validate` below, which re-runs that
comparison over a whole division.

WHAT THE MIRROR KEYS ON, AND WHY IT FORCES A CLEAN REPLACE
----------------------------------------------------------
Two identifiers do not line up with the HTML path, both structurally:

  * `SeasonTeam.id` is SEASON-scoped — one id is reused across every event the
    team enters — while `event_teams.event_team_id` is per (event, team) and is
    a bare PRIMARY KEY. Inserting the mirror's id directly would collapse a
    team's six events into one row and cross-link their rosters. Team keys are
    therefore synthesized per (event, team) and namespaced `gq:` so they can
    never collide with a real USAU EventTeamId.
  * `Game.gameId` is a content hash, not USAU's numeric game id, so game_key
    values do not match HTML-scraped rows either.

Consequently an event must be sourced from exactly ONE provider: mixing them
duplicates every team and every game under two key namespaces. `ingest_event`
therefore REPLACES an event's teams, games and rosters wholesale, which also
makes re-runs idempotent. `_drop_reseeded` is unnecessary here for the same
reason — a replace cannot leave a reseeded twin behind.

DIFFERENCES FROM THE HTML PARSE
-------------------------------
  * Pool labels are RIGHT here and wrong in the HTML parse. On the 2024 D-III
    women's event the mirror distributes 30 pool games evenly over Pools A-E;
    event_detail.parse_games lumps them into "Pool D" (24) and "Pool E" (6).
    tournaments.py groups pools off this label, so the mirror is the better
    source, not merely an equivalent one.
  * Bracket-game times arrive WITHOUT AM/PM ("1:00"), pool games with it
    ("1:00 PM"). analysis/tournaments._minutes reads a bare time as AM, which
    would sort a 1pm final before an 8:30am pool game and misorder brackets, so
    `_meridiem` restores it — see that function for the rule.
  * `slot` is only available for bracket games (`domId`, e.g. "game337311",
    the same form the HTML yields). Pool games have no domId; slot stays NULL.
    Nothing keys on slot — game_key does — and _slotnum already handles NULL.
  * Box scores (goals/assists/blocks/turnovers) exist only at championship
    events, because that is the only place USAU records them. Every other
    event still yields the full roster, names and numbers, which is what the
    roster model consumes.
  * `Event` carries no season, so season is the calendar year of startDate.
    That matches this DB's existing convention exactly, including the
    COVID-shifted 2021 college season whose events sit in Oct/Nov 2021.

THE ONE REAL DATA LOSS, MEASURED
--------------------------------
At a handful of events the mirror holds a played game's score and ONE side,
with no opponent to name, where the HTML page prints both. It is not
recoverable from the mirror — querying each team's own game list returns only
the side already known — so the game is dropped rather than half-attributed,
which keeps a wrong edge out of the model at the cost of a real one.

Measured with `validate` over two full divisions against data/usau.db and
data/usau_college_women_d3.db:

    college-women-d3   158 shared events   2,406 played games   0 lost
    college-d3         249 shared events   4,943 played games  18 lost (0.36%,
                                                               at 3 events)

Roster line counts match on 158/158 women's events (19,032 lines) and 248/249
men's (41,321). The single exception gains one player the HTML lacks. Names
differ on 0.2-0.5% of lines, and every difference inspected is one person
under two spellings: the mirror serves the player's CURRENT USAU name where
the HTML snapshot froze it at scrape time, so 'alice winebrenner' reads 'alice
baker' and 'topher olson' reads 'christopher olson'. For cross-season player
identity that is the better behaviour — one person keeps one name instead of
splitting at a surname change — but it is a difference, not a fix, and
identity/resolve.py is where it lands.
"""

import collections
import hashlib
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .build_db import (SCHEMA, _CLUB_EXCLUDE, _COLLEGE_EXCLUDE, _D3_MATCH,
                      _ensure_columns, connect)

GQL_URL = os.environ.get("USAU_GQL_URL", "https://usau-rankings.fly.dev/api/graphql")
# Separate from data/usau.db by default: the mirror's key namespaces cannot be
# mixed into an HTML-scraped event (see module docstring), so the two live in
# different files until a comparison says otherwise.
DB_PATH = Path(os.environ.get(
    "USAU_GQL_DB", Path(__file__).resolve().parent.parent / "data" / "usau_gql.db"))
WORKERS = int(os.environ.get("USAU_GQL_WORKERS", "12"))
# Every Relay connection on this endpoint truncates at 100 no matter what
# `first` asks for (verified: 2025 Club Nationals returns 100 + hasNextPage for
# first:400), so anything that can exceed 100 rows must be paginated.
PAGE = 100

# Our division key -> the mirror's (level, division). D-I and D-III share a
# competition level upstream and the mirror models no D-III flag either, so the
# split stays on the event NAME via build_db._D3_MATCH, exactly as the HTML
# path does it.
API_DIVISION = {
    "club-men": ("Club", "Men"),
    "club-women": ("Club", "Women"),
    "club-mixed": ("Club", "Mixed"),
    "college": ("College", "Men"),
    "college-d3": ("College", "Men"),
    "college-women": ("College", "Women"),
    "college-women-d3": ("College", "Women"),
}


class GraphQLError(RuntimeError):
    """The endpoint answered, but with GraphQL errors rather than data."""


def post(query: str, variables: dict | None = None, tries: int = 4) -> dict:
    """One GraphQL round trip, retrying transport faults with backoff.

    GraphQL validation errors are NOT retried: they are a bug in the query and
    will fail identically forever.
    """
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(
            GQL_URL, data=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": "usau-player-elo/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                payload = json.loads(r.read())
            break
        except (urllib.error.URLError, ssl.SSLError, TimeoutError,
                json.JSONDecodeError) as e:
            last = e
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * 2 ** attempt)
    else:  # pragma: no cover - loop always breaks or raises
        raise last
    if payload.get("errors"):
        raise GraphQLError(json.dumps(payload["errors"])[:600])
    return payload["data"]


def _meridiem(t: str | None) -> str | None:
    """Restore AM/PM on a bare "H:MM", which is how bracket games arrive.

    Ultimate is played in daylight: a tournament day runs from roughly 8am to
    about 8pm and no fixture is scheduled between midnight and 7am. Hours 8-11
    are therefore morning and 12-7 afternoon, which reproduces every bare value
    the mirror emits (8:30, 10:45, 11:15 -> AM; 1:00, 1:30, 3:45, 4:30, 6:00 ->
    PM). Values that already carry a meridiem, or that do not parse, pass
    through untouched rather than being guessed at.
    """
    if not t:
        return t
    s = t.strip()
    if not s or s.upper().endswith(("AM", "PM")):
        return s
    head, _, tail = s.partition(":")
    if not head.isdigit() or not tail[:2].isdigit():
        return s
    hour = int(head)
    if not 1 <= hour <= 12:
        return s
    return f"{s} {'AM' if 8 <= hour <= 11 else 'PM'}"


def _score(v) -> int | None:
    """Scores arrive as strings; "" and "W"/"L" forfeit marks are not numbers."""
    if v is None:
        return None
    s = str(v).strip()
    return int(s) if s.isdigit() else None


def _stage(g: dict) -> str | None:
    """The organiser's own stage label, as the HTML page shows it.

    `roundName` IS that label, quirks intact: on the 2019 D-III Championships
    it yields 'Quarters', 'Prequarters', '9th place semis' and '9th place
    Semis' — the same strings, including the inconsistent casing, that the
    HTML parse produces. So it is used verbatim.

    `bracket.name` is deliberately NOT folded in. It is free text too, and a
    verbose kind ('Championship Bracket', 'Second Place Bracket', '9th place
    (tie) 1'), so joining the two reads '9th place (tie) 1 9th place semis'.
    That loses more brackets in analysis/tournaments than it gains: where
    roundName omits the placement ('Semifinal' for a ninth-place semi) the
    HTML label omits it too, and brackets_of recovers the bracket structurally
    for both sources alike. Faithfulness beats a partial repair here.

    Pool games carry no roundName, so they fall through to the pool name —
    which is the one label the mirror gets RIGHT and the HTML parse does not.

    `description` is the last resort, for games the mirror attaches to no
    round, pool or bracket at all. It is what rescues pool-play completion
    rounds: at the 2018 Pennsylvania CC twelve such games carry nothing but
    description 'Pool Play Completion', which POOLISH matches, where the HTML
    parse files them under '1st Place' and calls them a bracket. Its
    "<pool> Schedule & Scores" form is redundant with pool.name, so the suffix
    comes off rather than reaching `classify` as noise.
    """
    desc = (g.get("description") or "").strip()
    desc = re.sub(r"\s+Schedule\s*&\s*Scores$", "", desc, flags=re.I)
    return (g.get("roundName") or (g.get("pool") or {}).get("name")
            or (g.get("bracket") or {}).get("name") or desc or None)


def team_key(event_api_id: str, team_api_id: str) -> str:
    """Per-(event, team) key in a namespace disjoint from USAU EventTeamIds.

    Needed because SeasonTeam.id is season-scoped and would otherwise fold a
    team's whole season into one event_teams row.
    """
    h = hashlib.sha1(f"{event_api_id}\x00{team_api_id}".encode()).hexdigest()
    return f"gq:{h[:32]}"


EVENT_LIST = """
query($f:EventFilter,$n:Int,$a:String){
  events(filter:$f, first:$n, after:$a, orderBy:{field:startDate,direction:asc}){
    pageInfo{ hasNextPage endCursor }
    edges{ node{ id name url city state startDate endDate } } } }
"""

EVENT_CORE = """
query($id:ID!){
  event(id:$id){
    id name url city state startDate endDate
    divisions{ division level
      teams{ team{ id name city state season } } } }
}
"""

EVENT_TEAMS = """
query($id:ID!,$n:Int,$a:String){
  event(id:$id){ teams(first:$n, after:$a){
    pageInfo{ hasNextPage endCursor }
    edges{ node{
      team{ id name city state season }
      statLines{ name number year position height pronouns
                 goals assists blocks turnovers } } } } }
}
"""

EVENT_GAMES = """
query($id:ID!,$n:Int,$a:String){
  event(id:$id){ games(first:$n, after:$a){
    pageInfo{ hasNextPage endCursor }
    edges{ node{
      gameId domId date time status roundName description
      pool{ name } bracket{ name }
      team1{ id } team1Score team2{ id } team2Score } } } }
}
"""


def _pages(query: str, event_id: str, field: str):
    """Every edge of a paginated field on one event, following pageInfo."""
    after = None
    while True:
        conn = post(query, {"id": event_id, "n": PAGE, "a": after})["event"][field]
        for edge in conn["edges"]:
            yield edge["node"]
        if not conn["pageInfo"]["hasNextPage"]:
            return
        after = conn["pageInfo"]["endCursor"]


def list_events(division: str, season: int) -> list[dict]:
    """The mirror's events for one division-season, after our own name filters.

    The mirror has no D-III flag and files Masters under open club, so the
    same name rules the HTML path applies are applied here — otherwise 279
    D-III games leak into D-I and masters teams get rated on the open scale.
    """
    level, div = API_DIVISION[division]
    flt = {"level": level, "division": div,
           "startDateMin": f"{season}-01-01", "startDateMax": f"{season}-12-31"}
    out, after = [], None
    while True:
        conn = post(EVENT_LIST, {"f": flt, "n": PAGE, "a": after})["events"]
        out += [e["node"] for e in conn["edges"]]
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]

    keep = []
    for ev in out:
        name = ev["name"] or ""
        if division.startswith("college"):
            if _COLLEGE_EXCLUDE.search(name):
                continue
            is_d3 = bool(_D3_MATCH.search(name))
            if is_d3 != division.endswith("-d3"):
                continue
        elif _CLUB_EXCLUDE.search(name):
            continue
        keep.append(ev)
    return keep


def fetch_event(event_api_id: str, division: str) -> dict | None:
    """One event's rows for one division: teams, rosters and games.

    Returns None when the event has no page for this division.

    A division is matched on LEVEL AND DIVISION, never division alone. One
    event routinely files the same division name at several levels — the U.S.
    Open carries Mixed/Club beside Mixed/Youth Club U-20, the Beach
    Championships Men/Club beside Men/Grand Masters — so matching on the name
    alone picks whichever the mirror happens to list first and can rate a youth
    or masters squad on the open club scale. `_CLUB_EXCLUDE` cannot catch that:
    it reads the EVENT name, and "Garden State (Club) 2018" is not suspicious.

    Team rows are seeded from the DIVISION's membership list, which is
    authoritative, and `Event.teams` then only adds the per-event stat lines.
    That ordering matters: at the 2017 Carolina D-I CC the division lists 12
    teams while the event-level connection is empty, and sourcing teams from
    the latter dropped all 34 played games for want of anyone to attribute them
    to. This way the games survive and only the roster lines are missing.

    Games come from the EVENT-level connection and are attributed to a
    division by team membership. When the event holds a single division that
    attribution is unconditional, which keeps fixtures whose sides are still
    TBD (null team ids) — the division-scoped connection is otherwise
    equivalent but would need re-fetching every division to paginate.
    """
    want_level, want_div = API_DIVISION[division]
    core = post(EVENT_CORE, {"id": event_api_id})["event"]
    divs = core.get("divisions") or []
    mine = next((d for d in divs if d.get("division") == want_div
                 and d.get("level") == want_level), None)
    if mine is None:
        return None

    members = {t["team"]["id"]: t["team"]
               for t in (mine.get("teams") or []) if t.get("team")}
    sole = len(divs) == 1

    teams = {tid: {
        "event_team_id": team_key(event_api_id, tid),
        # The mirror serves one name, "School (Nickname)", where the HTML
        # path splits a short display_name from a longer full_name. Both
        # columns get it: display_name is what every downstream join and
        # every identity key reads, so it must not be the empty half.
        "display_name": t.get("name"),
        "full_name": t.get("name"),
        "city": t.get("city"),
        "season": t.get("season"),
    } for tid, t in members.items()}

    rosters = {}
    for node in _pages(EVENT_TEAMS, event_api_id, "teams"):
        t = node.get("team")
        if not t or t["id"] not in members:
            continue
        rosters[team_key(event_api_id, t["id"])] = node.get("statLines") or []

    games = []
    for g in _pages(EVENT_GAMES, event_api_id, "games"):
        h = (g.get("team1") or {}).get("id")
        a = (g.get("team2") or {}).get("id")
        if not sole and h not in members and a not in members:
            continue  # belongs to another division of a cross-listed event
        stage = _stage(g)
        games.append({
            "game_key": g["gameId"],
            "slot": g.get("domId"),
            "stage": stage,
            "date": g.get("date"),
            "time": _meridiem(g.get("time")),
            "home_id": team_key(event_api_id, h) if h in members else None,
            "away_id": team_key(event_api_id, a) if a in members else None,
            "home_score": _score(g.get("team1Score")),
            "away_score": _score(g.get("team2Score")),
            "status": g.get("status"),
        })
    return {"event": core, "teams": teams, "rosters": rosters, "games": games}


def upsert_event(con, season: int, ev: dict, division: str) -> int:
    """events row for one mirror event, keyed the same as the HTML path.

    The mirror's url carries a trailing slash and the HTML path's does not;
    it is stripped so a row written by either source has one identity under
    UNIQUE(url, division).
    """
    url = (ev.get("url") or "").rstrip("/")
    con.execute(
        """INSERT INTO events (season, name, url, city, state, start_date, end_date,
                               division, has_schedule)
           VALUES (?,?,?,?,?,?,?,?,1)
           ON CONFLICT(url, division) DO UPDATE SET season=excluded.season,
             name=excluded.name, city=excluded.city, state=excluded.state,
             start_date=excluded.start_date, end_date=excluded.end_date,
             has_schedule=1""",
        (season, ev["name"], url, ev.get("city"), ev.get("state"),
         ev.get("startDate"), ev.get("endDate"), division))
    return con.execute("SELECT event_id FROM events WHERE url=? AND division=?",
                       (url, division)).fetchone()[0]


def ingest_event(con, event_id: int, data: dict) -> str:
    """Replace one event's teams, games and rosters with the mirror's.

    A wholesale replace, not an upsert: the mirror's team and game keys are a
    different namespace from the HTML path's, so a partial overlay would leave
    the event holding both and double every fixture.
    """
    old = [r[0] for r in con.execute(
        "SELECT event_team_id FROM event_teams WHERE event_id=?", (event_id,))]
    if old:
        con.executemany("DELETE FROM roster_entries WHERE event_team_id=?",
                        [(t,) for t in old])
    con.execute("DELETE FROM games WHERE event_id=?", (event_id,))
    con.execute("DELETE FROM event_teams WHERE event_id=?", (event_id,))

    for t in data["teams"].values():
        con.execute(
            """INSERT INTO event_teams (event_team_id, event_id, display_name,
                                        full_name, city, roster_fetched)
               VALUES (?,?,?,?,?,1)""",
            (t["event_team_id"], event_id, t["display_name"], t["full_name"],
             t["city"]))
    lines = 0
    for key, roster in data["rosters"].items():
        for p in roster:
            # points/assists/ds/turns are TEXT in this schema because the HTML
            # carries them as text; keep that, and keep "" for the events where
            # USAU records no box score rather than inventing a 0.
            con.execute(
                """INSERT OR REPLACE INTO roster_entries
                   (event_team_id, number, name, pronouns, position, height,
                    points, assists, ds, turns)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (key, p.get("number"), p.get("name"), p.get("pronouns"),
                 p.get("position"), p.get("height"),
                 str(p["goals"]) if p.get("goals") else "",
                 str(p["assists"]) if p.get("assists") else "",
                 str(p["blocks"]) if p.get("blocks") else "",
                 str(p["turnovers"]) if p.get("turnovers") else ""))
            lines += 1
    for g in data["games"]:
        con.execute(
            """INSERT OR REPLACE INTO games
               (event_id, game_key, slot, stage, date, time, home_id, away_id,
                home_score, away_score, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, g["game_key"], g["slot"], g["stage"], g["date"],
             g["time"], g["home_id"], g["away_id"], g["home_score"],
             g["away_score"], g["status"]))
    return f"{len(data['games'])} games, {len(data['teams'])} teams, {lines} roster lines"


def validate(html_db: str, gql_db: str, division: str = "college-women-d3"):
    """Compare a mirror-built DB against an HTML-scraped one, event by event.

    Comparison is deliberately NAME-AGNOSTIC on teams. The two sources print a
    team differently — "Haverford" against "Haverford/Bryn Mawr (Sneetches)" —
    so matching on names reports ~40% of events as differing when the fixtures
    are identical. What is compared instead is what the model actually reads.

    The GATE is the model-visible corpus: games with both sides, status Final
    and a real score. Fixtures that nobody played are reported but do not fail
    the comparison — the mirror drops the losing side of an unplayed pool game
    where the HTML page still prints both names, so a strict comparison flags
    events (2019 Atlantic Coast CC) whose 17 played games agree exactly.

    Prints a report and returns True when every shared event agrees on its
    model-visible score multiset and its roster line count.
    """
    def rd(path):
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return c, {u.rstrip("/"): (e, n, d) for e, u, n, d in c.execute(
            "SELECT event_id,url,name,start_date FROM events WHERE division=?",
            (division,))}

    H, he = rd(html_db)
    G, ge = rd(gql_db)
    shared = sorted(set(he) & set(ge), key=lambda u: he[u][2] or "")

    def scores(c, eid):
        """Multiset of score pairs over PLAYED games — the model's own filter."""
        return collections.Counter(
            tuple(sorted((a, b))) for a, b in c.execute(
                """SELECT home_score,away_score FROM games WHERE event_id=?
                   AND home_id IS NOT NULL AND away_id IS NOT NULL
                   AND status='Final' AND home_score IS NOT NULL
                   AND away_score IS NOT NULL AND home_score+away_score>0""",
                (eid,)))

    def unplayed(c, eid):
        return c.execute(
            """SELECT COUNT(*) FROM games WHERE event_id=?
               AND NOT (home_id IS NOT NULL AND away_id IS NOT NULL
                        AND status='Final' AND home_score IS NOT NULL
                        AND away_score IS NOT NULL
                        AND home_score+away_score>0)""", (eid,)).fetchone()[0]

    def orphans(c, eid):
        """Played games the source scored but could not attribute to two teams.

        The mirror's one real data gap: at a few events it holds the score and
        one side and simply has no opponent to name. Such a game is excluded
        from the model rather than wrongly attributed, so the failure is safe —
        but it IS lost, and this counts it.
        """
        return c.execute(
            """SELECT COUNT(*) FROM games WHERE event_id=? AND status='Final'
               AND home_score IS NOT NULL AND away_score IS NOT NULL
               AND home_score+away_score>0
               AND (home_id IS NULL OR away_id IS NULL)""", (eid,)).fetchone()[0]

    def people(c, eid):
        return {re.sub(r"\s+", " ", n.strip()).lower() for (n,) in c.execute(
            """SELECT r.name FROM roster_entries r
               JOIN event_teams t ON t.event_team_id=r.event_team_id
               WHERE t.event_id=?""", (eid,))}

    bad_scores, bad_counts, name_var = [], [], 0
    roster_lines = played = unplayed_h = unplayed_g = 0
    orph_h = orph_g = 0
    for u in shared:
        hs, gs = scores(H, he[u][0]), scores(G, ge[u][0])
        played += sum(gs.values())
        unplayed_h += unplayed(H, he[u][0])
        unplayed_g += unplayed(G, ge[u][0])
        orph_h += orphans(H, he[u][0])
        orph_g += orphans(G, ge[u][0])
        if hs != gs:
            bad_scores.append((he[u][2], he[u][1], sum(hs.values()), sum(gs.values())))
        hp, gp = people(H, he[u][0]), people(G, ge[u][0])
        roster_lines += len(gp)
        if len(hp) != len(gp):
            bad_counts.append((he[u][2], he[u][1], len(hp), len(gp)))
        name_var += len(gp - hp)

    empty = [u for u in set(he) - set(ge)
             if not H.execute("SELECT COUNT(*) FROM games WHERE event_id=?",
                              (he[u][0],)).fetchone()[0]]
    print(f"shared events                 {len(shared)}")
    print(f"  played games (mirror)       {played}")
    print(f"  events differing on those   {len(bad_scores)}")
    print(f"  roster lines                {roster_lines}")
    print(f"  events differing on those   {len(bad_counts)}")
    print(f"  roster name variants        {name_var} "
          f"({100 * name_var / max(roster_lines, 1):.2f}%, surname updates)")
    print(f"  unplayed fixtures  html={unplayed_h}  mirror={unplayed_g}")
    print(f"  played but unattributed (lost)  html={orph_h}  mirror={orph_g}")
    print(f"mirror-only events            {len(set(ge) - set(he))}")
    print(f"html-only events              {len(set(he) - set(ge))} "
          f"({len(empty)} holding no games at all)")
    for row in bad_scores + bad_counts:
        print("  DIFFERS:", *row)
    H.close()
    G.close()
    return not bad_scores and not bad_counts


def main(seasons: list[int], division: str = "college-women-d3",
         workers: int = WORKERS):
    if division not in API_DIVISION:
        raise SystemExit(f"unknown division {division!r}; "
                         f"choose from {', '.join(sorted(API_DIVISION))}")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = connect(DB_PATH)
    con.executescript(SCHEMA)
    _ensure_columns(con)
    print(f"db {DB_PATH}", flush=True)

    for season in seasons:
        events = list_events(division, season)
        print(f"\n{season} {division}: {len(events)} events", flush=True)
        # Fetch in parallel (the network is the cost), write serially: SQLite
        # takes one writer and the ingest is a delete-then-insert per event.
        with ThreadPoolExecutor(workers) as ex:
            jobs = {ex.submit(fetch_event, ev["id"], division): ev for ev in events}
            done = 0
            for fut in list(jobs):
                ev = jobs[fut]
                try:
                    data = fut.result()
                except Exception as e:
                    print(f"  ! {ev['name'][:50]}: {type(e).__name__}: {e}", flush=True)
                    continue
                if data is None:
                    continue
                event_id = upsert_event(con, season, ev, division)
                summary = ingest_event(con, event_id, data)
                con.commit()
                done += 1
                print(f"  [{done}/{len(events)}] {ev['startDate']} "
                      f"{ev['name'][:44]:46} {summary}", flush=True)
    con.close()


if __name__ == "__main__":
    argv = sys.argv[1:]
    div = "college-women-d3"
    if "--division" in argv:
        i = argv.index("--division")
        div = argv[i + 1]
        del argv[i:i + 2]
    if argv[:1] == ["--validate"]:
        if len(argv) != 3:
            raise SystemExit("usage: --validate HTML_DB GQL_DB [--division D]")
        raise SystemExit(0 if validate(argv[1], argv[2], div) else 1)
    if not argv:
        raise SystemExit(
            "usage: python -m scraper.graphql SEASON [SEASON ...] "
            "[--division college-women-d3]\n"
            "       python -m scraper.graphql --validate HTML_DB GQL_DB "
            "[--division college-women-d3]\n"
            f"divisions: {', '.join(sorted(API_DIVISION))}\n"
            f"writes to $USAU_GQL_DB (default {DB_PATH})")
    main([int(a) for a in argv], div)
