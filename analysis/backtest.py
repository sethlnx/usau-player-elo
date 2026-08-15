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
               COALESCE(ev.division, 'club-men'), g.stage
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
            "event_id": eid, "game_key": key, "stage": stage,
            "home_id": hid, "away_id": aid, "home_score": hs, "away_score": as_,
        })
    games.sort(key=lambda g: g["sort"])
    return games


def load_stat_events(con) -> list[tuple]:
    """Per-player stat lines grouped by team-event, sorted by event end date.

    Returns [(end_date, [(player_id, usage_index, quality), ...], event_team_id)]
    where usage_index is the player's share of team G+A+D+T scaled by roster
    count (1.0 = average teammate) and quality is G+A+D-T. Keyed on end_date so
    replay ingests an event's stats only after it has finished.

    The event_team_id rides along because ingestion is deferred for leakage
    safety, NOT because the rating movement happens later: a caller recording
    trajectories has to book the transfer against the event that earned it.
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
    for (end, etid), lines in by_team.items():
        total = sum(inv for _, inv, _ in lines)
        if total <= 0 or len(lines) < 2:
            continue
        n = len(lines)
        events.append((end, [(pid, inv * n / total, q) for pid, inv, q in lines],
                       etid))
    events.sort(key=lambda e: e[0])
    return events


UFA_QUALITY_SCALE = 1 / 3   # a UFA season is roughly three tournaments of exposure


def _team_zscores(values: list[float | None]) -> list[float]:
    """Within-team z-scores; missing or constant features contribute zero."""
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return [0.0] * len(values)
    mean = sum(present) / len(present)
    variance = sum((v - mean) ** 2 for v in present) / len(present)
    if variance <= 0.0:
        return [0.0] * len(values)
    scale = variance ** 0.5
    return [0.0 if v is None else (v - mean) / scale for v in values]


def load_ufa_stat_data(con) -> tuple[dict, list[tuple]]:
    """Load linked UFA stat rows once for repeated coefficient evaluations."""
    from ufa.link import resolve_links
    try:
        links = resolve_links(con)
        rows = con.execute("""
            SELECT s.player_id, p.team_id, s.year,
                   COALESCE(s.opointsplayed,0) + COALESCE(s.dpointsplayed,0),
                   s.completions, s.throwattempts, s.yardsthrown,
                   s.yardsreceived, s.hockeyassists,
                   COALESCE(s.goals,0) + COALESCE(s.assists,0)
                   + COALESCE(s.blocks,0) - COALESCE(s.throwaways,0)
                   - COALESCE(s.drops,0)
            FROM ufa_player_stats s
            JOIN ufa_players p ON p.player_id = s.player_id AND p.year = s.year
        """).fetchall()
    except sqlite3.OperationalError:     # DB predates the UFA tables
        return {}, []
    return links, rows


def build_ufa_stat_events(
    data: tuple[dict, list[tuple]], cfg: EloConfig | None = None,
) -> list[tuple]:
    """Build stable stat-event tuples from cached UFA rows and a config."""
    links, rows = data
    cfg = cfg or EloConfig()
    if not links:
        return []
    by_team: dict[tuple, list] = {}
    for (upid, team, year, pp, completions, attempts, ty, ry, ha, q) in rows:
        by_team.setdefault((year, team), []).append(
            (upid, pp, completions, attempts, ty, ry, ha, q)
        )
    events = []
    for (year, _team), lines in by_team.items():
        total_pp = sum(pp for _, pp, *_ in lines)
        n = len(lines)
        if total_pp <= 0 or n < 2:
            continue
        completion_values = [c for _, _, c, *_ in lines]
        total_completions = sum(
            c for c in completion_values if c is not None
        )
        completion_index = (
            [
                c * n / total_completions if c is not None else 1.0
                for c in completion_values
            ]
            if total_completions > 0 else [0.0] * n
        )
        usage = [
            pp * n / total_pp
            + cfg.ufa_completion_usage_weight * ci
            for (_, pp, *_), ci in zip(lines, completion_index)
        ]
        pct = [
            c / a if c is not None and a is not None and a > 0 else None
            for _, _, c, a, *_ in lines
        ]
        pct_z = _team_zscores(pct)
        ty_z = _team_zscores([
            line[4] if line[4] is not None else None for line in lines
        ])
        ry_z = _team_zscores([
            line[5] if line[5] is not None else None for line in lines
        ])
        ha_z = _team_zscores([
            line[6] if line[6] is not None else None for line in lines
        ])
        entries = []
        for i, (upid, _pp, _c, _a, _ty, _ry, _ha, q) in enumerate(lines):
            quality = UFA_QUALITY_SCALE * (
                q
                + cfg.ufa_completion_pct_weight * pct_z[i]
                + cfg.ufa_throwing_yards_weight * ty_z[i]
                + cfg.ufa_receiving_yards_weight * ry_z[i]
                + cfg.ufa_hockey_assists_weight * ha_z[i]
            )
            if upid in links:
                entries.append((links[upid], usage[i], quality))
        if len(entries) >= 2:
            events.append((f"{year}-09-01", entries, None))
    events.sort(key=lambda e: e[0])
    return events


def load_ufa_stat_events(con, cfg: EloConfig | None = None) -> list[tuple]:
    """UFA season stats as stat events for linked players, dated Sept 1.

    The returned tuple remains ``(date, [(pid, usage, quality), ...], etid)``.
    Existing points-played and G/A/BLK/T/D values are unchanged when the new
    UFA feature weights are zero. New features are normalized within each UFA
    team-season, so no future or cross-team statistics are used to choose a
    scale. Missing or constant features are neutral.
    """
    return build_ufa_stat_events(load_ufa_stat_data(con), cfg)


def load_ufa_games(con) -> tuple[list, dict, dict]:
    """UFA games as replay entries, with per-game rosters of linked players.

    Every final game is a one-game synthetic event so the authoritative replay
    can emit a team Elo point and the underlying score after each game. Rosters
    are players who took the field (points played > 0), mapped through accepted
    links. A team with fewer than seven linked players gets no roster entry, so
    replay's existing ghost fallback absorbs it while preserving the result.
    """
    from ufa.link import resolve_links
    try:
        links = resolve_links(con)
        game_rows = con.execute("""
            SELECT g.game_id, g.year, g.date, g.home_team_id, g.away_team_id,
                   g.home_score, g.away_score, g.week,
                   COALESCE(ht.full_name, g.home_team_id),
                   COALESCE(at.full_name, g.away_team_id)
            FROM ufa_games g
            LEFT JOIN ufa_teams ht
              ON ht.team_id = g.home_team_id AND ht.year = g.year
            LEFT JOIN ufa_teams at
              ON at.team_id = g.away_team_id AND at.year = g.year
            WHERE g.status = 'Final'
              AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
              AND NOT (g.home_score = 0 AND g.away_score = 0)
              AND g.home_score + g.away_score >= 4
        """).fetchall()
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
    for gid, year, date, home, away, hs, as_, week, home_name, away_name in game_rows:
        event_id = f"ufa:{gid}"
        hkey, akey = f"{event_id}:{home}", f"{event_id}:{away}"
        for key, team in ((hkey, home), (akey, away)):
            clubs_add[key] = f"ufa:{team}"
            pids = sorted(set(fielded.get((gid, team), [])))
            if len(pids) >= 7:
                rosters_add[key] = pids
        games.append({
            "sort": (date or f"{year}-06-01", "12:00", 0, gid),
            "date": date, "season": year, "division": "ufa",
            "event_id": event_id, "game_key": gid, "stage": week or "",
            "event_name": f"{away_name} at {home_name}",
            "home_name": home_name, "away_name": away_name,
            "home_id": hkey, "away_id": akey,
            "home_score": hs, "away_score": as_,
        })
    games.sort(key=lambda game: game["sort"])
    return games, rosters_add, clubs_add


# A club identity is its normalized name — deliberately, so "Rhino" and
# "Rhino Slam!" are one club and a college program's D-I and D-III sides are
# one program. Across GENDER divisions that breaks down: men's Phoenix and
# women's Phoenix are two teams from one org, and 73 club names (5 active in
# 2026) collide that way. Mixed and women's keys therefore carry a suffix.
# The men's group keeps the bare name so every existing key — history.json,
# CLUB_ALIASES, the U.S. Open tracker — is byte-identical to before.
CLUB_SUFFIX = {"club-mixed": " (mixed)", "club-women": " (women's)",
               # New GraphQL-only levels: a high school "Warriors" and a
               # college "Warriors" are unrelated programs, so every new
               # division gets its own suffix rather than risking a
               # coincidental name collision in the club-identity map (used
               # by the TeamElo reset/carry baselines and the display
               # tables — the player-level PUBLISHED model does not consult
               # this map for its predictions).
               "college-mixed": " (college mixed)",
               "hs-boys": " (HS)", "hs-girls": " (HS girls)",
               "hs-mixed": " (HS mixed)",
               "ms-boys": " (MS)", "ms-girls": " (MS girls)",
               "ms-mixed": " (MS mixed)",
               "ycc-u20-boys": " (YCC-U20)", "ycc-u20-girls": " (YCC-U20 girls)",
               "ycc-u20-mixed": " (YCC-U20 mixed)",
               "ycc-u17-boys": " (YCC-U17)", "ycc-u17-girls": " (YCC-U17 girls)",
               "ycc-u17-mixed": " (YCC-U17 mixed)",
               "ycc-u15-boys": " (YCC-U15)", "ycc-u15-girls": " (YCC-U15 girls)",
               "ycc-u15-mixed": " (YCC-U15 mixed)",
               "beach-men": " (beach)", "beach-women": " (beach women's)",
               "beach-mixed": " (beach mixed)",
               "beach-masters-men": " (beach masters)",
               "beach-masters-women": " (beach masters women's)",
               "beach-masters-mixed": " (beach masters mixed)",
               "beach-grandmasters-men": " (beach grandmasters)",
               "beach-grandmasters-women": " (beach grandmasters women's)",
               "beach-grandmasters-mixed": " (beach grandmasters mixed)",
               "beach-greatgrandmasters-men": " (beach GGM)",
               "beach-greatgrandmasters-women": " (beach GGM women's)",
               "beach-greatgrandmasters-mixed": " (beach GGM mixed)",
               "beach-legends-mixed": " (beach legends)",
               "league-men": " (league)", "league-mixed": " (league mixed)"}


def load_maps(con):
    """(rosters, clubs): event_team_id -> [player_id] and -> club key."""
    rosters: dict[str, list[int]] = {}
    for etid, pid in con.execute("SELECT event_team_id, player_id FROM roster_players"):
        rosters.setdefault(etid, []).append(pid)
    clubs = {
        etid: norm_club(full, disp) + CLUB_SUFFIX.get(division, "")
        for etid, full, disp, division in con.execute(
            """SELECT et.event_team_id, et.full_name, et.display_name,
                      COALESCE(ev.division, 'club-men')
               FROM event_teams et JOIN events ev ON ev.event_id = et.event_id""")
    }
    return rosters, clubs


def replay(model_kind: str, games, rosters, clubs, cfg: EloConfig,
           stat_events=None, on_game=None, on_stats=None):
    """Returns records of (season, expected, outcome) and the final model.

    stat_events (from load_stat_events) are ingested strictly walk-forward:
    a team-event's stat lines reach the model only once the replay passes the
    event's end date, so they never inform predictions of that same event.

    on_game(game, home_roster, away_roster, model, pre) fires after each game is
    applied, `pre` being the two team ratings from just before it. It exists so
    a caller can record rating trajectories from the one authoritative pass
    rather than monkeypatching the engine or replaying a second time and hoping
    the two agree.

    on_stats(end_date, entries, event_team_id, model) fires after each stat
    team-event is folded in. Deferred ingestion is a leakage guard, not a
    claim that the movement happens later — a trajectory caller needs this to
    book the transfer against the event_team_id that earned it, not against
    whatever game happens to be replaying when the walk-forward drain fires.
    event_team_id is None for stat events with no rated event of their own
    (e.g. a UFA season).
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
            end, entries, etid = stats[si]
            model.observe_stats(entries)
            if on_stats is not None:
                on_stats(end, entries, etid, model)
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
        division = g.get("division", "club-men")
        if model_kind == "player":
            home = rosters.get(g["home_id"]) or [f"ghost:{clubs.get(g['home_id'])}:{season}"]
            away = rosters.get(g["away_id"]) or [f"ghost:{clubs.get(g['away_id'])}:{season}"]
        elif model_kind == "reset":
            home = (clubs.get(g["home_id"], g["home_id"]), season)
            away = (clubs.get(g["away_id"], g["away_id"]), season)
        else:  # carry
            home = clubs.get(g["home_id"], g["home_id"])
            away = clubs.get(g["away_id"], g["away_id"])
        if model_kind == "player" and cfg.inactivity_decay > 0:
            model.age_players([home, away], division, gdate)


        # The hook wants the rating change across this game. pregame_ratings,
        # not team_rating: reading a rating before play_game has materialized
        # the rosters creates debutants at the global base instead of their
        # division's, which silently shifts the whole replay. Only paid for
        # when someone asked for a hook.
        pre = (model.pregame_ratings(home, away, division)
               if on_game is not None and isinstance(home, list) else None)
        if model_kind == "player":
            exp = model.play_game(
                home, away, g["home_score"], g["away_score"], division,
                clubs.get(g["home_id"], g["home_id"]),
                clubs.get(g["away_id"], g["away_id"]),
            )
        else:
            exp = model.play_game(
                home, away, g["home_score"], g["away_score"], division,
            )
        outcome = (1.0 if g["home_score"] > g["away_score"]
                   else 0.0 if g["home_score"] < g["away_score"] else 0.5)
        records.append((season, division, g.get("date"), exp, outcome))
        if on_game is not None:
            on_game(g, home, away, model, pre)
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
            eval_divisions=("club-men",)):
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
    if cfg is None:
        from analysis.rankings import PUBLISHED
        cfg = EloConfig(**PUBLISHED)
    from womens_pro.ratings import load_womens_pro_inputs

    con = sqlite3.connect(DB_PATH)
    usau_games = load_games(con)
    rosters, clubs = load_maps(con)
    womens_pro = load_womens_pro_inputs(con)
    games = sorted(usau_games + womens_pro.games, key=lambda g: g["sort"])
    rosters.update(womens_pro.rosters)
    clubs.update(womens_pro.clubs)
    stat_events = load_stat_events(con)
    ufa_events = load_ufa_stat_events(con, cfg)
    combined = sorted(stat_events + ufa_events, key=lambda e: e[0])
    ufa_games, r_add, c_add = load_ufa_games(con)
    all_games = sorted(games + ufa_games, key=lambda g: g["sort"])
    rosters_ufa = {**rosters, **r_add}
    clubs_ufa = {**clubs, **c_add}
    # cfg is resolved before UFA stat-event construction so coefficient
    # sweeps and published replays consume the same feature mapping.
    n_col = sum(1 for g in usau_games if g["division"] == "college")
    print(f"{len(usau_games)} USAU games ({n_col} college) + "
          f"{len(womens_pro.games)} PUL/WUL + {len(ufa_games)} UFA, "
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
        ("club men's thru July", set(eval_seasons), {"club-men"}, 7),
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


def tune(*_a, **_kw):
    """Retired. Use analysis.descent.

    This was a 4-axis grid over tau/k/college_base/rookie_discount scored on
    2021-2023 club games. Every part of that is now wrong: the split is
    FIT/VAL/TEST rather than train-on-the-eval-seasons, selection runs over
    all five divisions rather than club men's, and the axes it did not cover
    (the provisional window above all) turn out to matter far more than the
    ones it did.
    """
    raise NotImplementedError(
        "analysis.backtest.tune is retired — run `python -m analysis.descent` "
        "(21 axes, FIT/VAL/TEST, pruned on paired VAL cost)")


if __name__ == "__main__":
    if "--tune" in sys.argv:
        sys.exit("run `python -m analysis.descent` instead")
    compare()
