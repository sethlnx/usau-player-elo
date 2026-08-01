"""Export final player and team Elo tables to CSV.

Replays the full game history with the given config, then writes:
  data/player_elo.csv — every player: rating, games, last club/season seen
  data/team_elo.csv   — clubs in the most recent season, rated from their
                        latest event roster (the roster-derived team rating)

Usage: python -m analysis.rankings
"""

import csv
import json
import math
import sqlite3
import sys
from collections import defaultdict
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
#   cliff, 2021-2025 corpus              sqrt(551^2/n + 53^2)
#   exponential, 2021-2025               sqrt(415^2/n + 48^2)
#   hyperbolic + reg=0.15, 2017-2026     sqrt(275^2/n + 51^2)
#   hyperbolic + reg=0.0,  2017-2026     96.0  (flat)
#   + softmax tau=600, k=44              sqrt(340^2/n + 98^2)
#   + college 2017-2020 backfill, k=48   113.0 (flat)
#   + D-III division, d3 base 1250       112.0 (flat)
#   + mixed/women, tau=900, beta=8       103.0 (flat)
#
# The 1/n term stays exactly zero. Split-half reads 103.4 / 102.1 / 104.1 /
# 102.4 at mean n = 35 / 71 / 139 / 241 — no trend across a 6.8x range in
# games played, on 43,000 players. Same structural finding as before: a
# rating's uncertainty does not fall with experience, because hyperbolic never
# decays to 1 and offseason_regression=0 leaves nothing pulling toward an
# anchor. Anyone wanting convergence pays for it with reg > 0, and the descent
# priced that: reg is an axis and it stayed at 0.
#
# 112 -> 103 is not the ratings getting better pinned down in absolute terms.
# tau 600 -> 900 flattens the softmax, which inflates the whole scale (top
# rating 3199 -> 3444), so the band shrank while the ruler grew — roughly a
# 15% tightening once expressed as a fraction of the spread. The honest band
# is a flat +/-169.
#
# CAVEAT this fit does NOT capture: it is one number for everyone, and the
# uncertainty is plainly not uniform. Bucketing by how much softmax weight a
# player carries in their own team's rating, split-half sd/2 runs 161 for the
# lowest-influence tenth against 101 for the highest. A player who never moves
# their team's rating is barely measured at all (see low_info_anchor), and
# this column says otherwise. Fixing it means a sigma that takes influence as
# an argument, not just games.
def rating_sigma(games: int) -> float:
    return 103.0


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


