"""Recover the shape of every tournament in the corpus: pools, brackets, and
the series each instance belongs to.

USAU publishes a flat list of fixtures per event. It does NOT publish the
structure over them — which games formed a pool, which bracket a game sat in,
or that this year's Sectional is the same tournament as last year's. The
`stage` column is free text typed by thousands of different organisers and runs
to 4,139 distinct values, including "Chumpionship 9", "GAME TO GO TO THE GAME
TO GO" and "Round Name". So the structure is recovered rather than read:

    pools     cliques in the co-play graph      (labels unusable: of the 2,546
                                                 events where two or more pools
                                                 were recovered, 1,447 file
                                                 them under one heading)
    brackets  round RANK from the label, feeder wiring from the results
    series    the printed name with everything that varies between instances
              taken out

The split is deliberate. Pools are found structurally because the labels carry
no information; brackets are NOT, because a win-chain through mislabelled pool
play looks exactly like a nine-round bracket and inventing one is worse than
showing none. A game that cannot be placed stays loose and is displayed under
whatever the organiser called it.

Entry point: build(con) -> the `tourneys` payload embedded by analysis.site.
"""

import collections
import itertools
import re
from datetime import date

from analysis.rankings import DIVCODE

# Wins from a bracket's own final. Doubles as the display column index.
ROUND_NAMES = ["Final", "Semifinals", "Quarterfinals", "Prequarterfinals",
               "Round of 32", "Round of 64", "Round of 128"]

# Tried OUTERMOST ROUND FIRST: "Semi Finals" contains "Finals" and
# "Pre-Quarters" contains "Quarters", so the more specific match has to win.
RANK_RE = [(k, re.compile(p, re.I)) for k, p in [
    (0, r"\b(finals?|championships?|champs?|title|ship)\b"),
    (1, r"\b(semi[\s\-]?finals?|semis?|final\s*(four|4)|s\.?f\.?s?)\b"),
    (2, r"\b(quarter[\s\-]?finals?|quarters?|qtrs?|q\.?f\.?s?|elite\s*8)\b"),
    (3, r"\b(pre[\s\-]?quarter[\s\-]?finals?|pre[\s\-]?quarters?|pre[\s\-]?q\w*"
        r"|round\s*of\s*16|sweet\s*(16|sixteen)|octos?)\b"),
    (4, r"\b(round\s*of\s*32|r32)\b"),
    (5, r"\b(round\s*of\s*64|r64)\b"),
]][::-1]

ORD_WORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "sixth": 6, "seventh": 7, "eighth": 8, "eight": 8, "ninth": 9,
            "nineth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
            "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
            "sixteenth": 16, "seventeenth": 17, "nineteenth": 19,
            "twentieth": 20}

# Anything naming a finishing position other than first, or a losers' route.
# What it matches becomes the bracket's key, so "9th Place Quarters" and "9th
# Place Semis" land in the same bracket.
PLACEMENT_RE = re.compile(
    r"(\b(?!1st|first)\d*(?:2nd|3rd|[04-9]th|1[0-9]th|\d+(?:st|nd|rd|th))\b"
    r"|\b(?:" + "|".join(k for k, v in ORD_WORD.items() if v != 1) + r")\b"
    r"|\bt-\s?\d+|\bknockout\b|\bconsolation\b|\bconsols?\b|\bback\s?door\b"
    r"|\bbackdoor\b|\bbottom\b|\blower\b|\bchump\w*|\bchimp\w*|\bdump\w*"
    r"|\bsilver\b|\bbronze\b|\bplacement\b|\bloser\w*|\bplay[\s\-]?outs?\b"
    r"|\bninals?\b|\bnine-?als?\b|\bfivals?\b|\bfive-?nals?\b|\bwampionship\b"
    r"|\bcon [abc]\b|\bmini\b|\bswiss\b|\bnit\b|\bb\s+bracket\b"
    r"|\bc\s+bracket\b|\bemerge\b|\bstring\b|\bcheddar\b|\bcheese\b"
    r"|\bapplebee|\bchili)", re.I)

# A game-to-go decides a berth in the next round of the series, not a placing.
GTG_RE = re.compile(r"\bg(?:ame)?\s*[\-\s]?2?\s*to?\s*[\-\s]?go\b|\bg2g\b"
                    r"|\bgtg\b", re.I)

