"""Slice `data/history.json` into one resident core and three lazy tiers.

The page opens over `file://`, where `fetch()` is blocked and a classic
`<script>` from the same directory is not, so every split here is a script
tag. What decides the split is not size but WHEN a thing is needed:

    core        every entry point reads it              history.js
    players     one player's panel                      p/<pid % BUCKETS>.js
    rosters     one club's panel, all its seasons       r/<bucket>.js
    games       one event row expanded                  g/<season>.js

Two things make the split possible at all, and both are precomputed here
rather than derived in the browser:

**Trends.** `seasonData` in the page used to walk all 39,325 trajectories to
get a per-season median and a per-season top-25 cut. Both are population
statistics, so no subset computes them, and that single function pinned the
whole player corpus in memory. There are only 24 distinct answers (subject x
division x gender-matching), so they are computed once here — 40 KB gzipped
for every combination, against 1.9 MB for the corpus they replace.

**Which (club, event) pairs have games.** The event table marks a row
expandable only where the model scored games, which asked `games[evIdx]` for
every row and would fault every season just to draw the table. `gameSides`
answers it from the core: one sorted club-index list per event.

Bucketing. Players key on `pid % BUCKETS`, which JavaScript reproduces
directly. Clubs cannot — there is no string hash both languages agree on for
free — so a club's bucket rides on its `rostByClub` entry, which the panel
already looks up. Names inside a roster bucket are LOCAL to it: the browser
appends each bucket's pool to the growing global one and rewrites the indices
as it merges, so a name shared by two buckets is simply stored twice. Across
32 buckets that costs 22%, which is cheaper than any scheme for sharing them.
"""

import collections

# 32 buckets puts the worst player fault at 80 KB gzipped and the worst roster
# fault at 93 KB, which is the same order as a tournament season. Fewer buckets
# means fatter faults; more means the per-file gzip window shrinks and the
# TOTAL grows (chunking already costs 26% against one stream), so this is the
# knee rather than a limit.
BUCKETS = 32

# The per-season cut a subject must have made, once, ever, to earn a line on
# Trends. Mirrors TOPN in the page.
TOPN = 25


def _decode(entry, events):
    """(season, division, elo) per rated event, from the delta encoding."""
    deltas, vals = entry[0], entry[1]
    out, i = [], 0
    for k, d in enumerate(deltas):
        i += d
        if i >= len(events):
            continue
        v = vals[k]
        out.append((events[i][2], events[i][3], v[0] if isinstance(v, list) else v))
    return out


def _median(v):
    if not v:
        return 0
    h = len(v) // 2
    return v[h] if len(v) % 2 else (v[h - 1] + v[h]) / 2


def _combo(items, events, seasons, six, labels, gender, div, gen):
    """One Trends answer: the drawn lines, the population median, the count.

    `items` arrives in the order ties are to be broken in — the sort below is
    stable, so two subjects with the same current rating and the same peak
    come out in the order they went in.
    """
    rows = []
    for key, entry in items:
        if gen is not None and gender.get(key) != gen:
            continue
        vals = [None] * len(seasons)
        for season, pdiv, elo in _decode(entry, events):
            # A division selects POINTS, never whole subjects: 301 club keys
            # play in more than one, so any per-subject verdict misfiles them.
            if div is not None and pdiv != div:
                continue
            si = six.get(season)
            if si is not None:
                vals[si] = elo
        peak = max((v for v in vals if v is not None), default=None)
        if peak is None:
            continue
        rows.append((key, vals, peak))

    # Median over the WHOLE population, not the drawn lines: the mode answers
    # "how far above a typical subject", and the top 25 are typical of nothing.
    med = []
    for i in range(len(seasons)):
        med.append(_median(sorted(vals[i] for _, vals, _ in rows
                                  if vals[i] is not None)))
    # The cut is the 25th value compared with >=, so a tie is never broken
    # arbitrarily and a season can contribute 26. A season with fewer than 25
    # active subjects qualifies all of them.
    cut = []
    for i in range(len(seasons)):
        v = sorted((vals[i] for _, vals, _ in rows if vals[i] is not None),
                   reverse=True)
        cut.append(v[min(TOPN, len(v)) - 1] if v else None)
    top = [r for r in rows
           if any(r[1][i] is not None and cut[i] is not None and r[1][i] >= cut[i]
                  for i in range(len(seasons)))]
    # Ordered on the CURRENT season, so the series index — and with it the
    # colour and dash — matches the default legend order. All-time peak only
    # breaks ties among subjects that did not play it.
    last = len(seasons) - 1
    top.sort(key=lambda r: (r[1][last] is None, -(r[1][last] or 0), -r[2]))
    return {"top": [[k, labels(k), vals, peak] for k, vals, peak in top],
            "med": med, "n": len(rows)}