def latest_rosters(con, season: int, basis: str = "completed",
                   division: str = "club-men"):
    """club key -> player_ids from one event roster, chosen by `basis`.

    Division-scoped: one call rates one division. Teams from two divisions
    share a season and would otherwise be ranked against each other, and 73
    club names exist in more than one gender division, so a name alone is not
    a unique row.

    "completed" takes each club's most recent finished event. This is the
    results-grounded default: USAU posts rosters weeks ahead, so rating a team
    off a tournament it has not played makes the table jump on registration
    timing rather than on results.

    "upcoming" takes the next roster a club will field that is not yet in the
    books — a future registration OR an event being played right now. The
    earlier rule was `start_date > today`, which silently emptied the table on
    the morning of the tournament it was built for: the U.S. Open dropped from
    13 teams to 1 the moment its start date arrived, and every rating on the
    site's U.S. Open tab went null.

    An event still counts while `end_date >= today`. A NULL end_date is only
    trusted when the event has not started, since a null-dated event already
    under way is indistinguishable from a stale one whose roster has rotted.
    Only clubs with such an event appear, so this table is a fraction of the
    size and is NOT a full national ranking.
    """
    if basis == "completed":
        when, order = "ev.end_date IS NOT NULL AND ev.end_date <= date('now')", "ASC"
    elif basis == "upcoming":
        when, order = ("(ev.end_date >= date('now') OR "
                       " (ev.end_date IS NULL AND ev.start_date >= date('now')))"), "DESC"
    else:
        raise ValueError(f"basis must be 'completed' or 'upcoming', got {basis!r}")
    rows = con.execute(f"""
        SELECT COALESCE(et.full_name, et.display_name) AS club,
               ev.start_date, et.event_team_id, ev.name
        FROM event_teams et
        JOIN events ev ON ev.event_id = et.event_id
        WHERE ev.season = ?
          AND COALESCE(ev.division, 'club-men') = ?
          AND {when}
        ORDER BY ev.start_date {order}
    """, (season, division)).fetchall()
    # Last row wins: ASC keeps the latest completed event, DESC the soonest
    # upcoming one.
    rosters, clubs = load_maps(con)
    latest_etid, source, display = {}, {}, {}
    for club, sd, etid, evname in rows:
        key = clubs.get(etid)
        if not key:
            continue
        latest_etid[key] = etid
        source[key] = (evname, sd)
        display[key] = club
    return ({key: rosters.get(etid, []) for key, etid in latest_etid.items()},
            source, display)


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
#
# COLLEGE 2017-2020 BACKFILL. The corpus gained 15,466 college games, taking
# FIT from 892 college games against 8,697 club to 14,320 against 8,697, and
# bridge players from 8,145 to 12,905. Re-running the published config on it
# made things WORSE (TEST 0.4529 -> 0.4580), which is the protocol note above
# doing its job: the config had been fitted to a corpus where college barely
# existed before 2021.
#
# The reason is one parameter. division_bases["college"] was UNIDENTIFIABLE on
# the old corpus — flat to five decimals from 700 to 1450 — because club
# logloss cannot see a division that has almost no games in the window. It was
# a pure prior, and it was wrong. With real college history it becomes sharply
# identifiable and wants 1350, not 1150. That one move recovers the entire
# regression on its own (TEST 0.4580 -> 0.4529).
#
# Re-selected on VAL, then pruned one move at a time (kept only if reverting
# costs >0.0003 VAL). Five survive; the 8-move descent scored TEST 0.45148 and
# these five score 0.45165, so the other three were fitting VAL:
#   college_base   1150 -> 1350   +0.00355   see above
#   provisional_games  8 -> 14    +0.00153   more college debutants to absorb
#   scale_college   300 -> 260    +0.00096   now measurable, and club-like
#   prov_multiplier   7 -> 6      +0.00076   flatter, longer newcomer window
#   k                44 -> 48     +0.00047
# Rejected as noise: tau 600->700 (+0.00006), stat_transfer_beta 3->5
# (+0.00014). Beta staying at 3 matters — on FIT alone it climbs to 50 and
# wrecks the holdout, the same cold-start trap home_advantage fell into.
#
# TEST 2024-25: logloss 0.4580 -> 0.4517, accuracy 0.7830 -> 0.7855, brier
# 0.1497 -> 0.1468; paired 90% CI [-0.00898, -0.00374]. Against the config
# published BEFORE the backfill (0.4529 / 0.7822) it is a genuine net gain.
# Per-season the change hurts 2017-2019 (-0.002 to -0.006) and helps 2021-2025
# (+0.005 to +0.014) — the healthy direction, not a cold-start artifact.
# D-III (division "college-d3", 257 events / 4,950 games, 2017-2026).
#
# Its base is 1250, and finding that took correcting a mistake worth writing
# down: it was being scored on CLUB games. That is the wrong scoreboard. 84% of
# D-III players also appear at D-I events — D-III programs play D-I invites
# constantly — so only 1,930 players are D-III-only and the base barely moves
# club logloss at all. Judged there it looks unidentifiable and the two halves
# of the split fight: VAL is FLAT to 0.0001 across bases 100-700 while TEST
# slides monotonically the other way, best near 1350-1500. Eight hundred Elo of
# disagreement, which is what an unidentified parameter looks like.
#
# Scored on the division it actually governs, it is sharp and unanimous:
#     base        700     900    1100    1250    1350    1500    1700
#     D-III VAL   .5333   .5065  .4879   .4819   .4829   .4930   .5247
#     D-III TEST  .4851   .4705  .4612   .4599   .4628   .4739   .5034
#     D-III acc   .7631   .7683  .7714   .7796   .7745   .7652   .7415
# VAL, TEST and accuracy all bottom out at 1250 — the only parameter in the
# whole sweep where they agree on an interior optimum. It also sits where D-III
# players actually converge (median 1290), so debutants stop entering ~190 Elo
# below their own eventual level.
#
# GENERAL LESSON: score a division's base on that division's games. Club
# logloss is the headline metric but it cannot see a pool it barely touches,
# and forcing the question through it produces exactly the VAL/TEST civil war
# above.
#
# On club the move is neutral — paired TEST Δ -0.00018, CI [-0.00045,+0.00008].
# It is published for D-III TEST logloss 0.4596 -> 0.4575, with college also
# improving 0.4799 -> 0.4789. D-III accuracy slips 0.7765 -> 0.7724: better
# calibrated, marginally worse at discrete calls, the same trade as the 2021
# tune, and logloss is the target.
#
# A full 14-axis VAL descent was run alongside this and REJECTED. It gained
# 0.0027 on VAL and exactly zero on TEST (0.45159 -> 0.45159) while dropping
# accuracy — VAL overfitting, plainly. Its four club-side moves (mov_norm 6,
# provisional_multiplier 5, provisional_games 24, college_scale 220) are inside
# noise on TEST (Δ -0.00014, CI [-0.00099,+0.00077]) and make college worse.
# CLUB-MIXED and CLUB-WOMEN (822 + 549 events, 23,851 + 8,426 games,
# 2017-2026). Both are rated on the SAME scale as the men's divisions rather
# than in their own pools: 10,814 names appear in both mixed and a men's
# division and 4,981 in both mixed and women's, so mixed is a dense bridge and
# a player's rating follows them across it. Men's<->women's is NOT a bridge —
# identity.resolve splits those 279 names as two people, since no body plays
# both series.
#
# Bases chosen on VAL by the division's own games, per the D-III lesson above.
# The two interact (they share players through mixed) so they were swept
# jointly, 5x5 coarse then 3x5 fine, and (1500, 1600) is the joint optimum:
#     mb\wb        1450     1500     1550     1600     1650
#     1450       .44192   .44155   .44137   .44137   .44159
#     1500       .44236   .44182   .44147  [.44131]  .44136
#     1550       .44323   .44249   .44196   .44162   .44148
# (n-weighted mean of mixed and women VAL logloss.) Read the flatness, not the
# minimum: the whole grid spans 0.0019 and its interior spans 0.0005, so these
# bases are identified to about +/-100 and no further. What the surface does
# say is that neither pool wants to enter BELOW club — a women's debutant
# priced at 1450 is worse than one priced at 1600 on every row.
#
# division_scale club-women 200, against 260 everywhere else, is the one sharp
# result here: VAL runs .3861/.3755/.3702/.3678/[.3672]/.3676/.3702 across
# 120/140/160/180/200/220/260 and TEST agrees, bottoming at the same place.
# The women's division is simply more stratified — same-corpus logloss 0.375
# against 0.450 for club — and a narrower scale is what turns a given rating
# gap into the larger win probability that stratification implies. club-mixed
# sits at an interior 260, matching club and college (230 -> .46870,
# 260 -> .46847, 290 -> .46949).
#
# Effect on the existing club table: NONE, which is the point. Replaying the
# published config on the pre-merge corpus and this one on the merged corpus,
# club TEST 2024-25 moves 0.45140 -> 0.44977, paired 90% CI
# [-0.00412, +0.00086] — a CI spanning zero. Two divisions were added to the
# ratings without moving the division the site was built on.
# low_info_anchor 0.01, and it is the first knob here NOT chosen on logloss —
# it costs logloss. See EloConfig.low_info_anchor for the mechanism and the
# full price list.
#
# The pathology: softmax decides a player's INFLUENCE on the team rating while
# the delta is applied equally, and at the bottom those come apart completely.
# A player 2,300 Elo below his roster's best carries 0.4% of a 25-man team
# rating, so results say nothing about him, but he keeps absorbing team-level
# noise — and with offseason_regression at 0 nothing pulls him back. Whatever
# the 6x provisional window did in his first 14 games is permanent. Alexander
# Vencill went 10-27 as a college B-teamer, fell 1251 -> 157 at -30/game, then
# went 54-46 over 100 games and recovered +0.78/game: 340 players with 100+
# games sat below 900 Elo and 13 below zero.
#
# Why the protocol at the top of this file could not catch it: delete all 340
# from every roster and TEST logloss moves by at most 0.0012 in any division.
# They are inert. A rating no objective can see is unidentified, not low, and
# selecting on logloss will happily publish nonsense for it. Any future knob
# governing players who do not move the team rating needs the same treatment —
# judge it on the pathology, and state the logloss it costs.
#
# The price at 0.01, paired 90% CI, TEST 2024-25:
#   club        0.44977 -> 0.45117  +0.00141  [+0.00102, +0.00179]
#   club-mixed  0.46755 -> 0.46749  -0.00006  [-0.00037, +0.00026]
#   club-women  0.37539 -> 0.37591  +0.00053  [+0.00007, +0.00100]
#   college     0.47854 -> 0.47854  +0.00000  [-0.00024, +0.00023]
#   college-d3  0.45754 -> 0.45769  +0.00015  [-0.00021, +0.00050]
# Club is a real cost, not noise. It buys 340 stranded ratings down to 54, a
# floor of +554 instead of -315, and leaves the top of the table alone (Joe
# White 3194 -> 3199, Kameryn Groom 3104 -> 3098).
#
# The rejected alternative was crediting the delta in PROPORTION to softmax
# weight, which makes accountability match influence and sounds righter than
# this does. It is a disaster: club TEST 0.4498 -> 0.4737. Weighting the
# credit starves the model of information about most of a roster, and the
# roster IS the model. Equal credit stays.
# FIVE-DIVISION DESCENT (analysis/descent.py). 21 axes, 3 passes, selected on
# VAL, then every surviving move dropped one at a time and kept only if
# reverting cost >0.0003. It converged after two passes and TWELVE of the
# thirteen improving moves were pruned as VAL noise. Three survive:
#   tau                    600 -> 900   +0.00069 on reversion
#   stat_transfer_beta       3 -> 8     +0.00033
#   division_bases college 1350 -> 1250 +0.00046  (scored on college games)
#
# The objective changed and that is the substantive part. Selection is now the
# n-weighted VAL logloss over all five divisions, not club men's alone: club
# men's is 19,682 of 90,284 games, so tuning global knobs on it would let 78%
# of the corpus be collateral. Per-division bases and scales are still scored
# on their own division. The trade lands where you would expect — club men's
# TEST gives up +0.00105 (CI [-0.00068, +0.00278], noise) while club-mixed
# takes -0.00243 and college -0.00201, both CIs excluding zero. Weighted TEST
# 0.45761 -> 0.45685. No division moves significantly the wrong way.
#
# THE PROVISIONAL WINDOW EARNS ITS PLACE, and it is not close. Swept at the
# selected config, weighted VAL against the published point:
#   multiplier   1      2      3      4     [6]     8      12
#                +.0297 +.0154 +.0071 +.0025  —    +.0036 +.0231
#   games        4      6      10    [14]    20     30     50
#                +.0031 +.0014 +.0001  —    +.0007 +.0027 +.0071
#   shape        cliff  linear exp   [hyperbolic]
#                +.0092 +.0087 +.0039  —
# Turning it OFF (multiplier 1) costs 0.0297 — twenty times the entire
# descent's gain, and the single largest effect of any knob in this file.
# M=6 and N=14 are interior optima and hyperbolic beats every other shape.
#
# Worth holding both facts at once: the same 6x window is what strands players
# (see low_info_anchor — Alexander Vencill's whole 1,100-Elo hole was dug
# inside it). It is simultaneously the most valuable mechanism in the model
# and the source of its worst pathology. The answer is the anchor, not removal.
#
# low_info_anchor 0.01 -> 0.02, forced by tau. A flatter softmax pushes every
# player's share toward an equal one, which is exactly the quantity the anchor
# scales on, so at tau=900 the old 0.01 went slack: stranded players went
# 54 -> 120 and the floor -554 -> 477. At 0.02 the anchor is both stronger and
# CHEAPER than it was at tau=600 — 36 stranded, floor 657, and weighted VAL
# actually improves (0.45930 -> 0.45917). Re-check this whenever tau moves.
#
# home_advantage was swept as a diagnostic and NOT adopted, though it now wins
# on the numbers: 15 scores VAL 0.45784 against 0.45930 at zero, and TEST
# 0.45492 against 0.45679. It was worthless on the club-only corpus and is
# worth ~0.0015 on this one. The objection is unchanged and is not about the
# score: games.home_id is the team USAU lists first, which is the seed, so the
# knob imports USAU's own judgement into a model meant to rate teams from
# results alone and makes every prediction depend on schedule listing order.
# Adopt it only as a deliberate decision to accept that.
PUBLISHED = dict(tau=900.0, involvement_credit=False,
                 involvement_shrink=1.0, stat_transfer_beta=8.0,
                 provisional_shape="hyperbolic",
                 provisional_multiplier=6.0, provisional_games=14,
                 k=48.0, home_advantage=0.0, offseason_regression=0.0,
                 low_info_anchor=0.02,
                 division_scale={"club-men": 260.0, "college": 260.0,
                                 "college-d3": 260.0, "club-mixed": 260.0,
                                 "club-women": 200.0},
                 division_bases={"club-men": 1500.0, "college": 1250.0,
                                 "college-d3": 1250.0, "ufa": 1550.0,
                                 "club-mixed": 1500.0, "club-women": 1600.0})