# A bare finishing position IS that bracket's final: "Third Place", "9th Place
# Game", "13th/14th Place". Only when nothing else is going on in the label.
PLACE_ONLY_RE = re.compile(
    r"^[\W_]*(?:t-?\s*)?(?:\d+(?:st|nd|rd|th)|" + "|".join(ORD_WORD) + r")\b"
    r"[\s\-/&]*(?:and|&|/|\d+(?:st|nd|rd|th))?\s*(?:place|placement)?\s*"
    r"(?:game|match)?[\W_]*$", re.I)

POOLISH = re.compile(r"(^|\b)(pool|round robin|rr)\b", re.I)
TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp])?")

# Where an event sits in the championship series. 0 is regular season.
TIER_RE = [
    (4, re.compile(r"\bnational|college championships?\b"
                   r"|usa ultimate club championships?\b|u\.?s\.? open"
                   r"|world .*championships?|\bwucc\b|pan american", re.I)),
    (3, re.compile(r"\bregionals?\b", re.I)),
    (2, re.compile(r"\bsectionals?\b", re.I)),
    (1, re.compile(r"\bconference\b|\bcc\b|\bconf\b", re.I)),
]
TIER_NAMES = ["Regular season", "Conference", "Sectionals", "Regionals",
              "Nationals & majors"]

# Everything that varies between instances of one tournament.
NOISE_RE = [re.compile(p, re.I) for p in [
    r"\b(19|20)\d{2}\b",
    r"\b(men'?s?|women'?s?|mixed|coed|co-ed)\b",
    r"\b(d-?\s?i{1,3}|d-?\s?1|d-?\s?3|division\s+\d+|di{1,3})\b",
    r"\bcollege\b", r"\bclub\b", r"\bultimate\b", r"\busau?\b", r"\btct\b",
    r"\b(part|pt\.?)\s*\d+\b", r"#\s*\d+\b",
    r"\s[-\u2013\u2014]\s.*$",          # trailing " - ICC", " - Day 2"
    r"\((?:[^)]*)\)",                   # "(ICC)", "(B Teams)"
]]
EDITION_RE = re.compile(
    r"\s+(?:x{0,3}(?:ix|iv|v?i{0,3})|\d{1,3}(?:st|nd|rd|th)?)\s*$", re.I)

_SUF = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else _SUF.get(n % 10, 'th')}"


