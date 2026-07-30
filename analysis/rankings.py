"""Export final player and team Elo tables to CSV.

Replays the full game history with the given config, then writes:
  data/player_elo.csv — every player: rating, games, last club/season seen
  data/team_elo.csv   — clubs in the most recent season, rated from their
                        latest event roster (the roster-derived team rating)

Usage: python -m analysis.rankings
"""

import csv
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import (DB_PATH, load_games, load_maps,
                               load_stat_events, load_ufa_stat_events, replay)
from elo.engine import EloConfig


# Uncertainty in the rating AS AN ESTIMATE OF CURRENT SKILL. It converges with
# games, as an Elo interval should, to a resolution floor — it is NOT a
# forecast of future rating movement.
#
# The previous form, sqrt(110^2 + 445^2/n), was fit to "how far a rating still
# moves before settling". That conflates two things. Measuring movement over a
# fixed horizon H shows the 110 was pure skill DRIFT: past ~20 games the sd of
# future movement is flat in n and scales as sqrt(H) — a random walk of ~12
# Elo per game (43/66/118 at H=10/30/100), with near-zero mean reversion. A
# drift band is a forecast and needs a stated horizon; 110 silently meant
# "about 67 games out". It does not belong in a column named lo90/hi90.
#
# Trajectories cannot measure the remaining error, only whether the rating
# still moves: past the provisional window it stops dead (self-correcting
# component ~155 through n=14, 107 by n=18, ~0 by n=21 — the 6x multiplier
# slews a newcomer into place, then freezes). Stability is not accuracy, so
# accuracy is measured by split-half instead: replay the corpus twice on
# alternating games and compare the two independent estimates of the same
# player over the same period, where real drift is common and cancels.
# Over n in [50, 400], where both halves clear the provisional window,
# sigma = sd/2 reads 88, 77, 69, 67, 64 — fitting sqrt(551^2/n + 53^2).
#
# The 53 floor is identification, not drift: a player is only resolved apart
# from his teammates by appearing in varied roster combinations, and shared
# deltas leave a residue no game count removes. Caveats, both real: halving
# the corpus degrades every teammate and opponent rating too, so the fit runs
# somewhat wide; and below n~20 it is extrapolation rather than measurement.
#
# Re-fit whenever the engine's update dynamics change — these constants are
# properties of the replay, not of the sport. History of this fit:
#   cliff, 2021-2025 corpus            sqrt(551^2/n + 53^2)
#   exponential, 2021-2025             sqrt(415^2/n + 48^2)
#   hyperbolic + reg=0.15, 2017-2026   sqrt(275^2/n + 51^2)
#   hyperbolic + reg=0.0,  2017-2026   96.0  (flat)
#   + softmax tau=600, k=44            sqrt(340^2/n + 98^2)
#
# The 1/n term is BACK, though weakly. Under the plain roster mean it fitted to
# exactly zero: a rating's uncertainty did not fall with experience at all,
# because the hyperbolic multiplier never decays and reg=0 removed every pull
# toward an anchor. Softmax weighting restores a little convergence by a
# different route - a game's delta is shared by the whole roster, but the team
# rating a delta is computed FROM now leans on the players who are actually
# identified, so evidence concentrates instead of smearing evenly across 25
# names. Split-half reads 105.6 / 105.9 / 103.4 / 98.5 at mean n = 63 / 97 /
# 145 / 210 - a real if shallow decline, where before there was none.
#
# Read the levels honestly: the floor rose 96 -> 98 and the band is WIDER at
# every n a player actually reaches (±180 at n=30, ±168 at n=100, ±163 at
# n=300, against a flat ±158). Better game prediction has been bought with
# slightly less certainty about any individual, which is the same trade the
# reg=0 note below describes. The gradient is the honest part: experience now
# buys something, where under the previous config it bought nothing.
def rating_sigma(games: int) -> float:
    return math.sqrt(340.0 ** 2 / max(games, 1) + 98.0 ** 2)


Z90 = 1.645  # two-sided 90% interval