def _trends(history, player_names, genders, seasons, six):
    """Every Trends answer the four controls can ask for.

    Clubs carry no gender-matching group, so the page normalizes gen to 'all'
    on that side and only six club combinations exist rather than eighteen.

    Players are walked in ASCENDING pid, which is what the browser did when
    this ran there: `for (const key in obj)` visits integer-like keys in
    numeric order before anything else, so that — not insertion order — is
    what used to break a tie between two subjects on the same rating and the
    same peak. Club keys are not integer-like and keep insertion order.
    """
    events = history["events"]
    tn = history.get("teamNames", {})
    players = history["players"]
    out = {}
    for kind, items, labels in (
            ("p", [(k, players[k]) for k in sorted(players, key=int)],
             lambda k: player_names.get(k, "Player " + k)),
            ("c", list(history["teams"].items()), lambda k: tn.get(k) or k)):
        for div in ("all", "0", "1", "2", "3", "4"):
            for gen in (("all", "1", "2") if kind == "p" else ("all",)):
                out[f"{kind}|{div}|{gen}"] = _combo(
                    items, events, seasons, six, labels, genders,
                    None if div == "all" else int(div),
                    None if (gen == "all" or kind != "p") else int(gen))
    return out


def _delta(xs):
    out, prev = [], 0
    for x in sorted(xs):
        out.append(x - prev)
        prev = x
    return out


def club_records(history):
    """(club index, event index as str) -> [wins, losses].

    From the same scored games the drill-down expands, so a club's record on
    an event row and the games behind it can never disagree.

    Two fixtures contribute nothing. A DRAW is neither a win nor a loss; the
    model has a handful. And a club against ITSELF is not a result at all —
    USAU occasionally files a club's A and B squads under one name, and 76 of
    them land on a single key at one event — so counting it would credit the
    same club with a win and a loss for one game. decompose() drops these
    fixtures for the same reason.
    """
    out = collections.defaultdict(lambda: [0, 0])
    for ev, rows in history.get("games", {}).items():
        for r in rows:
            if r[0] == r[1] or r[2] == r[3]:
                out[(r[0], ev)], out[(r[1], ev)] = out[(r[0], ev)], out[(r[1], ev)]
                continue
            win, lose = (r[0], r[1]) if r[2] > r[3] else (r[1], r[0])
            out[(win, ev)][0] += 1
            out[(lose, ev)][1] += 1
    return out


def with_records(history):
    """Club trajectories with the event record folded into each point.

    A point is [elo, rosterSize] and becomes [elo, rosterSize, wins, losses],
    which the page reads positionally. Putting it here rather than in a
    parallel map costs 37 KB gzipped and no extra lookup: the panel is already
    walking these points to draw the table.
    """
    rec = club_records(history)
    cix = {k: i for i, k in enumerate(history.get("gameClubs", []))}
    out = {}
    for key, entry in history["teams"].items():
        ci, vals, i = cix.get(key), list(entry[1]), 0
        for k, d in enumerate(entry[0]):
            i += d
            wl = rec.get((ci, str(i))) if ci is not None else None
            if wl:
                vals[k] = list(vals[k]) + wl
        out[key] = [entry[0], vals] + entry[2:]
    return out


