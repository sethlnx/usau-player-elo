"""Backfill published bracket structure onto existing games from the mirror.

USAU publishes no tournament shape. `games.stage` is the only hint and it is
free text: organisers routinely label every bracket's decider "Finals", so
Texas 2 Finger 2024 holds SIX games reading exactly that — one per bracket —
and nothing on the page says which is the championship. Recovering the shape
from those labels is what analysis/tournaments.py has to do, and on events like
that it crowns the wrong club: it picked Clutch, who won the 9TH-PLACE bracket,
over Alamode, who won the event.

The GraphQL mirror publishes the shape outright — each bracket's name, its
`type`, and `placeStart` (1 for the championship) — so this attaches it to the
games already in the DB rather than re-deriving anything.

WHY THE JOIN IS ON TEAMS AND SCORES
-----------------------------------
`Game.domId` is USAU's own element id for a bracket fixture ("game412450") and
matches `games.slot` exactly, which looks like the obvious key. It is unusable
for the corpus: `slot` was added to the schema long after most of it was
scraped, so it is NULL for every club-men game before 2026 and can only ever be
filled by re-scraping behind the WAF.

So a game is matched on what both sources independently record — the unordered
pair of team names and the unordered pair of scores, within one event. Measured
on Texas 2 Finger 2024: all 70 of our played games matched, with ZERO ambiguous
keys. Ambiguity is still possible in principle (one event, one pair, twice, on
the same score), and any key that is not unique on BOTH sides is skipped rather
than guessed at.

Team names are normalized to alphanumerics with a trailing "(Nickname)" removed,
because the mirror prints "Carleton College-Eclipse (Eclipse)" where the HTML
scrape stores "Carleton College-Eclipse".

WHAT IS STORED
--------------
`bracket_round` is computed here, not stored raw: the mirror gives `nextGameId`,
which points FORWARD to the game a winner advances into, so a bracket's final is
the game that points nowhere. Counting back up the chain gives wins-from-final,
and 0 IS the final. That is what lets a bracket be drawn and a champion named
without reading a single label.

Idempotent: a re-run clears an event's four columns before writing them, so a
bracket corrected upstream does not leave the old one behind.

Usage: python -m scraper.structure [--division D] [SEASON ...]
       python -m scraper.structure --audit    # champions the labels got wrong
"""

import collections
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from .build_db import DB_PATH, _ensure_columns, connect
from .graphql import API_DIVISION, WORKERS, list_events, post

EVENT_STRUCT = """
query($id:ID!){
  event(id:$id){
    divisions{
      division level
      brackets{
        name type size placeStart
        games(first:100){ edges{ node{
          gameId nextGameId roundName
          team1{ name } team1Score team2{ name } team2Score status } } }
      }
    }
  }
}
"""


def norm_team(name: str | None) -> str:
    """Alphanumerics only, trailing parenthetical dropped.

    "Carleton College-Eclipse (Eclipse)" and "Carleton College-Eclipse" have to
    land on the same key; so do "Rhino Slam!" and "Rhino Slam".
    """
    n = re.sub(r"\s*\([^)]*\)\s*$", "", str(name or "").strip())
    return re.sub(r"[^a-z0-9]+", "", n.lower())


def game_key(a: str | None, b: str | None, sa, sb):
    """The join key: unordered teams, unordered scores. None when unusable."""
    ta, tb = norm_team(a), norm_team(b)
    if not ta or not tb:
        return None
    try:
        x, y = int(sa), int(sb)
    except (TypeError, ValueError):
        return None
    return (frozenset((ta, tb)), tuple(sorted((x, y))))


def _trees(games: list[dict]) -> dict[str, tuple[str, int]]:
    """gameId -> (root gameId, wins from that root).

    A published bracket is NOT always one tree. The 2026 Lehigh men's draw
    files 24 games under a single "Championship" heading at placeStart 1, and
    they are four independent knockouts: the real championship, a 9-16
    "Ninals", a 17-24 "Seventeenals", and a four-team flight. Grouping by the
    published NAME would call all of that one bracket and lose three of them;
    grouping by where nextGameId leads recovers each.

    nextGameId points FORWARD, at the game a winner advances into, so a root is
    a game pointing nowhere and the hop count to it is wins-from-final.
    """
    nxt = {g["gameId"]: g.get("nextGameId") for g in games}
    out = {}
    for gid in nxt:
        cur, hops, seen = gid, 0, set()
        while cur in nxt and nxt[cur] in nxt and cur not in seen:
            seen.add(cur)
            cur, hops = nxt[cur], hops + 1
        out[gid] = (cur, hops)
    return out