def last_appearance(con):
    """player_id -> (display_name, club, season) for the latest event seen."""
    rows = con.execute("""
        SELECT rp.player_id, p.display_name,
               COALESCE(et.full_name, et.display_name) AS club,
               ev.season, ev.start_date
        FROM roster_players rp
        JOIN players p USING (player_id)
        JOIN event_teams et USING (event_team_id)
        JOIN events ev ON ev.event_id = et.event_id
        ORDER BY ev.start_date
    """).fetchall()
    latest = {}
    for pid, name, club, season, _ in rows:
        latest[pid] = (name, club, season)
    return latest


def latest_rosters(con, season: int, basis: str = "completed"):
    """club -> player_ids from one club-event roster, chosen by `basis`.

    Division-scoped because the team tables are club-only; college event teams
    share the same season and would otherwise be ranked alongside them.

    "completed" takes each club's most recent finished event. This is the
    results-grounded default: USAU posts rosters weeks ahead, so rating a team
    off a tournament it has not played makes the table jump on registration
    timing rather than on results.

    "upcoming" takes the NEXT event a club has registered for — the squad it
    is about to field rather than the one it last fielded. Strictly ahead of
    today, because an event with a NULL end_date is indistinguishable from an
    in-progress one and its roster is already stale. Only clubs registered for
    a future event appear, so this table is a fraction of the size and is NOT
    a full national ranking.
    """
    if basis == "completed":
        when, order = "ev.end_date IS NOT NULL AND ev.end_date <= date('now')", "ASC"
    elif basis == "upcoming":
        when, order = "ev.start_date > date('now')", "DESC"
    else:
        raise ValueError(f"basis must be 'completed' or 'upcoming', got {basis!r}")
    rows = con.execute(f"""
        SELECT COALESCE(et.full_name, et.display_name) AS club,
               ev.start_date, et.event_team_id, ev.name
        FROM event_teams et
        JOIN events ev ON ev.event_id = et.event_id
        WHERE ev.season = ?
          AND COALESCE(ev.division, 'club') = 'club'
          AND {when}
        ORDER BY ev.start_date {order}
    """, (season,)).fetchall()
    # Last row wins: ASC keeps the latest completed event, DESC the soonest
    # upcoming one.
    latest_etid, source = {}, {}
    for club, sd, etid, evname in rows:
        latest_etid[club] = etid
        source[club] = (evname, sd)
    rosters, _ = load_maps(con)
    return ({club: rosters.get(etid, []) for club, etid in latest_etid.items()},
            source)


