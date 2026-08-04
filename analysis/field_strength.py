"""How strong was the field? A tier per tournament, from who turned up.

The Tournaments tab already labels an event by where it sits in USA Ultimate's
own series — Conference, Sectionals, Regionals, Nationals. That says what the
event IS FOR, not who was in it, and the two come apart constantly: the
Northeast men's Regional is a harder tournament than most Sectionals will ever
be, and Florida Warm Up is harder than either.

## The score is the average rating in the room

Every rating used is the one the club actually carried INTO the tournament:
its last rated result strictly before the event's start date. Nothing an
attendee did at the event, or after it, can raise the grade — so a tournament
is never strong merely because someone had a breakout there, and the grade is
exactly what a spectator could have known walking in. A club with no rated
result yet has shown nothing and is left out of the average rather than
guessed at.

This replaced a port of Smash Bros' Tournament Tier System, which scored an
event by how many of the division's top-ranked clubs attended, in steeply
decaying rank bands. It read well at the top and lied everywhere else, because
a band scheme has to end somewhere: everything outside the division's top
fifth scored ZERO, so a club ranked 43rd counted exactly as much as one ranked
211th, which is to say not at all. Select Flight Invite West 2026 drew Dark
Star (20th), SOUF (32nd) and Wavestorms (40th) and then nine clubs between
43rd and 111st, and came out D — a tier whose own label read "no ranked clubs
present". A mean has no cliff in it. Every attendee moves the number by what
they are actually worth, and the number is a rating, which is a thing people
already know how to read.

What a mean does have is a blind spot for SIZE: two elite clubs playing a
showcase game average higher than a sixteen-team Nationals, and a raw mean
duly made a 2-team exhibition championship-grade. So the average is taken
against a PRIOR of half a Nationals field's worth of merely typical teams. A
16-team event barely moves; a 2-team one is dragged most of the way back to
ordinary, because two results is not evidence of a tournament. This is the
only correction applied, and it costs one line.

## The bars are per division, and anchored at both ends

Ratings are one scale across all seven divisions, so the averages are directly
comparable — but the divisions are not equally deep, and a bar that is right
for club men's is wrong for D-III. Each division's ladder is pinned to two
facts about itself:

    S   the weakest national championship it has ever held
    C   its own median event
    A,B evenly spaced between the two

So an S is "the average team here was as strong as the average team at this
division's Nationals", and a D is "a below-average field for this division" —
the same claims everywhere, at different numbers. Every national championship
is an S by construction and stays one: the anchor is recomputed from the data,
so a thinner championship in some future season becomes the new floor rather
than dropping out of the tier.

Attendance is the set of clubs the model scored games for at that event, so a
team that registered and never played is not counted.
"""

import bisect
import collections
import re
import statistics

# Half a Nationals field. The mean is taken against this many notional clubs
# rated at the division's own median event, which is what stops a two-team
# showcase claiming the top on two data points. At 6 it removes every
# small-field S while leaving all 40 national championships in S; a 16-team
# event moves by a few Elo, a 2-team one by hundreds.
PRIOR = 6

# A division's own USAU national championship, used to pin the top of its
# ladder. The U.S. Open sits in the same series tier and is not one: it is a
# 12-team invitational, and treating it as the anchor would move the bar.
CHAMPIONSHIP_RE = re.compile(
    r"national championship|club championships|club nationals"
    r"|college championships?", re.I)
NOT_CHAMPIONSHIP_RE = re.compile(r"u\.?s\.? open", re.I)