def fetch_structure(event_api_id: str, division: str) -> list[dict] | None:
    """Bracket rows for one event-division, each carrying its own join key.

    Returns None when the mirror has no page for this division, [] when it
    publishes no brackets.

    `bracket` identifies the TREE, not the published heading — see `_trees`.
    Where a heading holds more than one, the root's own round name is appended
    so each tree is a bracket of its own; analysis/tournaments.py reads the
    placement back off that label, since the coarse placeStart gets it wrong.
    """
    want_level, want_div = API_DIVISION[division]
    ev = post(EVENT_STRUCT, {"id": event_api_id})["event"]
    mine = next((d for d in ((ev or {}).get("divisions") or [])
                 if d.get("division") == want_div
                 and d.get("level") == want_level), None)
    if mine is None:
        return None

    out = []
    for b in (mine.get("brackets") or []):
        games = [e["node"] for e in b["games"]["edges"]]
        # An UNWIRED bracket is worse than no bracket. Where the mirror
        # publishes a heading but no nextGameId anywhere in it, every game is
        # its own root and splitting by tree turns one bracket into N
        # single-game ones — Flat Tail 2017 became six "championships" and the
        # title went to whichever sorted first, losing Oregon, who won the game
        # the schedule calls "Champ". Nothing is attached in that case, so the
        # label path handles the bracket exactly as it did before.
        if len(games) > 1 and not any(g.get("nextGameId") for g in games):
            continue
        tree = _trees(games)
        roots = {r for r, _ in tree.values()}
        byid = {g["gameId"]: g for g in games}
        for g in games:
            k = game_key((g.get("team1") or {}).get("name"),
                         (g.get("team2") or {}).get("name"),
                         g.get("team1Score"), g.get("team2Score"))
            if k is None:
                continue
            root, hops = tree[g["gameId"]]
            label = (byid.get(root, {}).get("roundName") or "").strip()
            name = b.get("name")
            place = b.get("placeStart")
            if len(roots) > 1:
                # The key must be unique per TREE, and the label alone is not:
                # Lehigh's men's draw has two trees whose roots are both called
                # "Finals", which collided into one bracket holding two finals
                # and was rejected wholesale. The root id disambiguates. Only
                # the grouping uses this string; the page shows the placement.
                name = f"{name} / {root[:8]}"
                # The heading's placeStart is wrong for every tree under it but
                # one. A root labelled with a position range says what it really
                # decided — "Ninals (Knockout 9-10)" is 9th, "Seventeenals
                # (Knockout 17-18)" is 17th — so the first number in the label
                # wins. A root with no number ("Finals") keeps the heading's
                # place, which is what the title tree wants.
                num = re.search(r"\d+", label)
                if num:
                    place = int(num.group(0))
            out.append({
                "key": k,
                "bracket": name,
                "bracket_place": place,
                "bracket_type": b.get("type"),
                "bracket_round": hops,
            })
    return out


def apply_structure(con, event_id: int, rows: list[dict]) -> tuple[int, int]:
    """Write bracket columns for one event. Returns (attached, skipped)."""
    con.execute("""UPDATE games SET bracket=NULL, bracket_place=NULL,
                   bracket_type=NULL, bracket_round=NULL WHERE event_id=?""",
                (event_id,))
    mine = collections.defaultdict(list)
    for gk, h, a, hs, as_ in con.execute("""
            SELECT g.game_key, h.display_name, a.display_name,
                   g.home_score, g.away_score
            FROM games g
            LEFT JOIN event_teams h ON h.event_team_id = g.home_id
            LEFT JOIN event_teams a ON a.event_team_id = g.away_id
            WHERE g.event_id = ?""", (event_id,)):
        k = game_key(h, a, hs, as_)
        if k is not None:
            mine[k].append(gk)

    theirs = collections.Counter(r["key"] for r in rows)
    attached = skipped = 0
    for r in rows:
        k = r["key"]
        # Unique on BOTH sides or not at all: two fixtures between the same two
        # clubs ending on the same score cannot be told apart, and picking one
        # would file half an event's bracket under the wrong heading.
        if theirs[k] != 1 or len(mine.get(k, ())) != 1:
            skipped += 1
            continue
        con.execute(
            """UPDATE games SET bracket=?, bracket_place=?, bracket_type=?,
                                bracket_round=? WHERE event_id=? AND game_key=?""",
            (r["bracket"], r["bracket_place"], r["bracket_type"],
             r["bracket_round"], event_id, mine[k][0]))
        attached += 1
    return attached, skipped