# Published config. SOFTMAX-weighted team rating (tau=600) with stat-driven
# within-team transfers on and usage credit OFF; debuts enter at the division
# base (context_init off) and converge via a GRADUATED provisional multiplier.
#
# SELECTION PROTOCOL (changed; read this before tuning anything).
# The corpus splits three ways, not two:
#     FIT  2017-2021  8,697 club games   ratings accumulate here
#     VAL  2022-2023  4,636 club games   hyperparameters are CHOSEN here
#     TEST 2024-2025  5,474 club games   reported, never selected on
# The old two-way split chose on 2017-2021 and it demonstrably fails. Those are
# the cold-start seasons: every player enters at base with no history, so any
# parameter that imports outside information scores well there and is inert or
# harmful once ratings are informative. Three parameters were caught doing
# exactly this - home_advantage (see below), stat_transfer_beta (FIT improves
# monotonically out to beta=50 while 2022-25 degrades monotonically), and
# involvement_credit. Selecting on VAL, whose ratings are mature, matches the
# regime any real prediction runs in.
# Diagnostic to apply to every future move: print its per-season gain. A move
# that helps only 2017-2019 and decays toward zero is a cold-start artifact.
# A move that HURTS 2017 and helps later - as tau does - is the good sign.
#
# Coordinate descent, 3 passes, 15 axes, selected on VAL, then every surviving
# move dropped back one at a time and kept only if it cost >0.0003 VAL logloss.
# That pruning matters: the full 9-move descent output scored TEST 0.45403,
# while the 3 moves that survived score 0.45292. The extra six were fitting VAL.
# Rejected as noise by paired bootstrap on VAL: provisional_games 8->13
# (-0.00036, CI [-0.00139,+0.00070]), base_college 1150->1075 (-0.00046,
# CI [-0.00106,+0.00012]), plus scale_club, scale_college, mov_norm,
# stat_transfer_clamp, involvement_shrink, base_ufa.
#
# The three real moves, vs the previous published config:
#   tau  inf -> 600   the whole story, worth 0.0073 VAL. A plain roster mean
#                     says a club is its average player; softmax at 600 says a
#                     player 900 Elo above his worst teammate carries 4.5x the
#                     weight. That is the playing-time assumption the engine
#                     docstring always claimed, finally earning its place: it
#                     lost under the old split only because home_advantage was
#                     absorbing the same signal.
#   k    40  -> 44    +0.0007 VAL. 48 ties; 44 is the interior choice.
#   involvement_credit True -> False  +0.0006 VAL, and only once tau is finite.
#                     At tau=inf it is worth nothing either way (0.46089 vs
#                     0.46097); at tau=600 turning it off wins. Both mechanisms
#                     answer "who on this roster matters", and rating-based
#                     softmax does it better than stat-line usage. Mechanism B
#                     (stat_transfer_beta) is unaffected and stays at 3.
#
# TEST 2024-25: logloss 0.4579 -> 0.4529, paired 90% CI [-0.00818, -0.00177];
# accuracy 0.7769 -> 0.7822; brier 0.1501 -> 0.1476. Per-season gain is +0.003
# to +0.011 in every season from 2018 on and -0.0025 in 2017 alone.
#
# provisional_shape: was a cliff (flat 6x for 20 games, then 1x), which
# over-credited games 10-20 and froze a rating dead at game 20. Hyperbolic
# decay, 1 + (M-1)*N/(N+games), wins clearly at the tuned point: train 0.4852
# vs 0.4886 exponential / 0.4940 cliff, holdout 0.4697 vs 0.4778 / 0.4787. It
# was REJECTED on the old 2021-2025 corpus because at M=6/N=20 it inflated the
# scale (top rating 2821 -> 3201) on 4.6x the cliff's credit budget, which
# confounded the shape with a global k rise. Co-tuning M (9->7), N (10->8) and
# k together answers that: k had a 28..56 grid to fall into and stayed at 40,
# and the residual inflation is ~8% (top 2810 exponential -> 3023 hyperbolic).
# The real cost is not inflation but non-convergence — see rating_sigma above.
#
# division_bases["college"] 1300 -> 1150. Club logloss CANNOT identify this
# knob: train is flat to five decimals across 1150-1450 (0.48520/0.48521) and
# so is holdout club. Holdout COLLEGE separates it cleanly (0.4850 at 1150 vs
# 0.4910 at 1300, 0.4983 at 1450), so it is chosen on college evidence at zero
# club cost. On the old 2021-start corpus 1150 won train and LOST holdout club
# significantly (+0.0016); adding 2017-19 club history removed that conflict —
# a knob the smaller corpus could not resolve.
#
# division_scale club 290 -> 260, home_advantage 25 -> 35: both verified as
# interior optima after the first grid hit its edges (260: 245->0.48535,
# 275->0.48545; 35: 30->0.48542, 40->0.48530). club=290 previously matched the
# 286 that usau_baseline.py fits independently, so 260 is now a real divergence
# from that cross-check and worth revisiting if the baseline is re-fit.
#
# Caveat this config does NOT fix: low-game entities. A roster whose whole
# history is 7-13 games sits in the high-multiplier region under every shape
# (Magnitude (Quake)-Senior moved 1906 -> 1908 under exponential). That is an
# eligibility/uncertainty problem for the published tables, not a curve problem.
# offseason_regression 0.15 -> 0.0, the single largest gain of the re-tune:
# train club 0.4852 -> 0.4784, holdout club 0.4697 -> 0.4598 (paired 90% CI
# [-0.0114, -0.0082]) with accuracy UP too, 0.7674 -> 0.7725, and holdout
# college 0.4850 -> 0.4818. Monotone across 0.35..0.0, optimum at the boundary.
#
# It was found by asking why deeper history hurt. Holding the config fixed and
# truncating the corpus, adding 2018 and 2017 each made holdout WORSE
# (+0.00086 and +0.00043, both CIs excluding zero) while 2019 helped. The
# suspect was stale evidence surviving too long; the actual cause was the
# opposite — regression toward base was DISTORTING old history rather than
# preserving it. At reg=0 the harm disappears: adding 2018 (+0.00027) and 2017
# (-0.00049) both have CIs spanning zero, while 2019 helps by -0.00393.
#
# Two consequences worth knowing:
#  - Inactive players no longer decay, so a rating is now a player's LAST KNOWN
#    rating rather than a decayed one. player_elo.csv stops burying retirees and
#    current elo == career-end elo. This is a change in what the column means.
#  - division_bases now only sets where a debut enters; it is no longer the
#    target of a between-season pull, so the "regressing toward the generic base
#    would inflate the college pool" argument in engine.new_season is moot at
#    this setting (the code path still exists for reg > 0).
#
# A verification pass at reg=0 nudged provisional_games 8->6 and club scale
# 260->240 on train point estimates, but a paired bootstrap ON TRAIN put both
# inside its own noise (CIs spanning zero; the scale move was actually +0.00057
# worse when paired). Both rejected on train evidence, not on holdout.
# home_advantage 35 -> 0, REMOVED. There is no home field: ultimate tournaments
# are played at neutral sites, and games.home_id is merely the team USAU prints
# first, which is the seed. The listed-first team wins 70.1% of club games, and
# the effect is a pure seeding artifact — 78-81% in pools A-D, decaying to 62%
# by the semis, which no venue effect would do.
#
# It is dropped for two reasons. First, independence: the parameter imports
# USAU's seeding judgement into a model meant to rate teams from results alone,
# and it makes every prediction require knowing the schedule's listing order.
# Second, it does not work on data worth predicting. Removing it is free on the
# holdout — paired Δlogloss -0.00003, 90% CI [-0.00211, +0.00197] — and the
# holdout optimum was ~20, not the 35 that train chose.
#
# The per-season breakdown shows why train and holdout disagreed. The gain from
# +35 is +0.0193 in 2017, +0.0047 in 2018, +0.0024 in 2019, then ~0 or negative
# from 2021 on. In 2017 the ratings expected the listed-first team to win 62.3%
# against an actual 73.0%: with every player at base, USAU's seed genuinely knew
# more than the model. By 2022-25 that gap is 3-4 points and the bonus is inert.
# It was a cold-start crutch, and TRAIN (2017-2021) is exactly the window the
# cold start dominates. Note the failure mode: the train/holdout split guards
# against fitting noise, NOT against a train period structurally unlike the
# target. Re-check any parameter whose per-season gain trends toward zero.
#
# A verification pass at ha=0 nudged club scale 260->240, k 40->44 and
# provisional_multiplier 7->8 on train point estimates; a paired bootstrap ON
# TRAIN put all three inside its own noise. k 40->44 was later re-selected on
# VAL, where it is real; the other two remain rejected.
PUBLISHED = dict(tau=600.0, involvement_credit=False,
                 involvement_shrink=1.0, stat_transfer_beta=3.0,
                 provisional_shape="hyperbolic",
                 provisional_multiplier=7.0, provisional_games=8,
                 k=44.0, home_advantage=0.0, offseason_regression=0.0,
                 division_scale={"club": 260.0, "college": 300.0},
                 division_bases={"club": 1500.0, "college": 1150.0,
                                 "ufa": 1550.0})