LETTERS = ["S", "A", "B", "C", "D"]
TIER_NAMES = {
    "S": "championship-grade for this division",
    "A": "an elite field short of a championship one",
    "B": "a strong field",
    "C": "an ordinary field for this division",
    "D": "a below-average field for this division",
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


def field_means(history):
    """History event index -> (mean rating walked in with, rated attendees).

    Unshrunk, because the shrink needs a per-division typical event and that
    is not known until every event has been measured once.
    """
    events, clubs = history["events"], history["gameClubs"]
    traj = trajectories(history)
    out = {}
    for ev, rows in history["games"].items():
        i = int(ev)
        day = events[i][0]
        elos = []
        for c in {r[0] for r in rows} | {r[1] for r in rows}:
            dates, vals = traj.get(clubs[c], ((), ()))
            j = bisect.bisect_left(dates, day)
            if j:
                elos.append(vals[j - 1])
        if elos:
            out[i] = (statistics.mean(elos), len(elos))
    return out


def raw_scores(history):
    """History event index -> field rating, shrunk toward a typical event.

    Two passes: the first gets each division's median event, the second pulls
    every event that far toward it in proportion to how little of a field it
    actually was. n is the RATED attendance — an event where two clubs of
    sixteen carry a rating has two data points, whatever the draw says.
    """
    events = history["events"]
    plain = field_means(history)
    typical = {}
    for div in {e[3] for e in events}:
        vals = [m for i, (m, _n) in plain.items() if events[i][3] == div]
        typical[div] = statistics.median(vals) if vals else 0.0
    return {i: round((n * m + PRIOR * typical[events[i][3]]) / (n + PRIOR))
            for i, (m, n) in plain.items()}


def cutoffs(history, scores):
    """division -> [(rating, letter)], pinned to that division's own extremes.

    S is the weakest national championship the division has held, C is its
    median event, and A and B split the gap evenly. A division with no
    championship on record borrows the weakest in the corpus rather than
    inventing a scale for itself.
    """
    events = history["events"]
    champs, by_div = collections.defaultdict(list), collections.defaultdict(list)
    for i, m in scores.items():
        by_div[events[i][3]].append(m)
        name = events[i][1]
        if CHAMPIONSHIP_RE.search(name) and not NOT_CHAMPIONSHIP_RE.search(name):
            champs[events[i][3]].append(m)
    overall = min([m for ms in champs.values() for m in ms] or [0])
    out = {}
    for div, vals in by_div.items():
        top = min(champs[div]) if champs.get(div) else overall
        base = statistics.median(vals)
        step = (top - base) / 3
        out[div] = [(round(top - k * step), LETTERS[k]) for k in range(3)]
        # D has no threshold to print — it is "below C", and a sentinel here
        # leaks into the ladder the page draws.
        out[div] += [(round(base), "C"), (None, "D")]
    return out


def letter_for(cuts, rating):
    return next(t for c, t in cuts if c is None or rating >= c)


def score_events(history):
    """History event index -> (field rating, letter)."""
    scores = raw_scores(history)
    cuts = cutoffs(history, scores)
    events = history["events"]
    return {i: (m, letter_for(cuts[events[i][3]], m)) for i, m in scores.items()}


# analysis/rankings.py truncates the event name to 46 characters on its way
# into history.json and keeps no event id, so this is the join back to the
# tournament rows. Date, season and division make it unique on both sides:
# 2,861 history events, 2,861 distinct keys.
def _key(date, name, season, div):
    return ((date or "")[:10], (name or "")[:46], season, div)


def classify(history, events):
    """(verdicts, cuts) — verdicts parallel to `events`, cuts per division.

    A verdict is [field rating, letter], or None where there is no answer:
    `events` is `tourneys["events"]`, and an event where not one attendee had
    a rating yet — the first weekends of the corpus, and the novelty 4v4 and
    goalty brackets the model never rated — carries no chip. `cuts` is what
    the page prints so the bar is never a mystery; it differs per division by
    construction.
    """
    scores = raw_scores(history)
    cuts = cutoffs(history, scores)
    hev = history["events"]
    # A history event row IS (date, name, season, div) — the key, in order.
    by_key = {_key(*hev[i]): (m, letter_for(cuts[hev[i][3]], m))
              for i, m in scores.items()}
    return ([by_key.get(_key(e[4], e[1], e[2], e[3])) for e in events],
            {str(d): c for d, c in cuts.items()})