def split(history, player_names, genders, event_meta=None):
    """-> (core, players, rosters, games), each tier keyed by its bucket."""
    events = history["events"]
    seasons = sorted({e[2] for e in events})
    six = {s: i for i, s in enumerate(seasons)}
    rosters_in = history.get("rosters", {})
    people, pids = history.get("people", []), history.get("peoplePid", [])
    best = history.get("bestRosters", {})

    # Players key on pid % BUCKETS; the page reproduces that with `+pid % n`.
    players = collections.defaultdict(dict)
    for pid, entry in history["players"].items():
        players[int(pid) % BUCKETS][pid] = entry

    # A club's bucket rides on its rostByClub entry rather than on a string
    # hash both languages have to agree about. Every best-roster club also has
    # played rosters, so the index covers the whole roster tier.
    by_club = collections.defaultdict(list)
    for rk in rosters_in:
        by_club[rk[:rk.rindex("|")]].append(int(rk[rk.rindex("|") + 1:]))
    club_bucket = {ck: i % BUCKETS for i, ck in enumerate(sorted(by_club))}
    rost_by_club = {ck: [club_bucket[ck]] + _delta(evs)
                    for ck, evs in by_club.items()}

    # Each roster bucket owns its slice of the name pool, in globally sorted
    # order so the localized indices stay ascending and delta-encode. Both
    # sources arrive delta-encoded against the global pool and have to be
    # decoded before they can be re-keyed against a local one.
    def absolute(enc):
        out, cur = [], 0
        for d in enc:
            cur += d
            out.append(cur)
        return out

    rost_abs = {rk: (club_bucket[rk[:rk.rindex("|")]], absolute(enc))
                for rk, enc in rosters_in.items()}
    best_abs = {ck: (club_bucket[ck], ev, when, absolute(mem))
                for ck, (ev, when, mem) in best.items()}
    need = collections.defaultdict(set)
    for b, abs_ in rost_abs.values():
        need[b].update(abs_)
    for b, _, _, abs_ in best_abs.values():
        need[b].update(abs_)

    rosters, local = {}, {}
    for b, wanted in need.items():
        order = sorted(wanted)
        local[b] = {g: i for i, g in enumerate(order)}
        rosters[b] = {"r": {}, "b": {},
                      "p": [people[g] for g in order],
                      "i": [pids[g] for g in order]}
    for rk, (b, abs_) in rost_abs.items():
        rosters[b]["r"][rk] = _delta(local[b][g] for g in abs_)
    for ck, (b, ev, when, abs_) in best_abs.items():
        rosters[b]["b"][ck] = [ev, when, _delta(local[b][g] for g in abs_)]

    # Games by season, and the resident index of who is in them: the event
    # table needs to know a row is expandable without faulting the games.
    games, sides = collections.defaultdict(dict), {}
    for ev, rows in history.get("games", {}).items():
        games[events[int(ev)][2]][ev] = rows
        sides[ev] = _delta({r[0] for r in rows} | {r[1] for r in rows})

    core = {k: history[k] for k in (
        "events", "teamKey", "teamNames", "clubNames",
        "gameClubs", "gameStages", "bestSeason") if k in history}
    # Club trajectories carry their event record; the drill-down prints it on
    # every row, and faulting a season of games per row to count wins would
    # undo the whole point of the split.
    core["teams"] = with_records(history)
    core["gameSides"] = sides
    core["rostByClub"] = rost_by_club
    core["trends"] = _trends(history, player_names, genders, seasons, six)
    # eventIdx -> [strength score, letter, champion club key or null]. Both
    # facts are per EVENT, so they ride here rather than being repeated on
    # every club that attended.
    core["eventMeta"] = event_meta or {}
    return core, dict(players), rosters, dict(games)

