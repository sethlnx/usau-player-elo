"""How strong was the field? A tier per tournament, from who turned up.

The Tournaments tab already labels an event by where it sits in USA Ultimate's
own series — Conference, Sectionals, Regionals, Nationals. That says what the
event IS FOR, not who was in it, and the two come apart constantly: the
Northeast men's Regional is a harder tournament than most Sectionals will ever
be, and Florida Warm Up is harder than either.

The model borrowed here is Smash Bros' — specifically the Tournament Tier
System that PGStats runs for the Panda Global Rankings, which sorts events
into S/A/B/C from two inputs: how many people entered, and how many highly
ranked people entered. Three things are worth taking from it:

  * **Rank bands with steep, superlinear decay.** PGRU pays 224 points for a
    top-5 attendee against 64 for a top-50 one. Four of the top five in a room
    outweighs thirty ranked-but-ordinary entrants, which is the right shape:
    what makes a tournament hard is the ceiling, not the crowd.
  * **Regional multipliers.** A scene with fewer players should not be locked
    out of the top tier for being small. PGRU scales thresholds per region.
  * **The tier is a cutoff on a continuous score, and PGStats says so.** Both
    numbers are published here for the same reason: a high C is a low B.

Two things are deliberately NOT taken.

**Attendance is not a second path to the top.** PGRU takes the HIGHER of an
attendance score and a ranked-attendee score, because a 1,200-entrant open
bracket in Smash genuinely is a supermajor. Ultimate fields run 5 to 45 teams
and the biggest are college regular-season invites, not championships: a
16-team Nationals is the hardest tournament of its year and among the smaller
draws. Size still counts, but only through the attendees it brings — a club
outside its division-season's top fifth is worth nothing, so a 40-team
Sectional of unranked teams scores zero rather than tiering up on bulk.

**The bands are percentiles, not fixed ranks.** PGRU's top-50 works because
Smash is one global pool. Five divisions here run 67 to 500 clubs in a season,
so a fixed rank would make "top 50" mean the top tenth of college and the top
third of D-III. Percentile bands are the regional multiplier, moved from the
threshold onto the band.

## The score

Every rating used is the one the club actually carried INTO the tournament:
its last rated result strictly before the event's start date. Nothing an
attendee did at the event, or after it, can raise the grade — so a tournament
is never strong merely because someone had a breakout there, and the grade is
exactly what a spectator could have known walking in. A club with no rated
result yet is unranked and worth nothing; it has not shown anything.

A club's standing is its rank on that point-in-time rating within its own
division and season. The pool is the clubs that turn out in that division that
season, which is the competitive field the event sits in; every rating inside
it moves with the calendar. Ratings carry across divisions and seasons — one
number per club, the model's own convention — so "current" means the club's
last rated result anywhere, not its last one in this division.

Each attendee is worth points by which percentile band its rank falls in, and
the event's raw score is the sum. Raw sums are not comparable across
divisions: a pool of 500 clubs has a wider top 2% than one of 130, so it hands
out more high-band slots. The score is therefore divided by what a NOTIONAL
FULL-STRENGTH FIELD drawn from that same pool would have scored — its 16 best
clubs as of that date, 16 being the size of a USA Ultimate national
championship field in every club division. So 100 means "as hard as this
division's own Nationals ought to be", and it means the same thing in club
women's as in college. Scores above 100 are real and earned: Florida Warm Up
draws 43 college teams and beats the D-I championship it feeds.

Attendance is the set of clubs the model scored games for at that event, so a
team that registered and never played is not counted.
"""

import bisect
import collections

# PGRU's own weights, unchanged, re-aimed at percentile bands. The ratio is
# what matters: a band-1 attendee is worth 3.5 band-5 attendees.
BANDS = [(0.02, 224), (0.04, 128), (0.08, 96), (0.12, 80), (0.20, 64)]

# The notional full-strength field the score is a percentage of.
REFERENCE_FIELD = 16

# How many clubs must already carry a rating on the day for a percentile to
# mean anything. It bites in exactly two places: the first weekends of 2017,
# where the corpus starts and nobody has played yet, and D-III's
# COVID-truncated 2020, where 19 rated clubs put a single team in the top 2%
# band and a 19-team warm-up would score a perfect 100.
MIN_POPULATION = 2 * REFERENCE_FIELD