# A club's best roster must be at least this fraction of the largest squad it
# fielded that season. Picking the max-rated roster with no floor selects the
# SMALLEST one: a mean over an elite subset beats a mean over a full squad, and
# partial entries are common (a men's contingent at a cross-listed mixed event,
# a short-handed one-day round robin). Skeleton Squad's 11-man Mixed Easterns
# entry out-rates its 25-man Boston Invite squad by 85 Elo, which says nothing
# about how strong Skeleton Squad is. 30% of multi-roster clubs peak on a
# below-average-size roster, so the floor is doing real work.
FULL_SQUAD_FRACTION = 0.8


def best_rosters(con, season: int, model):
    """club -> (player_ids, event, date): the club's highest-rated FULL squad.

    "Best version" in the sense of the strongest lineup a club has actually
    listed this season, completed or upcoming, rather than whichever roster
    happens to be most recent. Rosters below FULL_SQUAD_FRACTION of the club's
    own largest are ineligible; see the note above.
    """
    rows = con.execute("""
        SELECT COALESCE(et.full_name, et.display_name) AS club,
               ev.name, ev.start_date, et.event_team_id
        FROM event_teams et
        JOIN events ev ON ev.event_id = et.event_id
        WHERE ev.season = ? AND COALESCE(ev.division, 'club') = 'club'
        ORDER BY ev.start_date
    """, (season,)).fetchall()
    rosters, _ = load_maps(con)
    cand = {}
    for club, evname, sd, etid in rows:
        pids = rosters.get(etid)
        if pids:
            cand.setdefault(club, []).append((pids, evname, sd))
    out, source = {}, {}
    for club, entries in cand.items():
        floor = FULL_SQUAD_FRACTION * max(len(p) for p, _, _ in entries)
        full = [e for e in entries if len(e[0]) >= floor]
        pids, evname, sd = max(full, key=lambda e: model.team_rating(e[0]))
        out[club], source[club] = pids, (evname, sd)
    return out, source