# Division as a small int, not an initial: "club"[:1] and "college"[:1] are
# both "c", which silently labelled every club event as college. Five codes
# now — a binary flag sent every D-III event back to "club". Codes are
# positional: history.json stores them per event, player_elo.csv packs them
# into a bitmask, and the site's DIVLABEL mirrors them. APPEND, never reorder.
DIVCODE = {"club-men": 0, "college": 1, "college-d3": 2,
           "club-mixed": 3, "club-women": 4}
# Divisions the team tables cover, in display order. College is excluded: a
# college roster is a season-long squad rather than an event entry, so
# "the roster it last played" does not mean the same thing there, and the
# three roster bases the tables are built on would not be comparable.
TEAM_DIVISIONS = ["club-men", "club-mixed", "club-women"]
# A club's best roster must be at least this fraction of the largest squad it
# fielded that season. Picking the max-rated roster with no floor selects the
# SMALLEST one: a mean over an elite subset beats a mean over a full squad, and
# partial entries are common (a men's contingent at a cross-listed mixed event,
# a short-handed one-day round robin). Skeleton Squad's 11-man Mixed Easterns
# entry out-rates its 25-man Boston Invite squad by 85 Elo, which says nothing
# about how strong Skeleton Squad is. 30% of multi-roster clubs peak on a
# below-average-size roster, so the floor is doing real work.
FULL_SQUAD_FRACTION = 0.8