# Cutoffs on the continuous score. S is every division's Nationals plus the
# handful of college invites that out-draw one; D is the long tail that no
# ranked club attended.
TIERS = [(90, "S"), (60, "A"), (30, "B"), (10, "C"), (0, "D")]
TIER_NAMES = {
    "S": "as hard as a national championship",
    "A": "an elite field short of a championship one",
    "B": "a strong field — the better Regionals and invites",
    "C": "some ranked clubs present",
    "D": "no ranked clubs present",
}


def _decode(entry, events):
    """(event index, rating) per rated event, from the delta encoding."""
    deltas, vals = entry[0], entry[1]
    out, i = [], 0
    for k, d in enumerate(deltas):
        i += d
        if i < len(events):
            v = vals[k]
            out.append((i, v[0] if isinstance(v, list) else v))
    return out


def trajectories(history):
    """club key -> ([date], [rating]), chronological.

    Across every division: a club's rating is one number it carries
    everywhere, so what it walked into a tournament with is its last rated
    result anywhere, not its last one in this division.
    """
    events = history["events"]
    by_club = collections.defaultdict(list)
    for key, entry in history["teams"].items():
        for i, elo in _decode(entry, events):
            by_club[key].append((events[i][0], elo))
    out = {}
    for key, points in by_club.items():
        points.sort()
        out[key] = ([d for d, _ in points], [e for _, e in points])
    return out


def pools(history):
    """(division, season) -> the clubs that turn out there.

    The competitive field an event sits in. Membership is the season's, but
    every rating read out of it is the one held on the day.
    """
    events = history["events"]
    out = collections.defaultdict(set)
    for key, entry in history["teams"].items():
        for i, _elo in _decode(entry, events):
            out[(events[i][3], events[i][2])].add(key)
    return out


def _points(rank, population):
    """What one attendee at this rank is worth in a pool this deep."""
    for frac, pts in BANDS:
        if rank <= max(1, round(frac * population)):
            return pts
    return 0


def _reference(population):
    return sum(_points(r, population) for r in range(1, REFERENCE_FIELD + 1))


def score_events(history):
    """History event index -> (score, tier), for every event the model rated.

    The standings are rebuilt per event, because the whole point is that they
    move: the same club is a different rank in May than it was in February.
    An event where fewer than MIN_POPULATION clubs carry a rating yet is
    skipped rather than guessed at. An event whose attendees are all outside
    the pool's top fifth scores zero, which is tier D and a real answer.
    """
    events, clubs = history["events"], history["gameClubs"]
    traj, pool = trajectories(history), pools(history)

    def elo_on(key, day):
        """What the club carried in: its last rated result strictly before."""
        dates, elos = traj[key]
        j = bisect.bisect_left(dates, day)
        return elos[j - 1] if j else None

    out = {}
    for ev, rows in history["games"].items():
        i = int(ev)
        day, season, div = events[i][0], events[i][2], events[i][3]
        live = []
        for key in pool.get((div, season), ()):
            elo = elo_on(key, day)
            if elo is not None:
                live.append((elo, key))
        if len(live) < MIN_POPULATION:
            continue
        # Ratings are integers and the pool holds hundreds of clubs, so ties
        # are routine and land on band edges. Break them on the club key: an
        # arbitrary rule, but a STATED one, and the same one every rebuild.
        live.sort(key=lambda p: (-p[0], p[1]))
        n = len(live)
        rank = {key: r for r, (_elo, key) in enumerate(live, 1)}
        field = {r[0] for r in rows} | {r[1] for r in rows}
        raw = sum(_points(rank[clubs[c]], n) for c in field if clubs[c] in rank)
        pct = round(100 * raw / _reference(n), 1)
        out[i] = (pct, next(t for cut, t in TIERS if pct >= cut))
    return out


# analysis/rankings.py truncates the event name to 46 characters on its way
# into history.json and keeps no event id, so this is the join back to the
# tournament rows. Date, season and division make it unique on both sides:
# 2,861 history events, 2,861 distinct keys.
def _key(date, name, season, div):
    return ((date or "")[:10], (name or "")[:46], season, div)


def classify(history, events):
    """Per tournament row, [score, tier] — or None where there is no answer.

    Returned parallel to `events`, which is `tourneys["events"]`: [id, name,
    season, div, start, ...]. Events the model never rated (novelty 4v4 and
    goalty brackets, ten of them) and events in a too-thin division-season
    come back None and carry no chip.
    """
    scored = score_events(history)
    # A history event row IS (date, name, season, div) — the key, in order.
    by_key = {_key(*history["events"][i]): v for i, v in scored.items()}
    return [by_key.get(_key(e[4], e[1], e[2], e[3])) for e in events]