def main(cfg: EloConfig | None = None):
    cfg = cfg or EloConfig(**PUBLISHED)
    con = sqlite3.connect(DB_PATH)
    games = load_games(con)
    rosters, clubs = load_maps(con)
    stat_events = sorted(load_stat_events(con) + load_ufa_stat_events(con),
                         key=lambda e: e[0])
    _, model = replay("player", games, rosters, clubs, cfg, stat_events)

    latest = last_appearance(con)
    out = DB_PATH.parent / "player_elo.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        # player_id is exported because display names are NOT unique: ambiguous
        # names are split per-club into separate players, so several rows can
        # read "Julian Kagi" with different ratings. Join on the id, never the name.
        w.writerow(["rank", "player", "player_id", "elo", "sigma", "lo90", "hi90",
                    "games", "last_club", "last_season"])
        ranked = sorted(
            ((st.rating, pid, st.games) for pid, st in model.players.items()
             if not str(pid).startswith("ghost:") and st.games >= 5),
            reverse=True)
        for i, (rating, pid, ngames) in enumerate(ranked, 1):
            name, club, season = latest.get(pid, ("?", "?", "?"))
            s = rating_sigma(ngames)
            w.writerow([i, name, pid, round(rating, 1), round(s, 1),
                        round(rating - Z90 * s, 1), round(rating + Z90 * s, 1),
                        ngames, club, season])
    print(f"wrote {out} ({len(ranked)} players with 5+ games)")

    season = con.execute("SELECT max(season) FROM events WHERE has_schedule=1").fetchone()[0]
    for basis, fname in (("completed", "team_elo.csv"),
                         ("upcoming", "team_elo_upcoming.csv"),
                         ("best", "team_elo_best.csv")):
        team_out = DB_PATH.parent / fname
        rosters_by_club, source = (best_rosters(con, season, model)
                                   if basis == "best"
                                   else latest_rosters(con, season, basis))
        with open(team_out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["rank", "club", "elo", "roster_size", "season",
                        "roster_event", "roster_event_date"])
            rated = [(model.team_rating(pids), club, len(pids))
                     for club, pids in rosters_by_club.items() if pids]
            for i, (rating, club, size) in enumerate(sorted(rated, reverse=True), 1):
                evname, sd = source[club]
                w.writerow([i, club, round(rating, 1), size, season, evname, sd])
        print(f"wrote {team_out} ({len(rated)} teams, season {season}, "
              f"{basis} rosters)")
    con.close()


if __name__ == "__main__":
    main()