def best_rosters(con, season: int, model, division: str = "club-men"):
    """club key -> player_ids: the club's highest-rated FULL squad.

    "Best version" in the sense of the strongest lineup a club has actually
    listed this season, completed or upcoming, rather than whichever roster
    happens to be most recent. Rosters below FULL_SQUAD_FRACTION of the club's
    own largest are ineligible; see the note above. Returns the same
    (rosters, source, display) triple as latest_rosters.
    """
    rows = con.execute("""
        SELECT COALESCE(et.full_name, et.display_name) AS club,
               ev.name, ev.start_date, et.event_team_id
        FROM event_teams et
        JOIN events ev ON ev.event_id = et.event_id
        WHERE ev.season = ? AND COALESCE(ev.division, 'club-men') = ?
        ORDER BY ev.start_date
    """, (season, division)).fetchall()
    rosters, clubs = load_maps(con)
    cand, display = {}, {}
    for club, evname, sd, etid in rows:
        pids, key = rosters.get(etid), clubs.get(etid)
        if pids and key:
            cand.setdefault(key, []).append((pids, evname, sd))
            display[key] = club
    out, source = {}, {}
    for key, entries in cand.items():
        floor = FULL_SQUAD_FRACTION * max(len(p) for p, _, _ in entries)
        full = [e for e in entries if len(e[0]) >= floor]
        pids, evname, sd = max(full, key=lambda e: model.team_rating(e[0]))
        out[key], source[key] = pids, (evname, sd)
    return out, source, display