def main(seasons: list[int] | None, division: str | None = None,
         workers: int = WORKERS):
    divisions = [division] if division else sorted(API_DIVISION)
    con = connect(DB_PATH)
    _ensure_columns(con)
    con.commit()

    for div in divisions:
        # Only events this DB holds can be updated, and they are keyed on
        # (url, division), so the mirror's list is filtered down to those urls.
        have = {u.rstrip("/"): (eid, nm) for eid, u, nm in con.execute(
            "SELECT event_id, url, name FROM events WHERE division=?", (div,))}
        if not have:
            print(f"{div}: not in this DB, skipping", flush=True)
            continue
        years = seasons or sorted({s for (s,) in con.execute(
            "SELECT DISTINCT season FROM events WHERE division=?", (div,))})
        todo = []
        for season in years:
            for ev in list_events(div, season):
                key = (ev.get("url") or "").rstrip("/")
                if key in have:
                    todo.append((have[key][0], ev["id"], have[key][1]))

        events = attached = skipped = brackets = 0
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(fetch_structure, api, div): (eid, nm)
                    for eid, api, nm in todo}
            for fut in list(futs):
                eid, nm = futs[fut]
                try:
                    rows = fut.result()
                except Exception as e:
                    print(f"  ! {nm[:44]}: {type(e).__name__}: {e}", flush=True)
                    continue
                if not rows:
                    continue
                a, s = apply_structure(con, eid, rows)
                con.commit()
                if a:
                    events += 1
                    brackets += len({r["bracket"] for r in rows})
                attached += a
                skipped += s
        print(f"{div}: {events} events, {attached} games attached, "
              f"{brackets} brackets, {skipped} unmatched", flush=True)
    con.close()


def audit(con):
    """Events whose champion CHANGES once the published structure is used.

    Both answers come from `decompose`, run twice on the same fixtures: once
    with the published columns stripped, which is exactly what the code did
    before this module existed, and once as it stands. Stripping matters —
    decompose now PREFERS the published shape, so comparing it against itself
    would report agreement by construction and measure nothing.
    """
    from analysis.tournaments import decompose, load, winner
    by_event = load(con)
    meta = {eid: (nm, dv, sd) for eid, nm, dv, sd in con.execute(
        "SELECT event_id, name, division, start_date FROM events")}

    def crown(games):
        _, brackets, _ = decompose(games)
        for kind, root_rank, rounds in brackets:
            if kind == "champ" and root_rank == 0 and len(rounds[-1]) == 1 \
                    and rounds[-1][0]:
                return winner(rounds[-1][0])
        return None

    out = []
    for eid, games in by_event.items():
        if not any(g.get("br") for g in games):
            continue                       # nothing published, nothing to move
        bare = [{**g, "br": None, "place": None, "bround": None} for g in games]
        before, after = crown(bare), crown(games)
        if before is None and after is None:
            continue
        if before is None or after is None or \
                norm_team(before) != norm_team(after):
            nm, dv, sd = meta[eid]
            out.append((sd, nm, dv, before, after))
    return out


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--audit" in argv:
        c = connect(DB_PATH)
        rows = audit(c)
        print(f"events where the label-recovered champion is wrong: {len(rows)}")
        for sd, nm, dv, before, after in sorted(rows, key=lambda r: (r[0] or "", r[1])):
            print(f"  {sd} {nm[:38]:40} {dv:17} {before} -> {after}")
        c.close()
        raise SystemExit(0)
    div = None
    if "--division" in argv:
        i = argv.index("--division")
        div = argv[i + 1]
        del argv[i:i + 2]
    main([int(a) for a in argv] or None, div)
