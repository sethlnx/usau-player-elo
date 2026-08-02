"""Recover the shape of every tournament in the corpus: pools, brackets, and
the series each instance belongs to.

USAU publishes a flat list of fixtures per event. It does NOT publish the
structure over them — which games formed a pool, which bracket a game sat in,
or that this year's Sectional is the same tournament as last year's. The
`stage` column is free text typed by ~3,000 different organisers and runs to
3,300 distinct values, including "Chumpionship 9", "GAME TO GO TO THE GAME TO
GO" and "Round Name". So the structure is recovered rather than read:

    pools     cliques in the co-play graph      (labels unusable: 2,103 of the
                                                 2,680 events with pool play
                                                 file every fixture under one
                                                 heading)
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
                   r"|world .*championships?|pan american", re.I)),
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
    r"\bcollege\b", r"\bclub\b", r"\bultimate\b", r"\busau?\b",
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
    """Fewest calendar days wins, size breaks the tie."""
    days = {pending[e][0]["date"][:10]
            for e in (frozenset(p) for p in itertools.combinations(sorted(clique), 2))
            if e in pending}
    return (len(days) if days else 9, -len(clique))


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

    size = lambda e: sum(1 for rd in e[2] for m in rd if m)

    def order(e):
        m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", e[0])
        if e[0] == "champ":
            return (0, 0, -size(e))
        return (1, int(m.group(1)), -size(e)) if m else (2, 0, -size(e))

    out.sort(key=order)
    return out, sorted(loose, key=lambda g: g["ord"])


def decompose(games):
    """(pools, brackets, loose) for one event's fixture list."""
    playable = [g for g in games if g["home"] and g["away"] and g["done"]]
    # A fixture whose two sides carry the same display name (USAU occasionally
    # files a club's A and B squads identically) is not an edge and cannot be
    # reasoned about; it goes straight to the loose pile.
    odd = [g for g in playable if g["home"] == g["away"]]
    playable = [g for g in playable if g["home"] != g["away"]]
    poolish = [g for g in playable if POOLISH.search(g["stage"])]
    rest = [g for g in playable if not POOLISH.search(g["stage"])]
    if poolish:
        pools, left = pools_of(poolish)
        rest = rest + left
    else:
        pools, rest = pools_of(rest)
    brackets, loose = brackets_of(sorted(rest, key=lambda g: g["ord"]))
    return pools, brackets, sorted(loose + odd, key=lambda g: g["ord"])


def load(con):
    """Every event's fixtures, in playing order, keyed by event id."""
    rows = con.execute("""
        SELECT g.event_id, g.game_key, g.stage, g.date, g.time, g.slot,
               h.display_name, a.display_name, g.home_score, g.away_score,
               g.status
        FROM games g
        LEFT JOIN event_teams h ON h.event_team_id = g.home_id
        LEFT JOIN event_teams a ON a.event_team_id = g.away_id""").fetchall()
    by_event = collections.defaultdict(list)
    for eid, key, stage, d, t, slot, home, away, hs, as_, status in rows:
        by_event[eid].append({
            "stage": (stage or "").strip(), "date": d or "",
            "home": home, "away": away, "hs": hs, "as": as_,
            "done": status == "Final" and hs is not None and as_ is not None
                    and (hs or 0) + (as_ or 0) > 0,
            "ord": (d or "\uffff", _minutes(t), _slotnum(slot), key),
        })
    for gs in by_event.values():
        gs.sort(key=lambda g: g["ord"])
    return by_event


def build(con):
    """The `tourneys` payload.

    Encoding is index-heavy because it carries 90,000 games into a file that
    has to open over file://. Team names live in one global pool; a game
    references its event's own field by a local index, which keeps almost
    every number under two digits.

        teams    [display name, ...]
        series   [[label, [event index, ...]], ...]
        events   [[id, name, season, div, start, end, place, nTeams, tier,
                   series index, champion local index or -1], ...]
        detail   {event index: {t, d, p, b, o}}
                   t  field, as global team indices
                   d  distinct dates
                   p  pools:    [[isLater, [game, ...]], ...]
                   b  brackets: [[key, [[game|0, ...], ...]], ...]
                   o  loose:    [[game, stage], ...]
        game     [homeLocal, awayLocal, homeScore, awayScore, dateIndex]
    """
    by_event = load(con)
    meta = list(con.execute("""
        SELECT event_id, name, season, division, start_date, end_date,
               city, state
        FROM events ORDER BY COALESCE(start_date, ''), name"""))
    counts = dict(con.execute(
        "SELECT event_id, COUNT(*) FROM event_teams GROUP BY 1"))

    teams, tix = [], {}

    def team(name):
        if name not in tix:
            tix[name] = len(teams)
            teams.append(name)
        return tix[name]

    events, detail, series = [], {}, collections.defaultdict(list)
    for eid, name, season, division, start, end, city, state in meta:
        games = by_event.get(eid)
        if not games:
            continue
        pools, brs, loose = decompose(games)
        if not (pools or brs or loose):
            continue

        field = sorted({g[k] for _, gs, _ in pools for g in gs for k in ("home", "away")}
                       | {g[k] for _, _, rounds in brs for rd in rounds for g in rd
                          if g for k in ("home", "away")}
                       | {g[k] for g in loose for k in ("home", "away") if g[k]})
        local = {t: i for i, t in enumerate(field)}
        dates = sorted({g["date"] for g in games if g["date"]})
        dix = {d: i for i, d in enumerate(dates)}
        enc = lambda g: [local[g["home"]], local[g["away"]], g["hs"], g["as"],
                         dix.get(g["date"], 0)]

        # Only a bracket that actually reached a single final crowns anybody.
        # A Regional that stopped at two semifinals has no champion, and says
        # so rather than promoting one of the semifinal winners.
        champ = -1
        for kind, root_rank, rounds in brs:
            if kind == "champ" and root_rank == 0 and len(rounds[-1]) == 1 \
                    and rounds[-1][0]:
                champ = local[winner(rounds[-1][0])]
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
        place = ", ".join(x for x in (city, state) if x)
        events.append([eid, name, season, DIVCODE.get(division, 0),
                       start or "", end or "", place,
                       counts.get(eid, len(field)), tier(name), 0, champ])
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
            "detail": detail, "rounds": ROUND_NAMES, "tiers": TIER_NAMES}