# Players below this many games get no stored trajectory. Matches the site's
# display floor: nothing links to a player the table will not show.
HISTORY_MIN_GAMES = 30


def write_history(con, games, rosters, clubs, snaps, game_deltas, model, season):
    """Emit data/history.json — per-event rating trajectories for the drill-down.

    Trajectories are keyed on (subject, event) rather than (subject, game): a
    weekend tournament is one point on the curve, which is how people remember
    a season and which keeps the file to ~3 MB instead of ~12. `snaps` arrives
    already populated by the on_game hook, so these are the SAME numbers the
    CSVs were written from — a second replay could drift from the first if any
    config read changed.

    The games behind each step ride along separately, grouped by event and
    stored once each (see below), which is what the per-event dropdowns open
    onto. That is one flat table for the whole corpus, not one per subject.
    """
    evinfo = {r[0]: r[1:] for r in con.execute(
        "SELECT event_id, name, start_date, season, COALESCE(division,'club-men') "
        "FROM events")}
    keep = {p for p, st in model.players.items()
            if not str(p).startswith("ghost:") and st.games >= HISTORY_MIN_GAMES}
    used = sorted({e for subj, evs in snaps.items() for e in evs
                   if subj[0] == "p" and subj[1] in keep} |
                  {e for subj, evs in snaps.items() for e in evs if subj[0] == "c"})
    ix = {e: i for i, e in enumerate(used)}
    events = [[evinfo[e][1][:10], evinfo[e][0][:46], evinfo[e][2],
               DIVCODE.get(evinfo[e][3], 0)]
              for e in used]

    def encode(evs):
        # Delta-encoded event indices keep the common case to 1-2 digits.
        items = sorted(evs.items(), key=lambda kv: evinfo[kv[0]][1] or "")
        idx, rat, prev = [], [], 0
        for e, payload in items:
            i = ix[e]
            idx.append(i - prev)
            prev = i
            rat.append(payload)
        return [idx, rat]

    # Which club a player turned out for, per event. Stored run-length — a
    # career is a handful of clubs, not one per event — as a flat array of
    # [startIdx, clubIdx] pairs where startIdx counts positions in THIS
    # player's own point list. 18% of points start a run, so this is ~5x
    # smaller than one index per point. clubIdx of -1 means the event-team
    # never resolved to a club identity.
    club_ix = {c: i for i, c in enumerate(sorted(
        {club for (kind, subj), evs in snaps.items() if kind == "p" and subj in keep
         for _r, club in evs.values() if club}))}

    def encode_player(evs):
        items = sorted(evs.items(), key=lambda kv: evinfo[kv[0]][1] or "")
        idx, rat, runs, prev, cur = [], [], [], 0, object()
        for pos, (e, (r, club)) in enumerate(items):
            i = ix[e]
            idx.append(i - prev)
            prev = i
            rat.append(r)
            if club != cur:
                cur = club
                runs += [pos, club_ix.get(club, -1)]
        return [idx, rat, runs]

    players, teams = {}, {}
    for (kind, subj), evs in snaps.items():
        if kind == "p":
            if subj in keep:
                players[str(subj)] = encode_player(evs)
        else:
            teams[subj] = encode({e: [r, n] for e, (r, n) in evs.items()})

    # The team tables key on an event-team's display name; the model keys on the
    # normalized club identity from load_maps. Those differ (and deliberately so
    # — CLUB_ALIASES merges "Rhino" into "Rhino Slam!"), so ship the mapping or
    # every club drill-down comes up empty.
    alias = {}
    for etid, full, dn in con.execute(
            "SELECT event_team_id, full_name, display_name FROM event_teams"):
        key = clubs.get(etid)
        if key and key in teams:
            alias[full or dn] = key

    # Rosters come from a direct query, not from `snaps`: the replay only ever
    # sees a player the club actually played a game with, and what the panel
    # should show is the roster as LISTED — including the guy who never took
    # the field.
    seen = set()
    roster_rows = []
    for eid, etid, pid, name in con.execute("""
            SELECT et.event_id, et.event_team_id, rp.player_id, p.display_name
            FROM roster_players rp JOIN event_teams et USING(event_team_id)
            JOIN players p ON p.player_id = rp.player_id"""):
        i, key = ix.get(eid), clubs.get(etid)
        if i is None or key not in teams:
            continue
        person = (name, str(pid))
        seen.add(person)
        roster_rows.append((f"{key}|{i}", person))
    # The most recent season's roster tab shows the club's BEST reported
    # full-strength squad — the same selection team_elo_best.csv rates off —
    # which may belong to an event not yet played and therefore absent from
    # `used`. Its people must land in the shared index, so collect them
    # before it is built. best_rosters now keys on the model's club key
    # directly, so no name round-trip through `alias` is needed.
    pname = dict(con.execute("SELECT player_id, display_name FROM players"))
    best_ref = {}
    for division in TEAM_DIVISIONS:
        best, bsrc, _display = best_rosters(con, season, model, division)
        for key, pids in best.items():
            if key not in teams:
                continue
            persons = [(pname[p], str(p)) for p in pids if p in pname]
            seen.update(persons)
            evname, sd = bsrc[key]
            best_ref[key] = (evname, sd, persons)
    # Deduped on (name, player_id), never on name alone: the resolver splits an
    # ambiguous name into distinct identities, so two entries legitimately read
    # the same and `peoplePid` has to stay parallel.
    person_ix = {p: i for i, p in enumerate(sorted(seen))}
    people = [p[0] for p in sorted(seen)]
    people_pid = [p[1] for p in sorted(seen)]
    grouped = {}
    for rkey, person in roster_rows:
        grouped.setdefault(rkey, set()).add(person_ix[person])
    rosters_out = {}
    for rkey, members in grouped.items():
        enc, prev = [], 0
        for m in sorted(members):
            enc.append(m - prev)
            prev = m
        rosters_out[rkey] = enc
    best_out = {}
    for key, (evname, sd, persons) in best_ref.items():
        enc, prev = [], 0
        for m in sorted(person_ix[p] for p in persons):
            enc.append(m - prev)
            prev = m
        best_out[key] = [evname, sd, enc]

    # Individual game results, so an event in the drill-down opens onto the
    # games behind it. Grouped by event and stored ONCE per game rather than
    # once per side: a club's row filters its event's list on its own index,
    # and a player's row does the same through the club he turned out for.
    # Per-team-event storage would duplicate all 58,000 games for nothing.
    #
    # These are the games the REPLAY scored — load_games' filtered, date-clamped
    # corpus — so an expanded event is exactly the evidence behind the rating
    # step printed beside it. What USAU lists but the model never saw (forfeits,
    # cancellations, an unseeded bracket slot) is absent by construction.
    #
    # Each row carries what the game did to the two CLUB ratings, captured
    # across play_game in the same replay. Club, not player: the engine scales
    # a game's delta by each man's own provisional multiplier, so a roster does
    # not move as one number — the club's softmax-weighted rating does, and it
    # is the number the panel can state without re-deriving anything.
    club_ord = sorted({c for g in games
                       for c in (clubs.get(g["home_id"]), clubs.get(g["away_id"]))
                       if c})
    club_num = {c: i for i, c in enumerate(club_ord)}
    stages, stage_num = [], {}
    game_rows: dict[int, list] = {}
    for g in games:
        i, hk, ak = (ix.get(g["event_id"]),
                     clubs.get(g["home_id"]), clubs.get(g["away_id"]))
        if i is None or hk is None or ak is None:
            continue
        st = g["stage"] or ""
        if st not in stage_num:
            stage_num[st] = len(stages)
            stages.append(st)
        hd, ad = game_deltas.get((g["event_id"], g["game_key"]), (0, 0))
        game_rows.setdefault(i, []).append([club_num[hk], club_num[ak],
                                            g["home_score"], g["away_score"],
                                            stage_num[st], hd, ad])

    # H.teams is keyed on the LOWERCASED normalized identity ('rhino slam!'),
    # which is not a thing to render. Pick the spelling from the club's most
    # recent event: deterministic, and it gives what they are called now rather
    # than whichever alias sorts first. Opponents are named too, not just
    # subjects with a trajectory — a club the model rated but never snapshotted
    # still turns up across the net in someone else's expanded event.
    latest = {}
    for etid, name, sd in con.execute(
            "SELECT et.event_team_id, COALESCE(et.full_name, et.display_name), "
            "ev.start_date FROM event_teams et JOIN events ev USING(event_id)"):
        key = clubs.get(etid)
        if key not in teams and key not in club_num:
            continue
        sd = sd or ""
        if key not in latest or sd >= latest[key][0]:
            latest[key] = (sd, name)
    team_names = {k: v[1] for k, v in latest.items()}

    out = DB_PATH.parent / "history.json"
    out.write_text(json.dumps({"events": events, "players": players,
                               "teams": teams, "teamKey": alias,
                               "clubNames": list(club_ix),
                               "teamNames": team_names,
                               "rosters": rosters_out, "people": people,
                               "peoplePid": people_pid,
                               "bestRosters": best_out, "bestSeason": season,
                               "gameClubs": club_ord, "gameStages": stages,
                               "games": game_rows},
                              separators=(",", ":")))
    print(f"wrote {out} ({len(players):,} players, {len(teams):,} clubs, "
          f"{len(alias):,} club aliases, {len(events):,} events, "
          f"{len(rosters_out):,} rosters, {len(best_out):,} best rosters, "
          f"{len(people):,} people, {len(team_names):,} club names, "
          f"{sum(len(v) for v in game_rows.values()):,} games, "
          f"{out.stat().st_size/1024/1024:.1f} MB)")