def series_key(name):
    """Collapse a printed event name to the series it belongs to.

    Instances of one tournament differ by year, by edition number ("Cooler
    Classic 30"), by division wording, and by whatever suffix the organiser
    chose that season ("- ICC", "(ICC)", "International"). All of it comes
    out. Division is a payload field of its own, so gendered wording goes too
    and the men's and women's halves of a Sectional share one history.

    "Open" is deliberately NOT treated as a division word: it is load-bearing
    in "U.S. Open", and stripping it split that series three ways.
    """
    s = " " + name.lower() + " "
    for rx in NOISE_RE:
        s = rx.sub(" ", s)
    s = re.sub(r"[^\w\s]+", " ", s)
    s = re.sub(r"\bchampionships\b", "championship", s)
    s = re.sub(r"\binternational\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    prev = None
    while s != prev:
        prev, s = s, EDITION_RE.sub("", s).strip()
    return s or name.lower().strip()


def tier(name):
    return next((k for k, rx in TIER_RE if rx.search(name)), 0)


def _minutes(t):
    m = TIME_RE.match(t or "")
    if not m:
        return 10 ** 4
    h, mi = int(m.group(1)) % 12, int(m.group(2))
    if (m.group(3) or "").lower() == "p":
        h += 12
    return h * 60 + mi


def _slotnum(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else 10 ** 9


def winner(g):
    """None on a tie: a drawn game advances nobody, so it can be neither a
    bracket root nor a feeder and falls out as a loose result."""
    if g["hs"] == g["as"]:
        return None
    return g["home"] if g["hs"] > g["as"] else g["away"]


def classify(stage):
    """(bracket key, round rank) for a knockout label, else None.

    The key groups a game with the others deciding the same placement --
    'champ' for the title, an ordinal for a placement bracket, 'gtg' for a
    berth. The rank is wins-from-that-bracket's-final.
    """
    s = (stage or "").strip()
    if not s:
        return None
    rank = next((k for k, rx in RANK_RE if rx.search(s)), None)
    if rank is None:
        if PLACE_ONLY_RE.match(s) or GTG_RE.search(s):
            rank = 0
        else:
            return None
    if GTG_RE.search(s):
        return ("gtg", rank)
    m = PLACEMENT_RE.search(s)
    return ((m.group(0).lower() if m else "champ"), rank)


def bracket_key(k):
    """'third', '3rd' and 't-3' all name the same bracket."""
    k = re.sub(r"^t[-\s]\s*", "", k)
    if k in ORD_WORD:
        return _ordinal(ORD_WORD[k])
    m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", k)
    return _ordinal(int(m.group(1))) if m else k


def _maximal_cliques(seed, adj, budget=4000):
    """Every maximal clique containing one edge, by Bron-Kerbosch with
    pivoting over its common neighbourhood. Neighbourhoods here are a handful
    of teams; the budget only guards against a pathological schedule."""
    a, b = seed
    out, left = [], [budget]

    def bk(R, P, X):
        if left[0] <= 0:
            return
        if not P and not X:
            out.append(R)
            return
        left[0] -= 1
        pivot = max(P | X, key=lambda u: len(P & adj[u]))
        for v in list(P - adj[pivot]):
            bk(R | {v}, P & adj[v], X & adj[v])
            P, X = P - {v}, X | {v}

    bk({a, b}, adj[a] & adj[b], set())
    return out or [{a, b}]


def _pool_score(clique, pending):
    """Fewest calendar days wins, size breaks the tie, team names settle it.

    That last term buys nothing but REPRODUCIBILITY, and it is not optional:
    the cliques arrive from a Bron-Kerbosch over sets of team names, so
    without it a tie is broken by string hash order and the recovered shape
    changes between runs of the same build. Five events used to flip their
    champion that way.
    """
    days = {pending[e][0]["date"][:10]
            for e in (frozenset(p) for p in itertools.combinations(sorted(clique), 2))
            if e in pending}
    return (len(days) if days else 9, -len(clique), sorted(clique))


def pools_of(games):
    """Recover round-robin pools as cliques in the co-play graph.

    Two rules keep this from over-reaching:

    * Cliques consume EDGES, not teams. A placement round robin reuses teams
      from the opening pools, so retiring a team would hide the second pool it
      plays in. A repeat fixture between the same two teams is that later
      pool's game, never a second win in the first pool's standings.

    * The seed is the earliest unplaced fixture, and among the maximal cliques
      through it the one spanning the fewest CALENDAR DAYS wins. Structure
      alone cannot tell the U.S. Open's opening pool of three from the 9-12
      pool of four that reuses two of its teams -- both are cliques, and the
      wrong one is bigger. Days separate them; kickoff times are missing or
      unparseable often enough that nothing finer is safe.

    An earlier version grew cliques greedily in fixture order instead. That is
    hostage to an ordering USAU does not reliably supply: a 25-team college
    invite came apart into fragments that left 32 of its pool games homeless.

    Returns ([(teams, games, is_later)], leftovers), leftovers being
    crossovers, play-ins and anything else that is not round robin play.
    """
    seen = {}
    for g in games:
        seen.setdefault(frozenset((g["home"], g["away"])), []).append(g)
    for gs in seen.values():
        gs.sort(key=lambda g: g["ord"])
    pending = {e: list(gs) for e, gs in seen.items()}
    adj = collections.defaultdict(set)
    for e in pending:
        a, b = tuple(e)
        adj[a].add(b)
        adj[b].add(a)

    found, left = [], []
    while pending:
        seed = min(pending, key=lambda e: pending[e][0]["ord"])
        cliques = [c for c in _maximal_cliques(tuple(seed), adj) if len(c) >= 3]
        if not cliques:
            left.append(pending[seed].pop(0))
            if not pending[seed]:
                a, b = tuple(seed)
                adj[a].discard(b)
                adj[b].discard(a)
                del pending[seed]
            continue
        teams = sorted(min(cliques, key=lambda c: _pool_score(c, pending)))
        gs = []
        for x, y in itertools.combinations(teams, 2):
            e = frozenset((x, y))
            gs.append(pending[e].pop(0))
            if not pending[e]:
                del pending[e]
                adj[x].discard(y)
                adj[y].discard(x)
        found.append((teams, sorted(gs, key=lambda g: g["ord"])))

    found.sort(key=lambda p: p[1][0]["ord"])
    placed, out = set(), []
    for teams, gs in found:
        out.append((teams, gs, bool(placed & set(teams))))
        placed |= set(teams)
    return out, sorted(left, key=lambda g: g["ord"])


def bracket_order(e):
    """Display order for a (kind, root_rank, rounds) bracket.

    Title first, then placement brackets by the position they decide, then
    anything whose key is not an ordinal. Bigger brackets first within a tie,
    so a full eight-team tree outranks a one-game playoff for the same place.

    Module level because `decompose` now merges two sources — the published
    brackets and the label-recovered ones — and both have to land in one order.
    """
    size = sum(1 for rd in e[2] for m in rd if m)
    if e[0] == "champ":
        return (0, 0, -size)
    m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", e[0])
    return (1, int(m.group(1)), -size) if m else (2, 0, -size)


def brackets_of(games):
    """Group labelled knockout games into brackets and wire each into a padded
    binary tree.

    Rank comes from the label, so no bracket is invented where the organiser
    named none; only the wiring is inferred, a slot's feeder being the game at
    the next rank out that this team won. Every rank-0 game is a root, so an
    event running two flights off one schedule -- college invites routinely do
    -- yields two trees rather than one tree and a pile of orphans.

    Returns ([(key, rounds)], leftovers). Rounds run outermost first; each
    slot holds a game or None (a bye, or an entrant who came straight from
    pool play).
    """
    by_kind = collections.defaultdict(lambda: collections.defaultdict(list))
    loose = []
    for g in games:
        c = classify(g["stage"])
        if c is None or winner(g) is None:
            loose.append(g)
        else:
            by_kind[bracket_key(c[0])][c[1]].append(g)

    out = []
    for kind, ranks in by_kind.items():
        used = set()

        def won(team, rank):
            for g in ranks.get(rank, ()):
                if id(g) not in used and winner(g) == team:
                    return g
            return None

        # Normally each rank-0 game is its own root, which is what lets an
        # event running two flights off one schedule come out as two trees.
        # When a bracket has no final at all, though, it is not absent — USAU
        # Regionals routinely stop at semifinals, both winners having already
        # qualified — so it is rooted on every game of the innermost rank it
        # does have and simply displayed without a Final column. Rooting only
        # on rank 0 threw those away: 6 bracket games at a Regional went to
        # the loose pile because nobody played a title game.
        root_rank = 0 if ranks.get(0) else min(ranks)
        roots = sorted(ranks[root_rank], key=lambda g: g["ord"])
        for seeds in ([[r] for r in roots] if root_rank == 0 else [roots]):
            rounds, frontier = [], list(seeds)
            for g in seeds:
                used.add(id(g))
            while frontier:
                nxt = []
                for m in frontier:
                    for t in (m["home"], m["away"]) if m else (None, None):
                        f = won(t, root_rank + len(rounds) + 1) if m else None
                        if f is not None:
                            used.add(id(f))
                        nxt.append(f)
                rounds.append(frontier)
                frontier = nxt if any(nxt) else []
            out.append((kind, root_rank, list(reversed(rounds))))
        loose += [g for gs in ranks.values() for g in gs if id(g) not in used]

    out.sort(key=bracket_order)
    return out, sorted(loose, key=lambda g: g["ord"])


def published_brackets(games):
    """(brackets, leftover) from the shape the organiser actually published.

    Same tuple shape `brackets_of` returns — (kind, root_rank, rounds), final
    last — so nothing downstream has to know which source it came from.

    This exists because the label cannot carry what `bracket_place` does. At
    Texas 2 Finger 2024 six games are labelled "Finals", one per bracket, and
    label recovery crowned the ninth-place winner. `place` says outright which
    bracket decides first place, so `kind` is read off it rather than guessed:
    1 is the title, anything else is that placement.

    WIRING is the gate, not `place`. Exactly one game may sit at round 0:
    `bracket_round` comes off nextGameId, so a bracket the mirror publishes with
    no wiring has every game reading as a root — Tally Classic XII's
    championship arrives as seven simultaneous "finals" — and trusting that
    loses the champion outright where label recovery still infers feeders from
    who won. One root is the test for wiring being real. A bracket failing it is
    handed back untouched and takes the label path exactly as before.

    A MISSING place is not disqualifying. PLU Mens+BBQ 2026 publishes its
    championship as "Sunday Bracket" with no placeStart at all, and requiring
    one threw the whole thing out: 37 of its 44 played games went loose. Where
    the place is absent the placement is read off the bracket's own name or its
    root label, and a bracket that names no position is the title bracket.
    """
    have = [g for g in games if g.get("br") and g.get("bround") is not None]
    if not have:
        return [], games
    per_bracket = collections.defaultdict(list)
    for g in have:
        per_bracket[g["br"]].append(g)

    out, used = [], set()
    for name, gs in per_bracket.items():
        place = next((g["place"] for g in gs if g["place"] is not None), None)
        by_round = collections.defaultdict(list)
        for g in gs:
            by_round[g["bround"]].append(g)
        if len(by_round.get(0, ())) != 1:
            continue
        # bround counts wins from the final, so descending order puts the
        # earliest round first and the final last.
        rounds = [sorted(by_round[r], key=lambda g: g["ord"])
                  for r in sorted(by_round, reverse=True)]
        if place:
            # `place` is per TREE, not per published heading:
            # scraper/structure.py splits a heading holding several knockouts
            # and reads each one's real position off its own root label.
            kind = "champ" if place == 1 else _ordinal(place)
        else:
            # The ROOT's label, never the bracket's name. A name says what KIND
            # of bracket it is; the root says what POSITION it decided, and
            # `classify` already reads that. Reading the name instead filed
            # Riverside Classic 2026's title bracket -- published as "Knockout
            # Bracket" with no placeStart -- under 'knockout', because
            # PLACEMENT_RE carries \bknockout\b to catch "13th Place (Knockout
            # 13-16)". Nothing then matched 'champ' and the event showed no
            # winner at all, though its root reads "Finals: Texas BBQ 10-9
            # Riverside". Its "Consolation Bracket" went the same way over a
            # root saying "5th Place".
            c = classify(by_round[0][0]["stage"] or "")
            kind = bracket_key(c[0]) if c and c[0] != "champ" else "champ"
        out.append((kind, min(by_round), rounds))
        used.update(id(g) for g in gs)
    return out, [g for g in games if id(g) not in used]


def decompose(games):
    """(pools, brackets, loose) for one event's fixture list."""
    playable = [g for g in games if g["home"] and g["away"] and g["done"]]
    # A fixture whose two sides carry the same display name (USAU occasionally
    # files a club's A and B squads identically) is not an edge and cannot be
    # reasoned about; it goes straight to the loose pile.
    odd = [g for g in playable if g["home"] == g["away"]]
    playable = [g for g in playable if g["home"] != g["away"]]
    # The PUBLISHED bracket comes first and is never second-guessed. Whatever
    # it does not cover carries on through the label path below, so an event
    # with structure for its championship and nothing else still gets its pools
    # recovered the old way.
    pub, playable = published_brackets(playable)
    poolish = [g for g in playable if POOLISH.search(g["stage"])]
    rest = [g for g in playable if not POOLISH.search(g["stage"])]
    if poolish:
        pools, left = pools_of(poolish)
        rest = rest + left
    else:
        # Nothing says "pool", so the pools have to be recovered structurally
        # from the whole schedule — 1,447 of the 2,546 multi-pool events file
        # every fixture under one heading. What that must NOT do is eat a game
        # the organiser already placed: a clique through the Final's edge is
        # indistinguishable from a round robin on structure alone, and
        # swallowing it loses the bracket. Centex 2023 lost Colorado's
        # universe-point title that way, and the Northwest mixed Regional lost
        # BFG's. Labels decide brackets everywhere else in this module; they
        # decide here too.
        named = [g for g in rest if classify(g["stage"])]
        pools, left = pools_of([g for g in rest if not classify(g["stage"])])
        rest = left + named
    brackets, loose = brackets_of(sorted(rest, key=lambda g: g["ord"]))
    # A kind the published shape already claimed cannot be claimed again. The
    # label path only ever sees leftovers, but a stray game reading like a
    # final earns its own 'champ' bracket and the page then shows two sections
    # both headed "Championship bracket" — the 2025 Lehigh men's draw does
    # exactly that. The published one wins and the leftover goes loose, where
    # it is still displayed, just not as a second title bracket.
    claimed = {kind for kind, _, _ in pub}
    keep = [b for b in brackets if b[0] not in claimed]
    loose = loose + [m for b in brackets if b[0] in claimed
                     for rd in b[2] for m in rd if m]
    return (pools, sorted(pub + keep, key=bracket_order),
            sorted(loose + odd, key=lambda g: g["ord"]))


def _event_key(source, event_id):
    """Keep supplemental event IDs distinct from the USAU integer IDs."""
    return event_id if source == "usau" else f"{source}:{event_id}"


def _event_filter(event_ids):
    if event_ids is None:
        return "", []
    ids = tuple(event_ids)
    if not ids:
        return " WHERE 0", []
    return " WHERE g.event_id IN (" + ",".join("?" for _ in ids) + ")", list(ids)


def load(con, source="usau", event_ids=None):
    """Every selected event's fixtures, keyed by source-qualified ID."""
    where, params = _event_filter(event_ids)
    rows = con.execute("""
        SELECT g.event_id, g.game_key, COALESCE(g.stage_pub, g.stage),
               g.date, g.time, g.slot,
               h.display_name, a.display_name, g.home_score, g.away_score,
               g.status, g.bracket, g.bracket_place, g.bracket_type,
               g.bracket_round
        FROM games g
        LEFT JOIN event_teams h ON h.event_team_id = g.home_id
        LEFT JOIN event_teams a ON a.event_team_id = g.away_id""" + where,
        params).fetchall()
    by_event = collections.defaultdict(list)
    for (eid, key, stage, d, t, slot, home, away, hs, as_, status,
         br, place, btype, bround) in rows:
        by_event[_event_key(source, eid)].append({
            "stage": (stage or "").strip(), "date": d or "",
            "home": home, "away": away, "hs": hs, "as": as_,
            "done": status in {"Final", "played"} and hs is not None
                    and as_ is not None and (hs or 0) + (as_ or 0) > 0,
            "br": br, "place": place, "btype": btype, "bround": bround,
            "ord": (d or "\uffff", _minutes(t), _slotnum(slot), key),
        })
    for gs in by_event.values():
        gs.sort(key=lambda g: g["ord"])
    return by_event


def _meta(con, source="usau", event_ids=None):
    where = ""
    params = []
    if event_ids is not None:
        ids = tuple(event_ids)
        if not ids:
            return []
        where = " WHERE event_id IN (" + ",".join("?" for _ in ids) + ")"
        params = list(ids)
    rows = con.execute("""
        SELECT event_id, name, season, division, start_date, end_date,
               city, state
        FROM events""" + where + """
        ORDER BY COALESCE(start_date, ''), name""", params)
    return [
        (_event_key(source, eid), name, season, division, start, end,
         city, state, source)
        for eid, name, season, division, start, end, city, state in rows
    ]


def build(con, supplemental=()):
    """Build the USAU tournament payload plus selected supplemental sources.

    `supplemental` contains ``(connection, source, event_ids)`` tuples. The
    existing USAU rows retain their numeric IDs and array positions; only
    supplemental IDs are namespaced at the payload boundary.
    """
    sources = [(con, "usau", None), *supplemental]
    by_event = {}
    meta = []
    counts = {}
    for source_con, source, event_ids in sources:
        by_event.update(load(source_con, source, event_ids))
        meta.extend(_meta(source_con, source, event_ids))
        where = ""
        params = []
        if event_ids is not None:
            ids = tuple(event_ids)
            if not ids:
                continue
            where = " WHERE event_id IN (" + ",".join("?" for _ in ids) + ")"
            params = list(ids)
        for eid, count in source_con.execute(
            "SELECT event_id, COUNT(*) FROM event_teams" + where +
            " GROUP BY 1",
            params,
        ):
            counts[_event_key(source, eid)] = count

    teams, tix = [], {}

    def team(name):
        if name not in tix:
            tix[name] = len(teams)
            teams.append(name)
        return tix[name]

    events, event_sources, detail = [], [], {}
    series = collections.defaultdict(list)

    # An event the source has announced but published nothing for yet — no
    # fixtures, often no team roll — used to be dropped here along with the
    # dead ones. That silently cost the page every UPCOMING tournament: 29 of
    # the 37 future-dated events in the corpus carry nothing but a name, a
    # date and a place, which is exactly what someone asking "what is on next
    # month" wants to see.
    #
    # So emptiness alone no longer disqualifies an event; emptiness in the
    # PAST does. A game-less event whose start date has been and gone is
    # cancelled, or one the source never filled in, and listing several
    # hundred of those would bury the handful that are actually ahead.
    today = date.today().isoformat()
    for eid, name, season, division, start, end, city, state, source in meta:
        games = by_event.get(eid)
        upcoming = (start or "") >= today
        if not games and not upcoming:
            continue
        pools, brs, loose = decompose(games) if games else ([], [], [])
        if games and not (pools or brs or loose) and not upcoming:
            continue

        field = sorted({g[k] for _, gs, _ in pools for g in gs for k in ("home", "away")}
                       | {g[k] for _, _, rounds in brs for rd in rounds for g in rd
                          if g for k in ("home", "away")}
                       | {g[k] for g in loose for k in ("home", "away") if g[k]})
        local = {t: i for i, t in enumerate(field)}
        dates = sorted({g["date"] for g in (games or []) if g["date"]})
        dix = {d: i for i, d in enumerate(dates)}
        enc = lambda g: [local[g["home"]], local[g["away"]], g["hs"], g["as"],
                         dix.get(g["date"], 0)]

        # Only a bracket that actually reached a single final crowns anybody.
        # A Regional that stopped at two semifinals has no champion, and says
        # so rather than promoting one of the semifinal winners. A DRAWN final
        # crowns nobody either — `winner` returns None on a tie, and the
        # published brackets surface ties the label path never reached.
        champ = -1
        for kind, root_rank, rounds in brs:
            if kind == "champ" and root_rank == 0 and len(rounds[-1]) == 1 \
                    and rounds[-1][0]:
                w = winner(rounds[-1][0])
                champ = local[w] if w is not None else -1
                break

        ix = len(events)
        detail[ix] = {
            "t": [team(t) for t in field],
            "d": dates,
            "p": [[int(later), [enc(g) for g in gs]] for _, gs, later in pools],
            "b": [[kind, root_rank,
                   [[enc(g) if g else 0 for g in rd] for rd in rounds]]
                  for kind, root_rank, rounds in brs],
            "o": [[enc(g), g["stage"]] for g in loose],
        }
        # The champion is carried as a GLOBAL team index, not the local one it
        # was found as: the list and the series table print a champion for
        # every row, and resolving through `detail[i].t` would make both of
        # them fault in an event's games just to read one name. That is the
        # coupling that stops `detail` being loaded per season. Costs ~15 KB.
        place = ", ".join(x for x in (city, state) if x)
        events.append([eid, name, season, DIVCODE.get(division, 0),
                       start or "", end or "", place,
                       counts.get(eid, len(field)), tier(name), 0,
                       detail[ix]["t"][champ] if champ >= 0 else -1])
        event_sources.append(source)
        series[series_key(name)].append(ix)

    # Series in a stable order, and each event told which one it is in. A
    # series of one is still a series: the panel says so rather than implying
    # a history that does not exist.
    slist = []
    for key in sorted(series, key=lambda k: (-len(series[k]), k)):
        ixs = sorted(series[key], key=lambda i: (events[i][2], events[i][3]))
        # The label is the spelling the tournament used most often, with the
        # year taken out. Most-common rather than longest: organisers bolt a
        # suffix on for a season or two ("- ICC", "(ICC)") and the longest
        # name is always one of those outliers.
        spellings = collections.Counter(
            re.sub(r"\s*\b(19|20)\d{2}\b\s*", " ", events[i][1]).strip(" -\u2013")
            for i in ixs)
        label = max(spellings, key=lambda s: (spellings[s], -len(s)))
        for i in ixs:
            events[i][9] = len(slist)
        slist.append([label, ixs])

    return {"teams": teams, "series": slist, "events": events,
            "eventSources": event_sources, "detail": detail,
            "rounds": ROUND_NAMES, "tiers": TIER_NAMES}
