"""Walk-forward backtest: player-level Elo vs team-level baselines.

Every model sees games in the same chronological order; for each game we
record the pre-game P(home wins) and the outcome, then update. Metrics are
therefore honestly out-of-sample at every point in time.

Models:
  player  — player-level Elo (softmax-weighted team mean, uniform updates)
  reset   — team Elo, team identity = (club, season): full new-year reset
  carry   — team Elo, team identity = club: naive carryover across seasons

Usage: python -m analysis.backtest [--tune]
"""

import math
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from elo.engine import EloConfig, PlayerElo, TeamElo
from identity.resolve import norm_club

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "usau.db"


def _parse_time(t: str | None) -> str:
    if not t:
        return "23:59"
    try:
        return datetime.strptime(t.strip(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return "23:59"


def load_games(con) -> list[dict]:
    rows = con.execute("""
        SELECT g.event_id, g.game_key, g.date, g.time, g.home_id, g.away_id,
               g.home_score, g.away_score, ev.season, ev.start_date, ev.end_date,
               COALESCE(ev.division, 'club'), g.stage
        FROM games g JOIN events ev USING (event_id)
        WHERE g.home_id IS NOT NULL AND g.away_id IS NOT NULL
          AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
          AND NOT (g.home_score = 0 AND g.away_score = 0)
          AND (g.status IS NULL OR g.status IN ('', 'Final'))
          AND g.home_score + g.away_score >= 4
    """).fetchall()
    # status filter drops Cancelled/In Progress rows that carry scores; the
    # score-total floor drops forfeit codes (1-0, 2-0, 2-1) — no real game
    # ends below 4 total points.
    games = []
    for (eid, key, date, time_, hid, aid, hs, as_, season,
         start, end, division, stage) in rows:
        # A date outside its event's window is a source typo — a 2024 regional
        # game stamped 2001-09-23, a 2022 invite stamped 2020-03-05 (year wrong,
        # month/day right). 269 of 30,598 rows. It matters because the replay
        # walks in date order and starts a new season on every change, so one
        # stray stamp reorders the corpus ACROSS seasons: new_season fired 10
        # times for 6 seasons (5 extra regressions toward base) and 34 games
        # replayed before games years older than them. Clamp to the nearest
        # bound: (time, event_id, game_key) still order games within an event,
        # and clamping never moves a game later, so no stat event can reach the
        # model earlier than it did before.
        eff = date or start or f"{season}-01-01"
        if start:
            eff = min(max(eff, start), end or start)
        games.append({
            "sort": (eff, _parse_time(time_), eid, key),
            "date": eff, "season": season, "division": division,
            "event_id": eid, "stage": stage,
            "home_id": hid, "away_id": aid, "home_score": hs, "away_score": as_,
        })
    games.sort(key=lambda g: g["sort"])
    return games


def load_stat_events(con) -> list[tuple]:
    """Per-player stat lines grouped by team-event, sorted by event end date.

    Returns [(end_date, [(player_id, usage_index, quality), ...]), ...] where
    usage_index is the player's share of team G+A+D+T scaled by roster count
    (1.0 = average teammate) and quality is G+A+D-T. Keyed on end_date so
    replay ingests an event's stats only after it has finished.
    """
    rows = con.execute("""
        SELECT re.event_team_id, rp.player_id, ev.end_date,
               re.points, re.assists, re.ds, re.turns
        FROM roster_entries re
        JOIN roster_players rp ON rp.event_team_id = re.event_team_id
             AND rp.name = re.name
        JOIN event_teams et ON et.event_team_id = re.event_team_id
        JOIN events ev ON ev.event_id = et.event_id
        WHERE ev.end_date IS NOT NULL
          AND (re.points != '' OR re.assists != '' OR re.ds != '' OR re.turns != '')
    """).fetchall()

    def num(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0

    by_team: dict[tuple, list] = {}
    for etid, pid, end, p, a, d, t in rows:
        g, a, d, t = num(p), num(a), num(d), num(t)
        by_team.setdefault((end, etid), []).append((pid, g + a + d + t, g + a + d - t))
    events = []
    for (end, _etid), lines in by_team.items():
        total = sum(inv for _, inv, _ in lines)
        if total <= 0 or len(lines) < 2:
            continue
        n = len(lines)
        events.append((end, [(pid, inv * n / total, q) for pid, inv, q in lines]))
    events.sort(key=lambda e: e[0])
    return events


UFA_QUALITY_SCALE = 1 / 3   # a UFA season is roughly three tournaments of exposure


def load_ufa_stat_events(con) -> list[tuple]:
    """UFA season stats as stat events for linked players, dated Sept 1.

    Usage index is true points-played share versus the full UFA team roster
    (unlinked teammates stay in the denominator); quality uses the same
    counting-stat form as USAU (G+A+blocks-throwaways-drops), scaled to
    tournament magnitude. Sept 1 dating keeps ingestion after the UFA season
    ends, so nothing leaks into predicting that summer's club games.
    """
    from ufa.link import resolve_links
    try:
        links = resolve_links(con)
        rows = con.execute("""
            SELECT s.player_id, p.team_id, s.year,
                   COALESCE(s.opointsplayed,0) + COALESCE(s.dpointsplayed,0),
                   COALESCE(s.goals,0) + COALESCE(s.assists,0)
                   + COALESCE(s.blocks,0) - COALESCE(s.throwaways,0)
                   - COALESCE(s.drops,0)
            FROM ufa_player_stats s
            JOIN ufa_players p ON p.player_id = s.player_id AND p.year = s.year
        """).fetchall()
    except sqlite3.OperationalError:     # DB predates the UFA tables
        return []
    if not links:
        return []
    by_team: dict[tuple, list] = {}
    for upid, team, year, pp, q in rows:
        by_team.setdefault((year, team), []).append((upid, pp, q))
    events = []
    for (year, _team), lines in by_team.items():
        total = sum(pp for _, pp, _ in lines)
        n = len(lines)
        if total <= 0 or n < 2:
            continue
        entries = [(links[upid], pp * n / total, q * UFA_QUALITY_SCALE)
                   for upid, pp, q in lines if upid in links]
        if len(entries) >= 2:
            events.append((f"{year}-09-01", entries))
    events.sort(key=lambda e: e[0])
    return events


def load_ufa_games(con) -> tuple[list, dict, dict]:
    """UFA games as replay entries, with per-game rosters of linked players.

    Returns (games, rosters_add, clubs_add). Rosters are the players who
    actually took the field (points played > 0) mapped through accepted
    links; a team fielding fewer than 7 linked players gets no roster entry,
    so replay's existing ghost fallback absorbs it (the game still
    calibrates the linked opponents).
    """
    from ufa.link import resolve_links
    try:
        links = resolve_links(con)
        game_rows = con.execute("""
            SELECT game_id, year, date, home_team_id, away_team_id,
                   home_score, away_score
            FROM ufa_games
            WHERE status = 'Final'
              AND home_score IS NOT NULL AND away_score IS NOT NULL""").fetchall()
        stat_rows = con.execute("""
            SELECT game_id, team_id, player_id FROM ufa_game_stats
            WHERE COALESCE(o_points_played,0) + COALESCE(d_points_played,0) > 0
        """).fetchall()
    except sqlite3.OperationalError:     # DB predates the UFA tables
        return [], {}, {}
    fielded: dict[tuple, list] = {}
    for gid, team, upid in stat_rows:
        if upid in links:
            fielded.setdefault((gid, team), []).append(links[upid])
    games, rosters_add, clubs_add = [], {}, {}
    for gid, year, date, home, away, hs, as_ in game_rows:
        hkey, akey = f"ufa:{gid}:{home}", f"ufa:{gid}:{away}"
        for key, team in ((hkey, home), (akey, away)):
            clubs_add[key] = f"ufa-{team}"
            pids = fielded.get((gid, team), [])
            if len(pids) >= 7:
                rosters_add[key] = pids
        games.append({
            "sort": (date or f"{year}-06-01", "12:00", 0, gid),
            "date": date, "season": year, "division": "ufa",
            "home_id": hkey, "away_id": akey,
            "home_score": hs, "away_score": as_,
        })
    return games, rosters_add, clubs_add


def load_maps(con):
    """(rosters, clubs): event_team_id -> [player_id] and -> club key."""
    rosters: dict[str, list[int]] = {}
    for etid, pid in con.execute("SELECT event_team_id, player_id FROM roster_players"):
        rosters.setdefault(etid, []).append(pid)
    clubs = {
        etid: norm_club(full, disp)
        for etid, full, disp in con.execute(
            "SELECT event_team_id, full_name, display_name FROM event_teams")
    }
    return rosters, clubs


def replay(model_kind: str, games, rosters, clubs, cfg: EloConfig,
           stat_events=None, on_game=None):
    """Returns records of (season, expected, outcome) and the final model.

    stat_events (from load_stat_events) are ingested strictly walk-forward:
    a team-event's stat lines reach the model only once the replay passes the
    event's end date, so they never inform predictions of that same event.

    on_game(game, home_roster, away_roster, model) fires after each game is
    applied. It exists so a caller can record rating trajectories from the one
    authoritative pass rather than monkeypatching the engine or replaying a
    second time and hoping the two agree.
    """
    if model_kind == "player":
        model = PlayerElo(cfg)
    else:
        model = TeamElo(cfg)
    stats = stat_events if (stat_events and model_kind == "player") else []
    si = 0
    records = []
    max_season = None

    for g in games:
        gdate = g.get("date") or g["sort"][0]
        while si < len(stats) and stats[si][0] < gdate:
            model.observe_stats(stats[si][1])
            si += 1
        # Regress on ADVANCE only. Keyed on "season changed", a corpus that
        # revisits an earlier season re-fires the offseason regression and
        # decays everyone toward base again; load_games clamps the stray dates
        # that caused that, and this keeps the invariant local to the loop.
        if max_season is not None and g["season"] > max_season:
            if model_kind != "reset":       # reset gets fresh entities anyway
                for _ in range(g["season"] - max_season):
                    model.new_season()
        max_season = g["season"] if max_season is None else max(max_season, g["season"])

        season = g["season"]
        division = g.get("division", "club")
        if model_kind == "player":
            home = rosters.get(g["home_id"]) or [f"ghost:{clubs.get(g['home_id'])}:{season}"]
            away = rosters.get(g["away_id"]) or [f"ghost:{clubs.get(g['away_id'])}:{season}"]
        elif model_kind == "reset":
            home = (clubs.get(g["home_id"], g["home_id"]), season)
            away = (clubs.get(g["away_id"], g["away_id"]), season)
        else:  # carry
            home = clubs.get(g["home_id"], g["home_id"])
            away = clubs.get(g["away_id"], g["away_id"])

        exp = model.play_game(home, away, g["home_score"], g["away_score"], division)
        outcome = (1.0 if g["home_score"] > g["away_score"]
                   else 0.0 if g["home_score"] < g["away_score"] else 0.5)
        records.append((season, division, g.get("date"), exp, outcome))
        if on_game is not None:
            on_game(g, home, away, model)
    return records, model


def metrics(records, seasons=None, divisions=None, max_month=None):
    """max_month filters on the game's calendar month (e.g. 7 = through July),
    isolating early-season games where cross-division carryover matters most."""
    rs = [(e, o) for s, d, date, e, o in records
          if (seasons is None or s in seasons)
          and (divisions is None or d in divisions)
          and (max_month is None or (date and int(date[5:7]) <= max_month))]
    n = len(rs)
    if n == 0:
        return {"n": 0}
    eps = 1e-12
    acc = sum(1 for e, o in rs if (e - 0.5) * (o - 0.5) > 0) / n
    brier = sum((e - o) ** 2 for e, o in rs) / n
    logloss = -sum(o * math.log(max(e, eps)) + (1 - o) * math.log(max(1 - e, eps))
                   for e, o in rs) / n
    return {"n": n, "accuracy": acc, "brier": brier, "logloss": logloss}


def compare(cfg: EloConfig | None = None,
            eval_seasons=(2024, 2025),
            eval_divisions=("club",)):
    """Headline eval is on CLUB games: the question is whether adding college
    signal improves club prediction, so college games train but don't score.

    The corpus splits THREE ways, not two:
        FIT  2017-2021   ratings accumulate; nothing is chosen here
        VAL  2022-2023   hyperparameters are selected here
        TEST 2024-2025   this default — reported, never selected on
    eval_seasons defaults to TEST so the CLI reports the honest number. Pass
    (2022, 2023) to see the selection set. The old default spanned 2022-2025,
    which mixed the two and flattered every result by half. See the protocol
    note above PUBLISHED in analysis/rankings.py for why FIT cannot be used to
    choose: it is the cold-start era, and it mis-selected three parameters.
    2026 is excluded, being half-played. 2020 does not exist — COVID cancelled
    the club series.

    cfg defaults to analysis.rankings.PUBLISHED, not to EloConfig()'s bare
    defaults. It used to default to the latter, so this table silently scored a
    config nobody publishes (home_advantage=0, no division_scale), which is why
    its numbers never matched the ones quoted for the published rankings.
    """
    con = sqlite3.connect(DB_PATH)
    games = load_games(con)
    rosters, clubs = load_maps(con)
    stat_events = load_stat_events(con)
    ufa_events = load_ufa_stat_events(con)
    combined = sorted(stat_events + ufa_events, key=lambda e: e[0])
    ufa_games, r_add, c_add = load_ufa_games(con)
    all_games = sorted(games + ufa_games, key=lambda g: g["sort"])
    rosters_ufa = {**rosters, **r_add}
    clubs_ufa = {**clubs, **c_add}
    if cfg is None:
        from analysis.rankings import PUBLISHED
        cfg = EloConfig(**PUBLISHED)
    n_col = sum(1 for g in games if g["division"] == "college")
    print(f"{len(games)} scored games ({n_col} college) + {len(ufa_games)} UFA, "
          f"{len(stat_events)} stat team-events + {len(ufa_events)} UFA; "
          f"eval seasons {eval_seasons}, divisions {eval_divisions}")
    header = f"{'model':<26}{'n':>7}{'accuracy':>10}{'brier':>8}{'logloss':>9}"
    print(header + "\n" + "-" * len(header))
    results = {}
    plain = EloConfig(**{**cfg.__dict__, "tau": math.inf})
    both = EloConfig(**{**plain.__dict__, "involvement_credit": True,
                        "stat_transfer_beta": 1.0})
    base = (games, rosters, clubs)
    withufa = (all_games, rosters_ufa, clubs_ufa)
    variants = [
        ("player (weighted)", "player", cfg, stat_events, base),
        ("player (plain mean)", "player", plain, stat_events, base),
        ("plain + inv credit", "player",
         EloConfig(**{**plain.__dict__, "involvement_credit": True}),
         stat_events, base),
        ("plain + stat transfer", "player",
         EloConfig(**{**plain.__dict__, "stat_transfer_beta": 1.0}),
         stat_events, base),
        ("plain + both", "player", both, stat_events, base),
        ("plain + both + UFA", "player", both, combined, base),
        ("plain + both + UFA + games", "player", both, combined, withufa),
        ("team reset", "reset", cfg, stat_events, base),
        ("team carryover", "carry", cfg, stat_events, base),
    ]
    slice_records = {}
    for label, kind, c, events, (gg, rr, cc) in variants:
        records, _ = replay(kind, gg, rr, cc, c, events)
        m = metrics(records, set(eval_seasons), set(eval_divisions))
        results[label] = m
        if kind == "player":
            slice_records[label] = records
        print(f"{label:<26}{m['n']:>7}{m['accuracy']:>10.4f}{m['brier']:>8.4f}{m['logloss']:>9.4f}")

    # Slices for the player models: early-season club games are where the
    # college bridge carries the prediction; college games sanity-check that
    # the shared pool isn't bought at the other division's expense.
    slices = [
        ("club thru July", set(eval_seasons), {"club"}, 7),
        ("college", set(eval_seasons), {"college"}, None),
    ]
    print("\nslices (player models):")
    for label, records in slice_records.items():
        for sname, ss, dd, mm in slices:
            m = metrics(records, ss, dd, max_month=mm)
            if m["n"]:
                print(f"  {label:<24} {sname:<15}{m['n']:>6}"
                      f"{m['accuracy']:>10.4f}{m['brier']:>8.4f}{m['logloss']:>9.4f}")
    con.close()
    return results


def tune(train_seasons=(2021, 2022, 2023), divisions=("club",)):
    """Grid search for the player model, scored on club games of the train
    seasons. Adds the unified-model knobs (college base, rookie discount) to
    the previously-tuned k/tau; offseason regression stays 0 (won before)."""
    con = sqlite3.connect(DB_PATH)
    games = load_games(con)
    rosters, clubs = load_maps(con)
    con.close()
    best = None
    for tau in (100, 150, 300):
        for k in (40, 60, 80):
            for college_base in (1200.0, 1300.0, 1400.0):
                for delta in (0.0, 50.0, 100.0):
                    cfg = EloConfig(
                        tau=tau, k=k, offseason_regression=0.0,
                        rookie_discount=delta,
                        division_bases={"club": 1500.0, "college": college_base})
                    records, _ = replay("player", games, rosters, clubs, cfg)
                    m = metrics(records, set(train_seasons), set(divisions))
                    key = m.get("logloss", float("inf"))
                    marker = ""
                    if best is None or key < best[0]:
                        best = (key, cfg)
                        marker = "  <-- best"
                    print(f"tau={tau:<4} k={k:<3} col_base={college_base:<7} "
                          f"delta={delta:<5} logloss={key:.4f} "
                          f"acc={m.get('accuracy', 0):.4f}{marker}", flush=True)
    print("\nbest config:", best[1])
    return best[1]


if __name__ == "__main__":
    if "--tune" in sys.argv:
        best_cfg = tune()
        compare(best_cfg)
    else:
        compare()