def main(cfg: EloConfig | None = None):
    cfg = cfg or EloConfig(**PUBLISHED)
    con = sqlite3.connect(DB_PATH)
    games = load_games(con)
    rosters, clubs = load_maps(con)
    stat_events = sorted(load_stat_events(con) + load_ufa_stat_events(con),
                         key=lambda e: e[0])

    # Trajectories are captured from the ONE authoritative replay via the hook,
    # keyed (kind, subject) -> event_id -> payload. Last write per event wins,
    # so each point is the rating after that subject's final game of the event.
    etev = dict(con.execute("SELECT event_team_id, event_id FROM event_teams"))
    snaps = defaultdict(dict)
    # The same capture at game grain, club side only: (event, game) -> the two
    # club-rating changes across that game.
    game_deltas = {}

    def capture(g, home, away, model, pre):
        gkey = (g["event_id"], g["game_key"])
        for n, (side, etid) in enumerate(((home, g["home_id"]), (away, g["away_id"]))):
            eid = etev.get(etid)
            if eid is None or not isinstance(side, list):
                continue
            club = clubs.get(etid)
            if club:
                after = model.team_rating(side)
                snaps[("c", club)][eid] = (round(after), len(side))
                if pre is not None:
                    game_deltas.setdefault(gkey, [0, 0])[n] = round(after - pre[n])
            for p in side:
                if not str(p).startswith("ghost:"):
                    snaps[("p", p)][eid] = (round(model.players[p].rating),
                                            club or "")

    _, model = replay("player", games, rosters, clubs, cfg, stat_events,
                      on_game=capture)

    latest = last_appearance(con)
    # Gender-matching group, decided in identity.resolve: 'm' if the identity
    # ever played a men's division, 'w' for the women's division, else the
    # pronoun majority off their mixed roster rows, else '' for unknown. It
    # rides on the player table rather than being re-derived here so the CSV,
    # the site and the DB can never disagree about who is in which bucket.
    gender = dict(con.execute("SELECT player_id, gender FROM players"))
    # Which divisions a player has appeared in, as bitmasks over DIVCODE, so
    # the site can filter the flat table by division. A rating is one number
    # across every division a player has played — a mask says where they
    # turned out, not that they have a separate rating there.
    #
    # TWO masks, because one is a trap. `divisions` is career-wide, and paired
    # with the table's "2026 rosters only" default it answers a question
    # nobody asked: Nathan Champoux last played club men's in 2018 and has
    # been on Hybrid every season since, but a career mask still files him
    # under men's, and every graduated player still shows up under college.
    # 43% of the club men's list was stale that way. `divisions_now` covers
    # only the player's LAST season, which is what "currently in this
    # division" means for a population already filtered to that season.
    div_ever, div_now = defaultdict(int), defaultdict(int)
    for pid, season_played, division in con.execute("""
            SELECT DISTINCT rp.player_id, ev.season,
                   COALESCE(ev.division, 'club-men')
            FROM roster_players rp
            JOIN event_teams et USING (event_team_id)
            JOIN events ev ON ev.event_id = et.event_id"""):
        bit = 1 << DIVCODE.get(division, 0)
        div_ever[pid] |= bit
        if latest.get(pid) and season_played == latest[pid][2]:
            div_now[pid] |= bit
    out = DB_PATH.parent / "player_elo.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        # player_id is exported because display names are NOT unique: ambiguous
        # names are split per-club into separate players, so several rows can
        # read "Julian Kagi" with different ratings. Join on the id, never the name.
        w.writerow(["rank", "player", "player_id", "elo", "sigma", "lo90", "hi90",
                    "games", "last_club", "last_season", "gender", "divisions",
                    "divisions_now"])
        ranked = sorted(
            ((st.rating, pid, st.games) for pid, st in model.players.items()
             if not str(pid).startswith("ghost:") and st.games >= 5),
            reverse=True)
        for i, (rating, pid, ngames) in enumerate(ranked, 1):
            name, club, season = latest.get(pid, ("?", "?", "?"))
            s = rating_sigma(ngames)
            w.writerow([i, name, pid, round(rating, 1), round(s, 1),
                        round(rating - Z90 * s, 1), round(rating + Z90 * s, 1),
                        ngames, club, season, gender.get(pid, ""),
                        div_ever.get(pid, 0), div_now.get(pid, 0)])
    print(f"wrote {out} ({len(ranked)} players with 5+ games)")

    season = con.execute("SELECT max(season) FROM events WHERE has_schedule=1").fetchone()[0]
    for basis, fname in (("completed", "team_elo.csv"),
                         ("upcoming", "team_elo_upcoming.csv"),
                         ("best", "team_elo_best.csv")):
        team_out = DB_PATH.parent / fname
        with open(team_out, "w", newline="") as f:
            w = csv.writer(f)
            # club_key is the model's identity and what the site links on;
            # `club` is the name USAU prints. They differ (aliases, and the
            # suffix that keeps men's and women's Phoenix apart).
            w.writerow(["rank", "club", "club_key", "division", "elo",
                        "roster_size", "season", "roster_event",
                        "roster_event_date"])
            total = 0
            # Ranked WITHIN a division: the divisions never play each other,
            # so one merged 1..n would invite a comparison the games do not
            # support. The site shows one division at a time for the same reason.
            for division in TEAM_DIVISIONS:
                by_club, source, display = (
                    best_rosters(con, season, model, division)
                    if basis == "best"
                    else latest_rosters(con, season, basis, division))
                rated = [(model.team_rating(pids), key, len(pids))
                         for key, pids in by_club.items() if pids]
                for i, (rating, key, size) in enumerate(sorted(rated, reverse=True), 1):
                    evname, sd = source[key]
                    w.writerow([i, display[key], key, division, round(rating, 1),
                                size, season, evname, sd])
                total += len(rated)
        print(f"wrote {team_out} ({total} teams across {len(TEAM_DIVISIONS)} "
              f"divisions, season {season}, {basis} rosters)")
    write_history(con, games, rosters, clubs, snaps, game_deltas, model, season)
    con.close()


if __name__ == "__main__":
    main()
