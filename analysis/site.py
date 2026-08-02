"""Build a single self-contained HTML page for the published rankings.

Five tabs: club rankings, player rankings, per-season Trends, a Tournaments
browser showing every event's recovered pools and bracket plus the history of
the series it belongs to, and a U.S. Open tracker whose bracket you fill in as
games finish, re-simulating the title odds live.

Everything is embedded in one file so it opens over file:// with no server
and no network. Inputs are the published artifacts only - this script never
replays the model, so the page can never disagree with the CSVs:

    data/player_elo.csv          player table (>= MIN_GAMES shown)
    data/team_elo.csv            clubs, most recent COMPLETED event roster
    data/team_elo_best.csv       clubs, best full-strength roster of 2026
    data/team_elo_upcoming.csv   clubs, next event roster - the U.S. Open field
    data/usau.db                 every event's schedule - the U.S. Open
                                 tracker, and the Tournaments browser

Usage: python -m analysis.site   ->   docs/index.html
"""

import csv
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH
from analysis.rankings import DIVCODE, PUBLISHED, TEAM_DIVISIONS
from analysis.tournaments import build as build_tournaments

# docs/ rather than site/: GitHub Pages can serve a branch's root or its
# /docs folder and nothing else, so putting the page here makes the project
# URL itself the app. The accompanying .nojekyll stops Pages running the
# output through Jekyll, which is pure overhead for a single static file.
OUT = DB_PATH.parent.parent / "docs" / "index.html"

# The player table's display floor, matching the ranking convention: below 30
# games a rating still sits inside the engine's provisional window.
MIN_GAMES = 30

# The U.S. Open field is 12 teams in 4 pools of 3, then a bracket. USAU does
# not label the pools anywhere in the data — every opening-round fixture sits
# in one table headed "Pool D" — so they are recovered from the fixtures
# themselves: a pool is a set of teams that have all played each other. The two
# same-stage games that cross two of those sets are the winners' crossover,
# which seeds the quarters and must stay out of the pool standings. Deriving
# pools from the co-play graph instead (what this did before day one) collapses
# the field into two components of six the moment the crossovers are seeded.
USOPEN_EVENT = "%U.S. Open%"

# Bracket columns in playing order. The placement columns USAU also publishes
# (Third Place, Fifth Semifinals, Fifth Place, Seventh Place) are dropped:
# nothing in them can reach the title, which is what the tracker prices.
BRACKET_ROUNDS = ["Prequarterfinals", "Quarterfinals", "Semifinals", "Final"]


def load_csv(name):
    p = DB_PATH.parent / name
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def _slot_order(game):
    """Sort key: USAU's own fixture numbering, which runs in playing order —
    pool rows, then the bracket column by column."""
    digits = re.sub(r"\D", "", game["slot"] or "")
    return int(digits) if digits else 0


def usopen(con):
    """(event row, [team names], [game dicts]) for the men's ICC.

    Teamless fixtures are kept (LEFT JOIN): a semifinal nobody has qualified
    for is still a slot the bracket has to draw. `slot` distinguishes a pool
    row (the page's numeric row id) from a bracket game ("game411460").
    """
    ev = con.execute(
        """SELECT event_id, name, start_date, end_date FROM events
           WHERE name LIKE ? AND season=2026 AND COALESCE(division,'club-men')='club-men'""",
        (USOPEN_EVENT,)).fetchone()
    if not ev:
        return None, [], []
    rows = con.execute(
        """SELECT g.slot, g.stage, g.date, g.time, h.display_name, a.display_name,
                  g.home_score, g.away_score, g.status
           FROM games g
           LEFT JOIN event_teams h ON h.event_team_id=g.home_id
           LEFT JOIN event_teams a ON a.event_team_id=g.away_id
           WHERE g.event_id=?""", (ev[0],)).fetchall()
    games = [{"slot": slot, "stage": stage, "date": d, "time": t,
              "home": home, "away": away, "hs": hs, "as": as_,
              "done": status == "Final" and (hs or 0) + (as_ or 0) > 0}
             for slot, stage, d, t, home, away, hs, as_, status in rows]
    games.sort(key=_slot_order)
    teams = [r[0] for r in con.execute(
        """SELECT COALESCE(full_name, display_name) FROM event_teams
           WHERE event_id=? ORDER BY 1""", (ev[0],))]
    return ev, teams, games


def pool_round(games):
    """(pools, crossovers) for the opening round robin.

    A pool is a maximal set of teams that have all played each other, grown one
    fixture at a time in slot order — so the round robin has built the pools by
    the time the crossovers arrive, and a game joining two finished pools is a
    crossover rather than evidence that they are one pool. Each intra-pool game
    gets a `pool` index for the standings.

    A later placement round robin (USAU files the 9-12 pool as "Pool E") fails
    the same test, so crossovers are held to the stage that built the pools.
    """
    rows = [g for g in games
            if (g["slot"] or "").isdigit() and g["home"] and g["away"]]
    pairs = {frozenset((g["home"], g["away"])) for g in rows}
    pools, where, cross = [], {}, []
    for g in rows:
        h, a = g["home"], g["away"]
        ph, pa = where.get(h), where.get(a)
        if ph is not None and ph == pa:
            g["pool"] = ph                      # intra-pool: counts to standings
        elif ph is None and pa is None:
            where[h] = where[a] = g["pool"] = len(pools)
            pools.append([h, a])
        elif ph is None or pa is None:
            joiner, idx = (h, pa) if ph is None else (a, ph)
            if all(frozenset((joiner, t)) in pairs for t in pools[idx]):
                where[joiner] = g["pool"] = idx
                pools[idx].append(joiner)
            else:
                cross.append(g)
        else:
            cross.append(g)
    stage = rows[0]["stage"] if rows else None
    return pools, [g for g in cross if g["stage"] == stage]


def build():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ev, field, sched = usopen(con)
    # Every event's recovered shape, for the Tournaments tab. Derived here
    # rather than replayed: analysis.tournaments reads the same schedule the
    # tracker does and infers pools and brackets from the results.
    tourneys = build_tournaments(con)
    con.close()

    all_players = load_csv("player_elo.csv")
    total_rated = len(all_players)
    players = [r for r in all_players if int(r["games"]) >= MIN_GAMES]
    clubs = {
        "completed": load_csv("team_elo.csv"),
        "best": load_csv("team_elo_best.csv"),
        "upcoming": load_csv("team_elo_upcoming.csv"),
    }

    # Ratings for the U.S. Open field come from the UPCOMING table, which rates
    # each club off the roster it registered for this event - not off whatever
    # it last played. That distinction is worth ~180 Elo for Truck Stop, who
    # fielded a B-squad at Pro Elite Challenge East.
    upcoming = {r["club"]: float(r["elo"]) for r in clubs["upcoming"]}
    ratings = {t: upcoming.get(t) for t in field}

    # Pool letters are cosmetic — USAU publishes none — so label them by
    # strength: A holds the strongest team, and each pool sorts strongest
    # first. They no longer decide anything. The bracket used to be guessed
    # from these letters ("2nd of one pool plays 3rd of another"); it is now
    # read from USAU's published slots, which day one has filled in.
    raw_pools, crossovers = pool_round(sched)
    rank_of = sorted(range(len(raw_pools)),
                     key=lambda i: -max((ratings.get(t) or 0) for t in raw_pools[i]))
    letter = {idx: chr(65 + n) for n, idx in enumerate(rank_of)}
    pools = {letter[i]: sorted(ts, key=lambda t: -(ratings.get(t) or 0))
             for i, ts in enumerate(raw_pools)}

    def game(g, pool=None):
        out = {"slot": g["slot"], "date": g["date"], "time": g["time"],
               "home": g["home"], "away": g["away"], "done": g["done"]}
        if g["done"]:
            out["hs"], out["as"] = g["hs"], g["as"]
        if pool is not None:
            out["pool"] = pool
        return out

    pool_games = [game(g, letter[g["pool"]]) for g in sched if "pool" in g]
    by_stage = {}
    for g in sched:
        if not (g["slot"] or "").isdigit():
            by_stage.setdefault(g["stage"], []).append(g)
    bracket = [{"name": r, "games": [game(g) for g in by_stage.get(r, [])]}
               for r in BRACKET_ROUNDS]

    # Trajectories for the drill-down, written by analysis.rankings from the
    # same replay that produced the CSVs. Optional: if it is missing the page
    # still builds, it just has nothing to open when a name is clicked.
    hist_path = DB_PATH.parent / "history.json"
    history = (json.loads(hist_path.read_text()) if hist_path.exists()
               else {"events": [], "players": {}, "teams": {}})

    # Gender-matching group, decided in identity.resolve and carried on
    # player_elo.csv: 1 = male-matching, 2 = female-matching, 0 = no evidence.
    # It rides inline on each player row for the table, and as a pid -> code
    # map for Trends, which works off history.json's own keys. The map is
    # restricted to players the history file actually holds, since the table
    # rows already carry their own code.
    GCODE = {"m": 1, "w": 2}
    genders = {r["player_id"]: GCODE[r["gender"]]
               for r in all_players if r.get("gender") in GCODE}
    # How load-bearing each rating is, from analysis/identify.py: a true
    # leave-one-out, positive when the results are better explained WITH the
    # player's rating than without it. Optional and partial by design — one
    # replay per player, so it covers the top 1,000 rather than all 39,000 —
    # and the page shows the flag only where it was actually measured.
    loo_path = DB_PATH.parent / "player_loo.csv"
    loo = {}
    if loo_path.exists():
        with open(loo_path, newline="") as f:
            loo = {r["player_id"]: float(r["loo"]) for r in csv.DictReader(f)}

    # Thresholds, not the raw sign. Since roster_shrink compressed the table,
    # 541 of the measured 1,000 sit inside |loo| < 0.001 — genuinely
    # indistinguishable, and flagging on `loo <= 0` would mark 394 players on
    # the strength of noise. Only a clearly negative reading is called out.
    def loo_code(pid):
        """2 supported, 1 indistinguishable, 0 not load-bearing, -1 unmeasured."""
        v = loo.get(pid)
        if v is None:
            return -1
        return 0 if v <= -0.002 else 2 if v >= 0.003 else 1

    payload = {
        "generated": date.today().isoformat(),
        "minGames": MIN_GAMES,
        "totalRated": total_rated,
        "scale": PUBLISHED["division_scale"]["club-men"],
        "players": [[r["player"], float(r["elo"]), float(r["lo90"]), float(r["hi90"]),
                     int(r["games"]), r["last_club"], int(r["last_season"]),
                     int(r["rank"]), r["player_id"], genders.get(r["player_id"], 0),
                     int(r["divisions"]), int(r["divisions_now"]),
                     loo_code(r["player_id"])]
                    for r in players],
        "genders": {pid: g for pid, g in genders.items()
                    if pid in history.get("players", {})},
        # Club rows carry both names: `club` as USAU prints it and `club_key`,
        # the model identity the drill-down opens on. Ranks are per division,
        # so the table shows one division at a time.
        "clubs": {k: [[int(r["rank"]), r["club"], float(r["elo"]),
                       int(r["roster_size"]), r["roster_event"],
                       DIVCODE.get(r["division"], 0), r["club_key"]]
                      for r in v] for k, v in clubs.items()},
        "history": history,
        "usopen": {
            "name": ev[1] if ev else "",
            "start": ev[2] if ev else "",
            "end": ev[3] if ev else "",
            "pools": pools,
            "ratings": ratings,
            "poolGames": pool_games,
            "crossovers": [game(g) for g in crossovers],
            "bracket": bracket,
        },
        "tourneys": tourneys,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"))))
    (OUT.parent / ".nojekyll").write_text("")
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} ({kb:,.0f} KB) + .nojekyll")
    played = sum(g["done"] for g in sched)
    print(f"  {len(payload['players']):,} players (>={MIN_GAMES} games), "
          f"{len(clubs['completed'])} clubs, {len(field)} U.S. Open teams, "
          f"{len(pools)} pools, {played}/{len(sched)} fixtures played")
    print(f"  {len(tourneys['events']):,} tournaments in "
          f"{len(tourneys['series']):,} series")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USAU Player-Elo Rankings</title>
<style>
:root {
  --bg:#f4f5f4; --surface:#fcfcfb; --ink:#12140f; --ink-2:#52544c; --ink-3:#86887e;
  --line:#e2e3dd; --line-strong:#c9cbc2; --accent:#1baf7a; --warn:#eb6834;
  --win:#1baf7a; --lose:#e34948; --chip:#ececE6;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Code",Menlo,Consolas,monospace;
  /* Series palette for the multi-series trend chart. Eight hues x four dash
     patterns distinguishes 32 lines; the chart draws 25. */
  --s1:#2a78d6; --s2:#008300; --s3:#e87ba4; --s4:#eda100;
  --s5:#1baf7a; --s6:#eb6834; --s7:#4a3aa7; --s8:#e34948;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#14150f; --surface:#1c1d16; --ink:#f4f4ee; --ink-2:#c3c3b8; --ink-3:#8b8c80;
  --line:#34352c; --line-strong:#45463b; --accent:#199e70; --warn:#d95926;
  --win:#199e70; --lose:#e66767; --chip:#2a2b22;
  --s1:#3987e5; --s2:#3aa53a; --s3:#d55181; --s4:#d89a1e;
  --s5:#199e70; --s6:#d95926; --s7:#9085e9; --s8:#e66767;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font);
     -webkit-font-smoothing:antialiased}
header{padding:22px 20px 0;max-width:1180px;margin:0 auto}
h1{font-size:19px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--ink-3);font-size:13px;margin-bottom:16px}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line);max-width:1180px;margin:0 auto;
    padding:0 20px}
nav button{background:none;border:0;padding:9px 15px;font:inherit;font-size:14px;
  color:var(--ink-3);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
nav button:hover{color:var(--ink)}
nav button.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:550}
main{max-width:1180px;margin:0 auto;padding:20px}
section{display:none} section.on{display:block}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
input[type=search],select{font:inherit;font-size:14px;padding:6px 10px;border-radius:7px;
  border:1px solid var(--line-strong);background:var(--surface);color:var(--ink)}
input[type=search]{min-width:210px}
label.chk{display:inline-flex;align-items:center;gap:6px;font-size:14px;color:var(--ink-2);
  cursor:pointer;user-select:none}
button.act{font:inherit;font-size:13px;padding:6px 12px;border-radius:7px;cursor:pointer;
  border:1px solid var(--line-strong);background:var(--surface);color:var(--ink-2)}
button.act:hover{border-color:var(--accent);color:var(--ink)}
table{width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 11px;border-bottom:1px solid var(--line);font-size:14px}
th{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-3);
   font-weight:600;background:var(--chip)}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono);
  font-size:13px}
td.rk{color:var(--ink-3);font-family:var(--mono);font-size:12.5px;width:44px}
/* An unpinned rating is marked, not silently printed as if it were measured.
   Muted rather than alarming: it says "we cannot see this", not "this is wrong". */
td.band.unsupported{color:var(--ink-3)}
td.band.weaksup{color:var(--ink-2)}
.unsup{color:var(--warn);font-weight:600;cursor:help}
.muted{color:var(--ink-3)} .note{font-size:12.5px;color:var(--ink-3);margin-top:10px;
  line-height:1.6;max-width:820px}
button.act.prim{border-color:var(--accent);color:var(--ink)}
button.act:disabled{opacity:.42;cursor:default;border-color:var(--line)}
button.act:disabled:hover{border-color:var(--line);color:var(--ink-2)}
/* A simulated result is a coin flip, not a played game. Dashed and desaturated
   so it can never be mistaken for a real one you typed in. */
.t.w.simd{background:color-mix(in srgb,var(--ink-3) 13%,transparent) !important;
  border-style:dashed !important;border-color:var(--ink-3) !important}
.t.w.simd .nm::after,.g .t.w.simd > span:first-child::after{content:' ~';color:var(--ink-3)}
.count{font-size:12.5px;color:var(--ink-3)}
/* US Open */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(258px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 13px}
.card h3{margin:0 0 9px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--ink-3);font-weight:600}
.g{display:flex;align-items:center;gap:6px;padding:4px 0;font-size:13.5px}
.g .t{flex:1;padding:3px 7px;border-radius:6px;cursor:pointer;border:1px solid transparent;
  display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.g .t:hover{border-color:var(--line-strong)}
.g .t.w{background:color-mix(in srgb,var(--win) 15%,transparent);border-color:var(--win);
  font-weight:600}
.g .t.l{opacity:.44}
/* A played game is not a control: the result came from USAU's schedule, not
   from a click, so the line drops its affordances and keeps its highlight. */
.g .t.fact,.m .t.fact{cursor:default}
.g .t.fact:hover{border-color:transparent}
.m .t.fact:hover{background:none}
.g .p{font-family:var(--mono);font-size:11.5px;color:var(--ink-3)}
.g .vs{color:var(--ink-3);font-size:11px}
.stand{width:100%;font-size:13px;margin-top:8px}
.stand td{padding:2.5px 0;border:0;font-size:13px}
.stand td.w{text-align:right;font-family:var(--mono);color:var(--ink-3);font-size:12px}
.seed{display:inline-block;width:17px;color:var(--ink-3);font-family:var(--mono);font-size:11px}
/* Bracket geometry. A CSS grid with explicit row spans does the alignment for
   free: a semi spans the two rows its quarters occupy, so align-items:center
   drops it exactly level with the midpoint between them. Connector geometry
   then follows from the row pitch (--mh + --rg) rather than magic numbers. */
.bracket{--mh:66px; --rg:10px; --cg:30px;
  display:grid; grid-template-columns:repeat(4,minmax(148px,1fr));
  grid-template-rows:auto repeat(4,var(--mh));
  column-gap:var(--cg); row-gap:var(--rg); align-items:center; margin-top:4px}
.round h4{margin:0 0 2px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--ink-3);font-weight:600}
.m{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:4px;
   position:relative;display:flex;flex-direction:column;justify-content:center;gap:1px}
.m .t{display:flex;gap:6px;padding:3px 6px;border-radius:5px;
  cursor:pointer;font-size:13px;align-items:baseline}
.m .t:hover{background:var(--chip)}
.m .t.w{background:color-mix(in srgb,var(--win) 15%,transparent);font-weight:600}
.m .t.l{opacity:.42}
.m .t.tbd{color:var(--ink-3);cursor:default}
.m .t.tbd:hover{background:none}
.m .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m .p{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
/* seed chip: which pool slot, or which earlier match, feeds this line */
.m .sd{font-family:var(--mono);font-size:9.5px;color:var(--ink-3);background:var(--chip);
  border-radius:3px;padding:1px 3px;min-width:26px;text-align:center;flex:none}
/* outgoing stub, and incoming stub + vertical join drawn by the <i> */
.m.out::after{content:'';position:absolute;left:100%;top:50%;
  width:calc(var(--cg)/2);border-top:1px solid var(--line-strong)}
.m > i.cin{position:absolute;right:100%;top:50%;transform:translateY(-50%);
  width:calc(var(--cg)/2);height:var(--span,0px);
  border-left:1px solid var(--line-strong)}
.m > i.cin::after{content:'';position:absolute;left:0;top:50%;
  width:calc(var(--cg)/2);border-top:1px solid var(--line-strong)}
.champline{margin-top:14px;font-size:13px;color:var(--ink-2)}
.champline b{background:color-mix(in srgb,var(--accent) 18%,transparent);
  border:1px solid var(--accent);border-radius:6px;padding:3px 9px}
.odds td.bar-c{width:38%;padding-right:12px}
.oddsbar{height:7px;border-radius:4px;background:var(--accent);min-width:1px}
.champ{background:color-mix(in srgb,var(--accent) 13%,transparent);border-color:var(--accent)}
@media (max-width:860px){
  .bracket{grid-template-columns:1fr;grid-template-rows:none;row-gap:6px}
  .bracket > *{grid-column:1 !important;grid-row:auto !important}
  .m.out::after,.m > i.cin{display:none}
}
/* drill-down overlay */
.nmlink{cursor:pointer;border-bottom:1px dotted var(--line-strong)}
.nmlink:hover{color:var(--accent);border-bottom-color:var(--accent)}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.42);display:none;z-index:40}
#scrim.on{display:block}
#detail{position:fixed;top:0;right:0;bottom:0;width:min(760px,100vw);z-index:41;
  background:var(--surface);border-left:1px solid var(--line-strong);
  overflow-y:auto;padding:18px 22px 40px;display:none;
  box-shadow:-10px 0 40px rgba(0,0,0,.18)}
#detail.on{display:block}
#detail h2{font-size:18px;margin:0 0 2px;letter-spacing:-.01em}
#detail .meta{color:var(--ink-3);font-size:13px;margin-bottom:14px}
#detail .close{position:absolute;top:14px;right:18px;font-size:20px;line-height:1;
  background:none;border:0;color:var(--ink-3);cursor:pointer;padding:4px 8px}
#detail .close:hover{color:var(--ink)}
.chart{width:100%;height:190px;display:block;margin:2px 0 6px}
.chart .ln{fill:none;stroke:var(--accent);stroke-width:2}
.chart .dot{fill:var(--accent)}
.chart .ax{stroke:var(--line);stroke-width:1}
.chart .gl{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.chart text{fill:var(--ink-3);font-size:10px;font-family:var(--mono)}
.chart .hit{fill:transparent;cursor:crosshair}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);
  font-size:11.5px;padding:4px 7px;border-radius:5px;display:none;z-index:50;
  font-family:var(--mono);white-space:nowrap}
#tip.on{display:block}
.hist td.d{font-family:var(--mono);font-size:12px;color:var(--ink-3);white-space:nowrap}
.hist td.r{text-align:right;font-family:var(--mono);font-size:13px}
.hist td.dl{text-align:right;font-family:var(--mono);font-size:11.5px}
.up{color:var(--win)} .dn{color:var(--lose)}

/* multi-series trend chart */
.tgrid{display:grid;grid-template-columns:1fr 210px;gap:16px;align-items:start}
@media (max-width:860px){.tgrid{grid-template-columns:1fr}}
.tchart{width:100%;height:auto;display:block}
.tchart .gl{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.tchart .ax{stroke:var(--line);stroke-width:1}
.tchart text{fill:var(--ink-3);font-size:10px;font-family:var(--mono)}
.tchart text.sx{text-anchor:middle}
/* Dimming is two class writes plus one root class, not 25 inline styles: the
   root carries .dim, only the hovered group and its legend row carry .hot. */
.tchart .sg{fill:none;stroke:currentColor;stroke-width:1.5;opacity:.85}
.tchart .sg circle{fill:currentColor;stroke:none}
.tchart .sg .hit{stroke-width:10;stroke-opacity:0}
.tchart.dim .sg{opacity:.15}
.tchart.dim .sg.hot{opacity:1;stroke-width:2.5}
/* Capped so a 164-series legend does not stretch the page metres deep. */
.legend{display:flex;flex-direction:column;gap:1px;max-height:432px;overflow-y:auto}
.lhead{font-size:11.5px;color:var(--ink-3);font-family:var(--mono);
  padding:0 4px 6px;border-bottom:1px solid var(--line);margin-bottom:5px}
.lrow{display:grid;grid-template-columns:16px 1fr auto;gap:8px;align-items:center;
  padding:2px 4px;border-radius:5px;cursor:pointer;font-size:13px;opacity:.92}
.lrow:hover{background:var(--chip)}
.legend.dim .lrow{opacity:.4}
.legend.dim .lrow.hot{opacity:1;background:var(--chip);font-weight:550}
.lrow .sw{width:14px;height:3px;overflow:visible}
.lrow .lbl{color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrow .pk{font-family:var(--mono);font-size:11.5px;color:var(--ink-3);text-align:right}
/* drill-down: back button + inline roster row */
#detail .back{position:absolute;top:17px;right:52px;font-size:13px;line-height:1;
  background:none;border:0;color:var(--ink-3);cursor:pointer;padding:4px 6px;display:none}
#detail .back.on{display:block}
#detail .back:hover{color:var(--ink)}
.hist td.tm{font-size:13px}
.hist tr.rost td{background:var(--chip);font-size:12.5px;line-height:1.85;
  color:var(--ink-2)}
.hist tr.rost .nmlink{color:var(--ink)}
/* An event row opens onto the games behind it. Same caret as the roster
   disclosure above, so the two read as the same gesture. */
.hist .disc{cursor:pointer;user-select:none}
.hist .disc::before{content:'\25b8';display:inline-block;width:12px;font-size:10px;
  color:var(--ink-3);transition:transform .12s ease}
.hist tr.open .disc::before{transform:rotate(90deg)}
.hist .disc:hover{color:var(--accent)}
.hist tr.gms > td{background:var(--chip);padding:7px 12px 9px 26px}
.gsum{font-size:11.5px;color:var(--ink-3);margin:0 0 5px;
  text-transform:uppercase;letter-spacing:.04em}
.gtbl{width:100%;border-collapse:collapse}
.gtbl td{border:0;padding:2px 10px 2px 0;font-size:13px}
.gtbl td.st{font-size:11.5px;color:var(--ink-3);white-space:nowrap;width:34%}
.gtbl td.wl{font-family:var(--mono);font-size:12px;width:22px;text-align:center}
.gtbl td.sc{font-family:var(--mono);font-size:12.5px;text-align:right;width:62px}
/* hovering a season column reorders the legend by that season */
.tchart .xh{fill:transparent}
.tchart .yg{stroke:var(--line-strong);stroke-width:1;opacity:0}
.tchart .yg.on{opacity:1}
/* Past ~40 series the field has to thin out or it reads as a single smear;
   isolation on hover is what makes an individual line legible. */
.tchart[data-dense] .sg{opacity:.5;stroke-width:1.1}
.tchart[data-dense].dim .sg{opacity:.08}
.tchart[data-dense].dim .sg.hot{opacity:1;stroke-width:2.5}
.lrow .pk.na{opacity:.42}
/* roster-by-season browser in a club panel, collapsed by default */
.rsec{margin:2px 0 18px;border:1px solid var(--line);border-radius:10px;
  background:var(--surface)}
.rsec > summary{cursor:pointer;list-style:none;padding:9px 13px;font-size:11.5px;
  text-transform:uppercase;letter-spacing:.05em;color:var(--ink-3);font-weight:600;
  display:flex;align-items:center;gap:7px;user-select:none}
.rsec > summary::-webkit-details-marker{display:none}
.rsec > summary::before{content:'\25b8';font-size:10px}
.rsec[open] > summary::before{content:'\25be'}
.rsec > summary:hover{color:var(--ink)}
.rtabs{display:flex;flex-wrap:wrap;gap:4px;padding:0 13px 10px}
.rtab{font:inherit;font-size:12.5px;font-family:var(--mono);padding:3px 9px;
  border-radius:6px;cursor:pointer;border:1px solid var(--line-strong);
  background:var(--surface);color:var(--ink-2)}
.rtab:hover{border-color:var(--accent);color:var(--ink)}
.rtab.on{background:var(--chip);border-color:var(--accent);color:var(--ink);
  font-weight:600}
#rpane{padding:0 13px 13px}
.rsum{font-size:12px;color:var(--ink-3);margin:0 0 9px;line-height:1.6}

/* ---------- tournaments ---------- */
.evtbl tr.ev{cursor:pointer}
.evtbl tr.ev:hover td{background:var(--chip)}
td.dt{font-family:var(--mono);font-size:12.5px;color:var(--ink-3);white-space:nowrap}
.tag{display:inline-block;font-size:10.5px;text-transform:uppercase;
  letter-spacing:.04em;padding:1px 6px;border-radius:4px;background:var(--chip);
  color:var(--ink-3);font-weight:600;white-space:nowrap}
.tag.t4{background:color-mix(in srgb,var(--accent) 20%,transparent);color:var(--ink)}
.tag.t3{background:color-mix(in srgb,var(--accent) 11%,transparent)}
.crown{color:var(--accent);font-weight:600}
#tvhead{margin:0 0 14px}
#tvhead h2{font-size:20px;margin:0 0 3px;letter-spacing:-.01em}
#tvhead .meta{color:var(--ink-3);font-size:13px}
.sect{font-size:13px;margin:24px 0 9px;color:var(--ink-2)}
.sect .muted{font-weight:400}
/* Bracket geometry, parameterised. The U.S. Open tracker's grid is fixed at
   four rounds; a recovered bracket runs from one round to seven, so columns,
   rows and the connector pitch are all set inline per tournament. */
.tbr{--mh:50px; --rg:8px; --cg:26px; display:grid; column-gap:var(--cg);
  row-gap:var(--rg); align-items:center; margin-top:4px; overflow-x:auto}
.tbr .m{background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:3px;position:relative;display:flex;flex-direction:column;
  justify-content:center;gap:1px}
.tbr .m.empty{background:none;border-style:dashed;opacity:.35}
.tbr .t{display:flex;gap:6px;padding:2px 6px;border-radius:5px;font-size:12.5px;
  align-items:baseline}
.tbr .t.w{background:color-mix(in srgb,var(--win) 15%,transparent);font-weight:600}
.tbr .t.l{opacity:.5}
.tbr .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tbr .p{font-family:var(--mono);font-size:11.5px;color:var(--ink-2)}
.tbr .sd{font-family:var(--mono);font-size:9.5px;color:var(--ink-3);
  background:var(--chip);border-radius:3px;padding:1px 3px;min-width:22px;
  text-align:center;flex:none}
.tbr .m.out::after{content:'';position:absolute;left:100%;top:50%;
  width:calc(var(--cg)/2);border-top:1px solid var(--line-strong)}
.tbr .m > i.cin{position:absolute;right:100%;top:50%;transform:translateY(-50%);
  width:calc(var(--cg)/2);height:var(--span,0px);
  border-left:1px solid var(--line-strong)}
.tbr .m > i.cin::after{content:'';position:absolute;left:0;top:50%;
  width:calc(var(--cg)/2);border-top:1px solid var(--line-strong)}
@media (max-width:860px){
  .tbr{grid-template-columns:1fr !important;grid-template-rows:none !important;
       row-gap:6px}
  .tbr > *{grid-column:1 !important;grid-row:auto !important}
  .tbr .m.out::after,.tbr .m > i.cin{display:none}
}
.stand td.pd{text-align:right;font-family:var(--mono);color:var(--ink-3);
  font-size:11.5px;padding-left:8px}
</style>

<div id="scrim"></div>
<div id="detail"><button class="back" id="dback">&lsaquo; Back</button>
  <button class="close" id="dclose">&times;</button>
  <div id="dbody"></div></div>
<div id="tip"></div>
<header>
  <h1>USAU Player-Elo Rankings</h1>
  <div class="sub" id="sub"></div>
</header>
<nav>
  <button data-t="clubs" class="on">Club Team Rankings</button>
  <button data-t="players">Players</button>
  <button data-t="events">Tournaments</button>
  <button data-t="trends">Trends</button>
  <button data-t="usopen">U.S. Open 2026</button>
</nav>
<main>

<section id="clubs" class="on">
  <div class="bar">
    <input type="search" id="cq" placeholder="Search club…" autocomplete="off">
    <select id="basis">
      <option value="completed">Most recent completed roster</option>
      <option value="best">Best full-strength roster of 2026</option>
      <option value="upcoming">Next event roster</option>
    </select>
    <select id="cdiv">
      <option value="0">Club Men's</option>
      <option value="3">Club Mixed</option>
      <option value="4">Club Women's</option>
    </select>
    <span class="count" id="ccount"></span>
  </div>
  <table><thead><tr>
    <th class="n">#</th><th>Club</th><th class="n">Elo</th>
    <th class="n">Roster</th><th>Rated off</th>
  </tr></thead><tbody id="ctb"></tbody></table>
  <p class="note" id="cnote"></p>
</section>

<section id="players">
  <div class="bar">
    <input type="search" id="q" placeholder="Search player or club…" autocomplete="off">
    <label class="chk"><input type="checkbox" id="only26" checked> 2026 rosters only</label>
    <select id="ming">
      <option value="30">30+ games</option>
      <option value="60">60+ games</option>
      <option value="120">120+ games</option>
      <option value="200">200+ games</option>
    </select>
    <select id="pdiv">
      <option value="all">All divisions</option>
      <option value="0">Club Men's</option>
      <option value="1">College</option>
      <option value="2">College D-III</option>
      <option value="3">Club Mixed</option>
      <option value="4">Club Women's</option>
    </select>
    <select id="pgen">
      <option value="all">All genders</option>
      <option value="1">Male-matching</option>
      <option value="2">Female-matching</option>
    </select>
    <span class="count" id="pcount"></span>
  </div>
  <table><thead><tr>
    <th class="n">#</th><th>Player</th><th class="n">Elo</th><th>90% band</th>
    <th class="n">G</th><th>Last club</th><th class="n">Yr</th>
  </tr></thead><tbody id="ptb"></tbody></table>
  <p class="note" id="pnote"></p>
</section>

<section id="events">
  <div id="tlist">
    <div class="bar">
      <input type="search" id="eq" placeholder="Search tournament, city…" autocomplete="off">
      <select id="ediv">
        <option value="all">All divisions</option>
        <option value="0">Club Men's</option>
        <option value="1">College</option>
        <option value="2">College D-III</option>
        <option value="3">Club Mixed</option>
        <option value="4">Club Women's</option>
      </select>
      <select id="eyear"></select>
      <select id="etier">
        <option value="all">All tournaments</option>
        <option value="series">Championship series</option>
        <option value="4">Nationals &amp; majors</option>
        <option value="3">Regionals</option>
        <option value="2">Sectionals</option>
        <option value="1">Conference</option>
        <option value="0">Regular season</option>
      </select>
      <span class="count" id="ecount"></span>
    </div>
    <table class="evtbl"><thead><tr>
      <th>Dates</th><th>Tournament</th><th>Division</th>
      <th class="n">Teams</th><th>Champion</th><th class="n">Editions</th>
    </tr></thead><tbody id="etb"></tbody></table>
    <p class="note" id="enote"></p>
  </div>
  <div id="tview" style="display:none">
    <div class="bar"><button class="act" id="eback">&lsaquo; All tournaments</button></div>
    <div id="tvhead"></div>
    <div id="tvbody"></div>
    <p class="note" id="tvnote"></p>
  </div>
</section>

<section id="trends">
  <div class="bar">
    <select id="tsub">
      <option value="p">Players</option>
      <option value="c">Clubs</option>
    </select>
    <select id="tdiv">
      <option value="all">All divisions</option>
      <option value="0">Club Men's</option>
      <option value="1">College</option>
      <option value="2">College D-III</option>
      <option value="3">Club Mixed</option>
      <option value="4">Club Women's</option>
    </select>
    <select id="tgen">
      <option value="all">All genders</option>
      <option value="1">Male-matching</option>
      <option value="2">Female-matching</option>
    </select>
    <select id="tmode">
      <option value="elo">Elo</option>
      <option value="med">Above season median</option>
    </select>
    <span class="count" id="tcount"></span>
  </div>
  <div class="tgrid">
    <div id="tchart"></div>
    <div>
      <div class="lhead" id="tlhead"></div>
      <div class="legend" id="tlegend"></div>
    </div>
  </div>
  <p class="note" id="tnote"></p>
</section>

<section id="usopen">
  <div class="bar">
    <button class="act prim" id="simGame">Simulate next game</button>
    <button class="act prim" id="simRound">Simulate next round</button>
    <button class="act" id="reset">Clear entered results</button>
    <span class="count" id="ucount"></span>
  </div>
  <h3 style="font-size:13px;margin:14px 0 9px;color:var(--ink-2)">
    Pool play <span class="muted" style="font-weight:400">— played results are
    fixed; click a team to call an unplayed game</span></h3>
  <div class="grid" id="pools"></div>
  <h3 style="font-size:13px;margin:22px 0 9px;color:var(--ink-2)">Bracket</h3>
  <div class="bracket" id="bracket"></div>
  <div class="champline" id="champline"></div>
  <h3 style="font-size:13px;margin:22px 0 9px;color:var(--ink-2)">
    Title odds — re-simulated from the games already played</h3>
  <table class="odds"><thead><tr>
    <th class="n">#</th><th>Team</th><th class="n">Elo</th>
    <th class="n">Reach SF</th><th class="n">Reach final</th><th class="n">Title</th>
    <th class="bar-c"></th>
  </tr></thead><tbody id="otb"></tbody></table>
  <p class="note" id="unote"></p>
</section>

</main>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct = v => (v*100).toFixed(1) + '%';

/* The caveat that has to travel with any cross-gender comparison on this page.
   Men's and women's players never meet, so their ratings are commensurable
   only through the mixed division both play in — a bridge, not a head-to-head.
   Read a female-matching rating as a position within a pool linked to the
   men's pool by shared players, not as a prediction of a game that no USAU
   series ever schedules. */
const GENDER_NOTE =
  `mixed division, where men's and women's players share a roster. That makes ` +
  `a rating comparable ACROSS divisions only as far as the bridge carries: club ` +
  `men's and club women's teams never play each other, so a cross-gender gap is ` +
  `an inference through mixed, not a head-to-head result. Male- and ` +
  `female-matching come from division play where it exists, the roster page's ` +
  `Pronouns column otherwise, and a first-name likelihood (98.6% accurate on ` +
  `held-out players) for the mixed-only remainder; players no rule places are ` +
  `left out of both filtered views.`;

/* ---------- tabs ---------- */
function showTab(id) {
  document.querySelectorAll('nav button').forEach(
    x => x.classList.toggle('on', x.dataset.t === id));
  document.querySelectorAll('section').forEach(
    s => s.classList.toggle('on', s.id === id));
  // Reducing 26k trajectories to season maps is ~370k operations, so it runs on
  // first activation of the tab rather than at load. seasonData memoises.
  if (id === 'trends' && !$('#tsvg')) drawTrends();
}
document.querySelectorAll('nav button').forEach(
  b => b.onclick = () => showTab(b.dataset.t));

$('#sub').textContent =
  `Every player carries a personal Elo across seasons; a club's rating is the ` +
  `softmax-weighted mean of its event roster. Clubs cover the three club ` +
  `divisions, Players and Trends add college and college D-III. Tournaments ` +
  `covers every event in the corpus. The U.S. Open tracker is club men's. ` +
  `Generated ${D.generated || ''}.`;

/* ---------- clubs ---------- */
const CNOTE = {
  completed: 'Each club rated off the most recent event it has actually finished. ' +
    'This is the default published table.',
  best: "Each club rated off its strongest roster of 2026 that was at least 80% the size " +
    "of its own largest squad. The floor matters: without it, taking a max over rosters " +
    "picks the smallest one, because a mean over an elite subset beats a mean over a full squad.",
  upcoming: 'Each club rated off the roster it has registered for its next event. ' +
    'Rosters post weeks ahead, so these are provisional until the games are played — ' +
    'but for a club that fielded a B-squad last time out, this is the truer number.'
};
function drawClubs() {
  const basis = $('#basis').value, div = +$('#cdiv').value;
  // One division at a time, and the rank shown is the one the CSV assigned
  // WITHIN it: club men's and club women's teams never play each other, so a
  // merged 1..n would invite a comparison the games cannot settle.
  const q = $('#cq').value.trim().toLowerCase();
  const pop = (D.clubs[basis] || []).filter(r => r[5] === div);
  // Search is a lookup, not a re-ranking, the same as the player table: a club
  // keeps the number it holds in its division, so hits come back sparse
  // (#3, #17, #41). Matches the printed name and the event it was rated off.
  const rows = q ? pop.filter(r => r[1].toLowerCase().includes(q) ||
                                   String(r[4]).toLowerCase().includes(q))
                 : pop;
  $('#ctb').innerHTML = rows.map(r =>
    `<tr><td class="rk">${r[0]}</td>` +
    `<td><span class="nmlink" data-club="${esc(r[6])}">${esc(r[1])}</span></td>` +
    `<td class="n">${r[2].toFixed(0)}</td><td class="n">${r[3]}</td>` +
    `<td class="muted" style="font-size:13px">${esc(r[4])}</td></tr>`).join('');
  $('#ccount').textContent = q
    ? `${rows.length} of ${pop.length} ${DIVLABEL[div]} clubs match`
    : `${pop.length} ${DIVLABEL[div]} clubs`;
  $('#cnote').textContent = CNOTE[basis];
}
['input', 'change'].forEach(e => $('#cq').addEventListener(e, drawClubs));
$('#basis').onchange = drawClubs;
$('#cdiv').onchange = drawClubs;

/* ---------- players ---------- */
/* Leave-one-out verdict per rating, measured in analysis/identify.py for the
   top 1,000 only. It is a statement about IDENTIFIABILITY, not about the
   player: within a roster every player takes the same delta, so a rating can
   drift somewhere the games never pin down. Where that has happened the band
   printed beside it understates the uncertainty, and saying so is more honest
   than inventing a wider one — converting a logloss delta into an Elo
   interval is not something this corpus can calibrate. */
const LOOCLASS = {0: 'unsupported', 1: '', 2: '', '-1': ''};
const LOOTIP = {
  0: 'Removing this player from every roster does NOT hurt the prediction of ' +
     'their own teams\u2019 games — the results are explained at least as well ' +
     'without this rating. Treat the number as unpinned.',
  1: 'Measured, and the effect is too small to call either way: the games ' +
     'neither clearly support this rating nor clearly contradict it.',
  2: 'Load-bearing — removing this player measurably degrades the prediction ' +
     'of their own teams\u2019 games, so the rating carries real information.',
  '-1': 'Not measured — leave-one-out covers the top 1,000 by rating.'
};
function drawPlayers() {
  const q = $('#q').value.trim().toLowerCase();
  const only26 = $('#only26').checked, ming = +$('#ming').value;
  const gen = $('#pgen').value, div = $('#pdiv').value;
  // Rank is a property of the player within the POPULATION the toggles define,
  // so it is assigned before the search runs. Searching is a lookup, not a
  // re-ranking: find a player and their number is the one they actually hold,
  // and results come back sparse (#12, #47, #103) rather than renumbered 1..n.
  let pop = D.players.filter(p => p[4] >= ming);
  if (only26) pop = pop.filter(p => p[6] === 2026);
  // Gender-matching is evidence, not a partition: a player with none is in
  // neither filtered view, so the two never sum to the unfiltered count.
  if (gen !== 'all') pop = pop.filter(p => p[9] === +gen);
  // Division is a bitmask of where the player turned out, and WHICH mask
  // depends on the season toggle. With "2026 rosters only" on, the question
  // is who is in this division NOW, so it reads the last-season mask: Nathan
  // Champoux has been on Hybrid since 2019 and stops being filed under club
  // men's for two events in 2018. With the toggle off the list already spans
  // every era, so the career mask is the honest match. The rating itself is
  // one number across every division — narrowing selects WHO is listed, it
  // does not recompute anyone against that division alone.
  if (div !== 'all') {
    const bit = 1 << +div;
    pop = pop.filter(p => (only26 ? p[11] : p[10]) & bit);
  }
  const rankOf = new Map();
  pop.forEach((p, i) => rankOf.set(p, i + 1));
  const rows = q ? pop.filter(p => p[0].toLowerCase().includes(q) ||
                                   String(p[5]).toLowerCase().includes(q))
                 : pop;
  const shown = rows.slice(0, 300);
  $('#ptb').innerHTML = shown.map(p =>
    `<tr><td class="rk" title="#${p[7]} of all ${D.totalRated.toLocaleString()} rated players">` +
    `${rankOf.get(p)}</td>` +
    `<td><span class="nmlink" data-pid="${p[8]}">${esc(p[0])}</span></td>` +
    `<td class="n">${p[1].toFixed(0)}</td>` +
    `<td class="band ${LOOCLASS[p[12]] || ''}" title="${esc(LOOTIP[p[12]] || '')}">` +
    `[${p[2].toFixed(0)}, ${p[3].toFixed(0)}]${p[12] === 0 ? ' <span class="unsup">?</span>' : ''}</td>` +
    `<td class="n">${p[4]}</td><td class="muted" style="font-size:13px">${esc(p[5])}</td>` +
    `<td class="n">${p[6]}</td></tr>`).join('');
  $('#pcount').textContent = q
    ? `${rows.length.toLocaleString()} of ${pop.length.toLocaleString()} match` +
      (rows.length > 300 ? ' — showing first 300' : '')
    : `${pop.length.toLocaleString()} players` +
      (pop.length > 300 ? ' — showing first 300' : '');
}
['input','change'].forEach(e => {
  $('#q').addEventListener(e, drawPlayers);
  $('#only26').addEventListener(e, drawPlayers);
  $('#ming').addEventListener(e, drawPlayers);
  $('#pgen').addEventListener(e, drawPlayers);
  $('#pdiv').addEventListener(e, drawPlayers);
});
$('#pnote').textContent =
  `Searching does not renumber anything — a player keeps the rank they hold in the ` +
  `current list, so results come back sparse. The toggles do change the rank, ` +
  `because they change who is being ranked; hover a rank to see the player's ` +
  `position across all ${D.totalRated.toLocaleString()} rated players. ` +
  `Bands are 90% intervals on the rating as an estimate of current skill, and ` +
  `they are ONE population figure for everyone, which the "?" marks call out: ` +
  `for the top 1,000 each rating was re-tested by dropping the player from ` +
  `every roster and replaying, and a "?" means the results are explained at ` +
  `least as well without it. Read the whole top of this table with that in ` +
  `mind — the game delta is applied to every rostered player equally, so what ` +
  `separates teammates is thin, and a player's position reflects how far they ` +
  `sit above their own teammates as much as how good they are. It is also not ` +
  `the club scale: the best player here reads ${Math.round(Math.max(...D.players.map(p=>p[1])))} ` +
  `against a best club of about 2,580, because a club's rating is a mean over ` +
  `20-plus people and one star barely moves it. Players ` +
  `below ${D.minGames} games are omitted: under that the engine's provisional ` +
  `multiplier is still moving a rating faster than results justify. Ratings never ` +
  `decay, so an unfiltered list mixes eras — "2026 rosters only" is on by default. ` +
  `The division follows that toggle: with it on you get players in the ` +
  `division NOW, with it off anyone who ever played it. Nobody appears in two ` +
  `club divisions at once — men's, mixed and women's are alternatives, so a ` +
  `player is filed under whichever they played most this season and someone ` +
  `on a mixed team shows only under mixed. College is different and is kept ` +
  `alongside: it runs in the spring and club in the summer, so 1,789 people ` +
  `are genuinely in both and appear in both. Their rating is ` +
  `still the one number they carry everywhere, not a per-division rating. ` +
  `All five divisions share one rating scale, bridged by the ${GENDER_NOTE}`;

/* ---------- U.S. Open ---------- */
const U = D.usopen, R = U.ratings, SCALE = D.scale;
const POOLS = U.pools, PK = Object.keys(POOLS).sort();
const PGAMES = U.poolGames, XGAMES = U.crossovers, RNDS = U.bracket;
const SFI = RNDS.findIndex(rd => rd.name === 'Semifinals'), FI = RNDS.length - 1;
const ABBR = {Prequarterfinals:'PQ', Quarterfinals:'QF', Semifinals:'SF', Final:'F'};

/* Played games are FACTS. They live in the page, come from USAU's own
   schedule, and no click can move them; what is stored in your browser is only
   the calls you make on games not yet played. State is keyed on the fixture's
   SLOT id, which is USAU's game number and never moves. v3: it used to be
   keyed on positions in a schedule array, and day one renumbered those the
   moment TBD slots were seeded with real game ids. */
const KEY = 'usopen2026.v3';
let S = load();
function load() {
  let v;
  try { v = JSON.parse(localStorage.getItem(KEY)); } catch (e) { v = null; }
  v = v || {};
  return {w: v.w || {}, sim: v.sim || {}};
}
function save() { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} }

const P = (a,b) => 1 / (1 + Math.pow(10, ((R[b]||1500) - (R[a]||1500)) / SCALE));
const winner = g => g.done ? (g.hs > g.as ? g.home : g.away) : null;
// A call only stands while it names one of the two teams actually in the slot:
// an upstream result can change who is standing there.
const called = (g,h,a) => (S.w[g.slot] === h || S.w[g.slot] === a) ? S.w[g.slot] : null;
const settled = (g,h,a) => winner(g) || called(g,h,a);

/* Resolve every bracket slot. USAU publishes participants as soon as it knows
   them - day one filled the prequarters AND the quarters - so a slot reads its
   teams off the fixture and only falls back to a feed while it is still TBD.
   That feed is the one structural assumption left in the bracket: a round half
   the size of the one before it takes the winners of games 2i and 2i+1, which
   is how USAU's own columns line up. The old code had to guess the whole
   pairing from pool finishes; none of that survives contact with the real
   bracket. */
function resolve(pick) {
  const out = [];
  RNDS.forEach((rd, r) => {
    const prev = out[r-1], feed = prev && prev.length === 2 * rd.games.length;
    out.push(rd.games.map((g, i) => {
      const home = g.home || (feed ? prev[2*i].w : null);
      const away = g.away || (feed ? prev[2*i+1].w : null);
      return {g, home, away, w: (home && away) ? pick(g, home, away) : null};
    }));
  });
  return out;
}

/* Pool standings. Ties break on rating - point differential is not modelled. */
function standings(k) {
  const ts = POOLS[k], w = {};
  ts.forEach(t => w[t] = 0);
  PGAMES.forEach(g => {
    if (g.pool !== k) return;
    const x = settled(g, g.home, g.away);
    if (x) w[x]++;
  });
  return {w, order: ts.slice().sort((x,y) => (w[y]-w[x]) || (R[y]-R[x]))};
}
let SEED = {};   // team -> "A1", its pool finish; rebuilt whenever a result moves

/* Monte Carlo over what is left, conditioned on every result already in.
   With the quarters published, pool play no longer feeds anything, so the
   remaining uncertainty is the bracket itself. */
function simulate(n) {
  const sf = {}, fin = {}, ti = {};
  Object.keys(R).forEach(t => { sf[t] = 0; fin[t] = 0; ti[t] = 0; });
  const pick = (g,h,a) => settled(g,h,a) || (Math.random() < P(h,a) ? h : a);
  const bump = (tally, t) => { if (t) tally[t] = (tally[t] || 0) + 1; };
  for (let s = 0; s < n; s++) {
    const B = resolve(pick);
    if (SFI >= 0) B[SFI].forEach(m => { bump(sf, m.home); bump(sf, m.away); });
    const f = B[FI][0];
    if (f) { bump(fin, f.home); bump(fin, f.away); bump(ti, f.w); }
  }
  return {sf, fin, ti, n};
}

/* One fixture line. A played game shows its score where an unplayed one shows
   the model's probability, so the two read in the same shape; the pre-game
   number survives as the row's tooltip, which is where an upset shows up. */
function gameRow(g) {
  const w = settled(g, g.home, g.away), ph = P(g.home, g.away);
  const sd = S.sim[g.slot] ? ' simd' : '', fact = g.done ? ' fact' : '';
  const side = (t, own, p) => {
    const cls = (w ? (w === t ? 't w' + sd : 't l') : 't') + fact;
    const hook = g.done ? '' : ` data-g="${g.slot}" data-w="${esc(t)}"`;
    return `<div class="${cls}"${hook}><span>${esc(t)}</span>` +
           `<span class="p">${g.done ? own : pct(p)}</span></div>`;
  };
  const tip = g.done ? ` title="model gave ${esc(g.home)} ${pct(ph)} before the game"` : '';
  return `<div class="g"${tip}>${side(g.home, g.hs, ph)}` +
         `<span class="vs">v</span>${side(g.away, g.as, 1-ph)}</div>`;
}

function drawPools() {
  const cards = PK.map(k => {
    const st = standings(k), gs = PGAMES.filter(g => g.pool === k);
    const done = gs.filter(g => settled(g, g.home, g.away)).length;
    return `<div class="card"><h3>Pool ${k} — ${done}/${gs.length} played</h3>` +
      gs.map(gameRow).join('') +
      `<table class="stand">` + st.order.map((t,j) =>
        `<tr><td><span class="seed">${j+1}</span>${esc(t)}` +
        `<span class="muted" style="font-size:11.5px"> ${R[t] ? R[t].toFixed(0) : '—'}</span></td>` +
        `<td class="w">${st.w[t]}W</td></tr>`).join('') + `</table></div>`;
  });
  if (XGAMES.length)
    cards.push(`<div class="card"><h3>Crossover — seeds the quarters</h3>` +
               XGAMES.map(gameRow).join('') + `</div>`);
  $('#pools').innerHTML = cards.join('');
}

/* One match box. The seed chip carries the team's pool finish once it is
   known, and the feed that will fill the slot ("W QF1") while it is not.
   `place` is the grid position, `span` the height of the incoming vertical
   connector - 0 for a one-to-one feed, one row pitch for a semi, two for the
   final. */
function slot(m, r, i, place, span, opts) {
  opts = opts || {};
  const g = m.g, w = m.w, sm = S.sim[g.slot] ? ' simd' : '';
  const prev = RNDS[r-1], feed = prev && prev.games.length === 2 * RNDS[r].games.length;
  const lbl = j => feed ? 'W ' + ABBR[prev.name] + (2*i + j + 1) : 'TBD';
  const line = (x, other, j) => {
    const chip = `<span class="sd">${x ? (SEED[x] || '') : lbl(j)}</span>`;
    if (!x) return `<div class="t tbd">${chip}<span class="nm">TBD</span></div>`;
    const cls = (w ? (w === x ? 't w' + sm : 't l') : 't') + (g.done ? ' fact' : '');
    const hook = g.done ? '' : ` data-b="${g.slot}" data-w="${esc(x)}"`;
    const val = g.done ? (x === g.home ? g.hs : g.as) : (other ? pct(P(x, other)) : '');
    return `<div class="${cls}"${hook}>${chip}<span class="nm">${esc(x)}</span>` +
           `<span class="p">${val}</span></div>`;
  };
  const cin = span === null ? '' : `<i class="cin" style="--span:${span}px"></i>`;
  return `<div class="m${opts.champ ? ' champ' : ''}${opts.out ? ' out' : ''}" ` +
         `style="${place}">${cin}${line(m.home, m.away, 0)}${line(m.away, m.home, 1)}</div>`;
}

function drawBracket() {
  const B = resolve(settled);
  // Row pitch must match the CSS custom properties --mh and --rg; the grid is
  // four rows deep, so a round of n games gets a 4/n row step.
  const PITCH = 66 + 10, ROWS = 4;
  const at = (col, row, span) =>
    `grid-column:${col};grid-row:${row}${span > 1 ? ' / span ' + span : ''}`;
  const head = RNDS.map((rd, r) =>
    `<div class="round" style="${at(r+1, 1)}"><h4>${esc(rd.name)}</h4></div>`).join('');
  const cols = RNDS.map((rd, r) => {
    const step = ROWS / rd.games.length, nPrev = r ? RNDS[r-1].games.length : 0;
    const span = r === 0 ? null : (nPrev === 2 * rd.games.length ? PITCH * (ROWS / nPrev) : 0);
    return B[r].map((m, i) =>
      slot(m, r, i, at(r+1, i*step + 2, step), span,
           {out: r < FI, champ: r === FI && !!m.w})).join('');
  }).join('');
  $('#bracket').innerHTML = head + cols;
  const champ = B[FI][0] && B[FI][0].w;
  $('#champline').innerHTML = champ
    ? `Champion: <b>${esc(champ)}</b>`
    : `<span class="muted">Click through the bracket, or simulate it. Each slot ` +
      `shows the pool finish or the game that feeds it until the teams are known.</span>`;
}

function drawOdds() {
  const N = 40000, r = simulate(N);
  const rows = Object.keys(R).sort((a,b) => r.ti[b]-r.ti[a] || r.sf[b]-r.sf[a] || R[b]-R[a]);
  const top = r.ti[rows[0]] / N || 1;
  const cell = v => v ? pct(v) : '<span class="muted">—</span>';
  $('#otb').innerHTML = rows.map((t,i) =>
    `<tr><td class="rk">${i+1}</td><td>${esc(t)}</td>` +
    `<td class="n">${R[t] ? R[t].toFixed(0) : '—'}</td>` +
    `<td class="n">${cell(r.sf[t]/N)}</td><td class="n">${cell(r.fin[t]/N)}</td>` +
    `<td class="n"><b>${cell(r.ti[t]/N)}</b></td>` +
    `<td class="bar-c"><div class="oddsbar" style="width:${100*(r.ti[t]/N)/top}%"></div></td>` +
    `</tr>`).join('');
  const all = PGAMES.concat(XGAMES, ...RNDS.map(rd => rd.games));
  const done = all.filter(g => g.done).length;
  const mine = all.filter(g => !g.done && S.w[g.slot]).length;
  $('#ucount').textContent =
    `${N.toLocaleString()} simulations · ${done}/${all.length} games played` +
    (mine ? ` · ${mine} called by hand` : '');
}

function drawUS() {
  SEED = {};
  PK.forEach(k => standings(k).order.forEach((t,i) => SEED[t] = k + (i+1)));
  drawPools(); drawBracket(); drawOdds(); updateSimButtons();
}

/* ---- simulation controls ----
   "Next" means the earliest game still open in playing order: pool play and
   crossovers first, then the bracket round by round. A bracket slot is only
   playable once both its participants are known. */
function pending() {
  const pool = PGAMES.concat(XGAMES)
                     .filter(g => !settled(g, g.home, g.away))
                     .map(g => ({g, home: g.home, away: g.away}));
  if (pool.length) return {name: 'Pool play', items: pool};
  const B = resolve(settled);
  for (let r = 0; r < RNDS.length; r++) {
    const items = B[r].filter(m => m.home && m.away && !m.w);
    if (items.length) return {name: RNDS[r].name, items};
  }
  return null;
}

/* Draw one result from the model's own probability, not the favourite. */
function playOne(m) {
  S.w[m.g.slot] = Math.random() < P(m.home, m.away) ? m.home : m.away;
  S.sim[m.g.slot] = 1;
}

function updateSimButtons() {
  const p = pending(), g = $('#simGame'), r = $('#simRound');
  if (!p) {
    g.disabled = r.disabled = true;
    g.textContent = 'Simulate next game';
    r.textContent = 'Tournament complete';
    return;
  }
  g.disabled = r.disabled = false;
  const it = p.items[0];
  g.textContent = `Simulate next game — ${it.home} v ${it.away}`;
  r.textContent = `Simulate ${p.name} — ${p.items.length} game` +
                  (p.items.length === 1 ? '' : 's');
}

$('#simGame').onclick = () => {
  const p = pending(); if (!p) return;
  playOne(p.items[0]); save(); drawUS();
};
$('#simRound').onclick = () => {
  const p = pending(); if (!p) return;
  p.items.forEach(playOne);       // same round, so participants cannot shift
  save(); drawUS();
};

function call(sl, team) {
  if (S.w[sl] === team) delete S.w[sl]; else S.w[sl] = team;
  delete S.sim[sl];               // typed by hand, so no longer simulated
  save(); drawUS();
}
$('#pools').addEventListener('click', e => {
  const el = e.target.closest('[data-g]');
  if (el) call(el.dataset.g, el.dataset.w);
});
$('#bracket').addEventListener('click', e => {
  const el = e.target.closest('[data-b]');
  if (el) call(el.dataset.b, el.dataset.w);
});
$('#reset').onclick = () => { S = {w:{}, sim:{}}; save(); drawUS(); };

$('#unote').innerHTML =
  `Probabilities come from the published player-Elo model at club scale ${SCALE}, with ` +
  `each club rated off <b>the roster it registered for this event</b> — not off its last ` +
  `completed tournament. Neutral throughout: <code>home_advantage</code> is 0, so no ` +
  `seeding information enters.<br><br>` +
  `<b>Played games are read from USAU's schedule</b>, scores and all, and cannot be ` +
  `clicked away; the odds are conditioned on them. Pools are recovered from the ` +
  `results — USAU labels every opening fixture "Pool D" — as the sets of teams that ` +
  `have all played each other, which also separates out the two crossover games that ` +
  `seed the quarters. The bracket itself is no longer guessed: prequarter and quarter ` +
  `matchups are USAU's own. Only the semifinal feed is assumed, quarters 1-2 into one ` +
  `semi and 3-4 into the other.<br><br>` +
  `<b>Simulated results are dashed and marked ~.</b> The two simulate buttons draw ` +
  `each result from the model's own probability, not from the favourite — so a 55% ` +
  `game goes the other way about 45% of the time, and running the same round twice ` +
  `will not always agree. Clicking a team yourself overrides the simulated pick and ` +
  `clears the mark.<br><br>` +
  `Three-way pool ties break on rating, because point differential is not modelled. ` +
  `Warao and EVOLUTION are international entrants with no USAU history, so their ratings ` +
  `encode absence of evidence rather than measured weakness. Calls you enter are kept in ` +
  `this browser only.`;

/* ---------- drill-down: play history + rating curve ---------- */
const H = D.history || {events:[], players:{}, teams:{}, teamKey:{}};
const TK = H.teamKey || {};    // display name -> normalized club key
const TN = H.teamNames || {};  // normalized club key -> current display spelling
const CN = H.clubNames || [];  // affiliation index -> normalized club key
const ROST = H.rosters || {};  // "<clubKey>|<eventIdx>" -> delta-encoded people
const BR = H.bestRosters || {};   // clubKey -> [event, date, person deltas]
const BSEASON = H.bestSeason;     // the season BR applies to (the current one)
const PEOPLE = H.people || [], PPID = H.peoplePid || [];
const HEV = H.events;   // [date, name, season, divisionCode]
// Games behind each event, grouped by event index and stored once per game:
// [homeClubIx, awayClubIx, homeScore, awayScore, stageIx, homeDelta, awayDelta]
// against GC/GST. The deltas are what the game did to each CLUB's rating.
const GC = H.gameClubs || [], GST = H.gameStages || [], GMS = H.games || {};
const GCIX = new Map(GC.map((k, i) => [k, i]));
// Per-event division tag in the drill-down table, indexed by DIVCODE. It used
// to be a 3-slot array with an `|| 'club'` fallback, which silently labelled
// every mixed and women's event as club men's the moment those divisions
// existed. Keep this the same length as DIVCODE.
const DIVTAG = ["men's", 'college', 'D-III', 'mixed', "women's"];
/* One club's games at one event, from its own side of the net. Only the games
   the model scored are here, so an expanded event IS the Δ beside it. */
function gamesAt(ckey, evIdx) {
  const rows = GMS[evIdx], me = GCIX.get(ckey);
  if (!rows || me === undefined) return [];
  const out = [];
  for (const r of rows) {
    const home = r[0] === me;
    if (!home && r[1] !== me) continue;
    out.push({opp: GC[home ? r[1] : r[0]], mine: home ? r[2] : r[3],
              theirs: home ? r[3] : r[2], stage: GST[r[4]] || '',
              d: (home ? r[5] : r[6]) || 0});
  }
  return out;
}
// player_id -> its row in the ranked table, so a drill-down rebuilds its own
// header instead of reading it off whichever element happened to be clicked.
const PBY = new Map(D.players.map(p => [String(p[8]), p]));
const SEASONS = [...new Set(HEV.map(e => e[2]))].sort((a, b) => a - b);
const SIX = new Map(SEASONS.map((s, i) => [s, i]));
// H.teams is keyed on the lowercased model identity; never render that raw.
const clubLabel = k => TN[k] || k;

/* Stored delta-encoded: rebuild absolute event indices. `runs` (players only)
   is the run-length club affiliation: [startIdx, clubIdx] pairs positioned
   against THIS subject's own point list, clubIdx -1 meaning unresolved. */
function decode(entry) {
  if (!entry) return [];
  const deltas = entry[0], vals = entry[1], runs = entry[2] || [];
  const out = []; let i = 0, r = 0, club = '';
  for (let k = 0; k < deltas.length; k++) {
    i += deltas[k];
    while (r < runs.length && runs[r] === k) {
      club = runs[r + 1] >= 0 ? (CN[runs[r + 1]] || '') : '';
      r += 2;
    }
    const ev = HEV[i];
    if (!ev) continue;
    const v = vals[k];
    out.push({date: ev[0], event: ev[1], season: ev[2], div: ev[3], evIdx: i,
              elo: Array.isArray(v) ? v[0] : v,
              n: Array.isArray(v) ? v[1] : null, club: club});
  }
  return out;
}

const DAY = 864e5;
const asDay = s => Date.parse(s) / DAY;

/* Rating curve. x is real calendar time, so a gap in a career shows as a gap
   rather than being collapsed into an evenly-spaced sequence. */
function chart(pts) {
  if (pts.length < 2) return '<p class="muted" style="font-size:13px">' +
    'Not enough events to plot.</p>';
  const W = 700, Hh = 190, L = 44, R = 10, T = 12, B = 010 + 18;
  const xs = pts.map(p => asDay(p.date)), ys = pts.map(p => p.elo);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = Math.max(40, (y1 - y0) * 0.15); y0 -= pad; y1 += pad;
  const px = v => L + (W - L - R) * (x1 === x0 ? .5 : (v - x0) / (x1 - x0));
  const py = v => T + (Hh - T - B) * (1 - (v - y0) / (y1 - y0));
  let s = `<svg class="chart" viewBox="0 0 ${W} ${Hh}" preserveAspectRatio="none">`;
  // y gridlines at round numbers
  const step = (y1 - y0) > 900 ? 500 : (y1 - y0) > 350 ? 200 : 100;
  for (let g = Math.ceil(y0 / step) * step; g < y1; g += step) {
    s += `<line class="gl" x1="${L}" x2="${W - R}" y1="${py(g).toFixed(1)}" ` +
         `y2="${py(g).toFixed(1)}"/><text x="4" y="${(py(g) + 3).toFixed(1)}">${g}</text>`;
  }
  // year ticks
  const yr0 = new Date(pts[0].date).getUTCFullYear(),
        yr1 = new Date(pts[pts.length - 1].date).getUTCFullYear();
  for (let y = yr0; y <= yr1; y++) {
    const d = asDay(y + '-01-01');
    if (d < x0 || d > x1) continue;
    s += `<text x="${(px(d) - 12).toFixed(1)}" y="${Hh - 4}">${y}</text>`;
  }
  s += `<polyline class="ln" points="${pts.map((p,i) =>
        px(xs[i]).toFixed(1) + ',' + py(p.elo).toFixed(1)).join(' ')}"/>`;
  pts.forEach((p, i) => {
    s += `<circle class="dot" cx="${px(xs[i]).toFixed(1)}" cy="${py(p.elo).toFixed(1)}" r="2.6"/>`;
    s += `<circle class="hit" cx="${px(xs[i]).toFixed(1)}" cy="${py(p.elo).toFixed(1)}" r="9" ` +
         `data-tip="${esc(p.date + '  ' + p.elo + '  ' + p.event)}"/>`;
  });
  return s + '</svg>';
}

function histTable(pts, isTeam, ckey) {
  const rows = pts.slice().reverse().map((p, i, arr) => {
    const nxt = arr[i + 1];
    const d = nxt ? p.elo - nxt.elo : null;
    const dl = d === null ? '' :
      `<span class="${d >= 0 ? 'up' : 'dn'}">${d >= 0 ? '+' : ''}${d}</span>`;
    let mid;
    if (isTeam) {
      const rk = ckey + '|' + p.evIdx;
      mid = ROST[rk]
        ? `<td class="n"><span class="nmlink" data-roster="${esc(rk)}">` +
          `${p.n ?? ''}</span></td>`
        : `<td class="n">${p.n ?? ''}</td>`;
    } else {
      mid = `<td class="tm">` + (p.club
        ? `<span class="nmlink" data-club="${esc(p.club)}">` +
          `${esc(clubLabel(p.club))}</span>`
        : `<span class="muted">—</span>`) + `</td>`;
    }
    // The event opens onto its games — a club's own, a player's through the
    // club they turned out for. A player whose event-team never resolved to a
    // club identity has nothing to open, and neither does an event whose games
    // the model dropped.
    const gk = isTeam ? ckey : p.club;
    const ev = gk && gamesAt(gk, p.evIdx).length
      ? `<span class="disc" data-games="${esc(gk + '|' + p.evIdx)}" ` +
        `data-kind="${isTeam ? 'c' : 'p'}" data-d="${d === null ? '' : d}">` +
        `${esc(p.event)}</span>`
      : esc(p.event);
    return `<tr><td class="d">${p.date}</td>` +
           `<td>${ev}<span class="muted" style="font-size:11.5px">` +
           ` ${DIVTAG[p.div] || DIVTAG[0]}</span></td>` + mid +
           `<td class="r">${p.elo}</td><td class="dl">${dl}</td></tr>`;
  }).join('');
  return `<table class="hist"><thead><tr><th>Date</th><th>Event</th>` +
         (isTeam ? '<th class="n">Roster</th>' : '<th>Team</th>') +
         `<th class="n">Elo after</th><th class="n">Δ</th></tr></thead>` +
         `<tbody>${rows}</tbody></table>`;
}

/* The games behind one event, in the order the model replayed them, each with
   what it did to the CLUB's rating.

   Those game moves rarely add up to the Δ on the row, and the gap is the
   interesting part: a club's rating is the softmax mean of whoever took the
   field, so between two events it also moves because a different squad showed
   up (and because those players played elsewhere in between). Truck Stop's
   U.S. Open row reads +248 off +53 of actual results — the other +195 is the
   A-squad replacing the B-squad that went 2-4 at Pro Elite Challenge East.
   A club panel therefore splits the row into both parts rather than showing a
   total that looks wrong.

   In a player panel the moves are still the club's: the engine amplifies each
   game's delta by where a player sits in their provisional window, so their
   share of the same game differs from their teammates' and the row's Δ is
   their own. */
function gamesPane(ckey, evIdx, rowDelta, kind) {
  const gs = gamesAt(ckey, evIdx);
  const w = gs.filter(g => g.mine > g.theirs).length;
  const tot = gs.reduce((s, g) => s + g.d, 0);
  const sgn = v => (v >= 0 ? '+' : '') + v;
  const swing = v => v === 0
    ? `<span class="muted">0</span>`
    : `<span class="${v > 0 ? 'up' : 'dn'}">${sgn(v)}</span>`;
  const body = gs.map(g => {
    const won = g.mine > g.theirs;
    const opp = H.teams[g.opp]
      ? `<span class="nmlink" data-club="${esc(g.opp)}">` +
        `${esc(clubLabel(g.opp))}</span>`
      : esc(clubLabel(g.opp));
    return `<tr><td class="st">${esc(g.stage)}</td><td>${opp}</td>` +
           `<td class="wl ${won ? 'up' : 'dn'}">${won ? 'W' : 'L'}</td>` +
           `<td class="sc">${g.mine}–${g.theirs}</td>` +
           `<td class="dl">${swing(g.d)}</td></tr>`;
  }).join('');
  const rest = rowDelta === null ? 0 : rowDelta - tot;
  const head = `${esc(clubLabel(ckey))} · ${gs.length} game` +
    `${gs.length === 1 ? '' : 's'} · ${w}-${gs.length - w} · ` +
    (kind === 'c'
      ? `${swing(tot)} from results` +
        (Math.abs(rest) >= 1 ? ` · ${swing(rest)} from a changed roster` : '')
      : `club ${swing(tot)}`);
  return `<p class="gsum">${head}</p>` +
         `<table class="gtbl"><tbody>${body}</tbody></table>`;
}

function toggleGames(el) {
  const tr = el.closest('tr'), nx = tr.nextElementSibling;
  if (nx && nx.classList.contains('gms')) {
    nx.remove(); tr.classList.remove('open'); return;
  }
  // lastIndexOf, because a club key is free to contain the separator.
  const rk = el.dataset.games, c = rk.lastIndexOf('|');
  const d = el.dataset.d === '' ? null : +el.dataset.d;
  const row = document.createElement('tr');
  row.className = 'gms';
  row.innerHTML = `<td colspan="5">` +
    `${gamesPane(rk.slice(0, c), +rk.slice(c + 1), d, el.dataset.kind)}</td>`;
  tr.after(row);
  tr.classList.add('open');
}

/* Roster indices are delta-encoded and ascending; `people` is name-sorted, so
   ascending index is already ascending name. */
function decDeltas(enc) {
  const out = []; let i = 0;
  for (let k = 0; k < enc.length; k++) { i += enc[k]; out.push(i); }
  return out;
}
function rosterOf(rk) { return ROST[rk] ? decDeltas(ROST[rk]) : []; }
/* Names as links, or plain text for anyone below the trajectory floor: they
   were on the roster and are not dropped, there is just nothing to open. */
function nameList(ids) {
  return ids.map(i => H.players[PPID[i]]
    ? `<span class="nmlink" data-pid="${esc(PPID[i])}">${esc(PEOPLE[i])}</span>`
    : `<span class="muted">${esc(PEOPLE[i])}</span>`).join(', ');
}

/* clubKey -> [eventIdx], built once off the roster keys. lastIndexOf, because
   a club key is free to contain the separator. */
let rostByClub = null;
function rosterIndex() {
  if (rostByClub) return rostByClub;
  rostByClub = {};
  for (const rk in ROST) {
    const c = rk.lastIndexOf('|');
    const key = rk.slice(0, c);
    (rostByClub[key] || (rostByClub[key] = [])).push(+rk.slice(c + 1));
  }
  return rostByClub;
}
/* Most recent season first, and most recent event first inside it. */
function rosterSeasons(ckey) {
  const evs = rosterIndex()[ckey];
  if (!evs) return [];
  const by = new Map();
  for (const ei of evs) {
    const ev = HEV[ei];
    if (!ev) continue;
    if (!by.has(ev[2])) by.set(ev[2], []);
    by.get(ev[2]).push(ei);
  }
  return [...by.entries()].sort((a, b) => b[0] - a[0]).map(e => ({
    season: e[0],
    eis: e[1].sort((a, b) => (HEV[b][0] || '').localeCompare(HEV[a][0] || ''))}));
}
/* ONE roster per season. For past seasons that is the union of every played
   event's listed squad. For the CURRENT season it is the best full-strength
   roster reported to USAU — the same squad team_elo_best.csv rates the club
   off — which may be registered for an event not yet played; the Ev column
   then says how many of the season's played events each man was actually
   listed for, 0 meaning registered only. Clubs with no current registration
   (college teams; best rosters are club-division only) fall back to the union. */
function rosterPane(ckey, season) {
  const grp = rosterSeasons(ckey).find(g => g.season === season) || {eis: []};
  const br = season === BSEASON ? BR[ckey] : null;
  const seen = new Map();
  grp.eis.forEach(ei => rosterOf(ckey + '|' + ei).forEach(
    i => seen.set(i, (seen.get(i) || 0) + 1)));
  const ids = br ? decDeltas(br[2]) : [...seen.keys()];
  if (!ids.length) return '';
  const rows = ids.map(i => {
    const pid = PPID[i], r = PBY.get(String(pid));
    return {pid, name: PEOPLE[i], ev: seen.get(i) || 0,
            elo: r ? r[1] : null, rank: r ? r[7] : null};
  });
  // Strongest first; anyone below the rating floor has no number to sort on
  // and goes last alphabetically rather than being treated as a zero.
  rows.sort((a, b) => {
    if ((a.elo === null) !== (b.elo === null)) return a.elo === null ? 1 : -1;
    if (a.elo !== null && a.elo !== b.elo) return b.elo - a.elo;
    return a.name.localeCompare(b.name);
  });
  const body = rows.map(r =>
    `<tr><td class="rk">${r.rank === null ? '—' : r.rank}</td><td>` +
    (H.players[r.pid]
      ? `<span class="nmlink" data-pid="${esc(r.pid)}">${esc(r.name)}</span>`
      : `<span class="muted">${esc(r.name)}</span>`) +
    `</td><td class="n">${r.elo === null ? '—' : r.elo.toFixed(0)}</td>` +
    `<td class="n">${r.ev}</td></tr>`).join('');
  const sum = br
    ? `${rows.length} players — best full-strength roster reported to USAU, ` +
      `registered for ${esc(br[0])} (${br[1]}). This is the squad the ` +
      `"best" club table rates off; Ev counts the ${grp.eis.length} played ` +
      `event${grp.eis.length === 1 ? '' : 's'} this season, 0 meaning ` +
      `registered but not yet played with.`
    : `${rows.length} players · ${grp.eis.length} event` +
      `${grp.eis.length === 1 ? '' : 's'}: ` +
      grp.eis.map(ei => esc(HEV[ei][1])).join(', ');
  return `<p class="rsum">${sum}</p>` +
    `<table class="hist"><thead><tr><th class="n">#</th><th>Player</th>` +
    `<th class="n">Elo</th><th class="n" title="Played events listed for, of ` +
    `${grp.eis.length} this season">Ev</th></tr></thead>` +
    `<tbody>${body}</tbody></table>`;
}
function rosterSection(ckey) {
  const groups = rosterSeasons(ckey);
  // A club whose only current-season listing is an upcoming registration has
  // no played roster yet — the best roster alone justifies the season tab.
  if (BR[ckey] && !groups.some(g => g.season === BSEASON))
    groups.unshift({season: BSEASON, eis: []});
  if (!groups.length) return '';
  const cur = groups[0].season;
  return `<details class="rsec" data-ck="${esc(ckey)}">` +
    `<summary>Rosters by season</summary><div class="rtabs">` + groups.map(g =>
      `<button class="rtab${g.season === cur ? ' on' : ''}" ` +
      `data-season="${g.season}">${g.season}</button>`).join('') +
    `</div><div id="rpane">${rosterPane(ckey, cur)}</div></details>`;
}

function toggleRoster(el) {
  const tr = el.closest('tr'), nx = tr.nextElementSibling;
  if (nx && nx.classList.contains('rost')) { nx.remove(); return; }
  const ids = rosterOf(el.dataset.roster);
  // toggleRoster reuses nameList, so the plain-text floor rule is stated once.
  const html = nameList(ids);
  const row = document.createElement('tr');
  row.className = 'rost';
  row.innerHTML = `<td colspan="5">${ids.length} listed — ${html}</td>`;
  tr.after(row);
}

// Where the panel has been, so a roster/affiliation hop can be walked back.
let navStack = [], cur = null;

/* Self-resolving: the caller supplies only the identity, because roster links
   and history-table links have no name/elo/rank to read off. */
function openDetail(kind, key, opts) {
  opts = opts || {};
  let ckey = key, title, parts = [];
  if (kind === 'p') {
    const row = PBY.get(String(key));
    if (row) {
      title = row[0];
      parts.push(`Elo <b>${row[1].toFixed(0)}</b>`, `${row[4]} games`,
                 `#${row[7]} of ${D.totalRated.toLocaleString()} rated`);
    } else {
      const i = PPID.indexOf(String(key));
      title = i >= 0 ? PEOPLE[i] : 'Player ' + key;
    }
  } else {
    ckey = TK[key] || key;
    title = clubLabel(ckey);
    // A college team, or a club inactive in 2026, is in no basis table at all;
    // it still has a trajectory, so show that and omit the rank. Matched on
    // the model KEY, never the printed name: three divisions share names, and
    // matching "Phoenix" by name opens the men's row on the women's club.
    const tbl = D.clubs[$('#basis').value] || [];
    const row = tbl.find(r => r[6] === ckey);
    if (row) {
      const n = tbl.filter(r => r[5] === row[5]).length;
      parts.push(`Elo <b>${row[2].toFixed(0)}</b>`, `roster ${row[3]}`,
                 `#${row[0]} of ${n} ${DIVLABEL[row[5]]} clubs`);
    }
  }
  const pts = decode(kind === 'p' ? H.players[key] : H.teams[ckey]);
  const peak = pts.length ? Math.max(...pts.map(p => p.elo)) : null;
  const peakAt = pts.find(p => p.elo === peak);
  if (peak !== null) parts.push(
    `peak <b>${peak}</b> after ${esc(peakAt.event)} (${peakAt.date})`);
  $('#dbody').innerHTML =
    `<h2>${esc(title)}</h2><div class="meta">${parts.join(' · ')}</div>` + chart(pts) +
    `<p class="note" style="margin:0 0 14px">Each point is the rating after that ` +
    `event — a weekend tournament is one step, not one point per game. ` +
    `${pts.length} event${pts.length === 1 ? '' : 's'} on record; click one in ` +
    `the table for the games behind it` +
    (kind === 'p' ? ', which are the games of the club they turned out for. The ' +
     'per-game move shown there is the club\'s: the engine amplifies each ' +
     'delta by where a player sits in their provisional window, so the Δ on the ' +
     'row is their own' : '') + `.</p>` +
    (kind === 'c' ? rosterSection(ckey) : '') +
    (pts.length ? histTable(pts, kind === 'c', ckey) : '');
  if (opts.push !== false) {
    const top = navStack[navStack.length - 1];
    if (!top || top.kind !== kind || top.key !== key) navStack.push({kind, key});
  }
  cur = {kind, key};
  $('#dback').classList.toggle('on', navStack.length > 1);
  const h = '#' + kind + '/' + encodeURIComponent(key);
  if (location.hash !== h) location.hash = h;
  $('#detail').classList.add('on'); $('#scrim').classList.add('on');
}
$('#dback').onclick = () => {
  if (navStack.length < 2) return;
  navStack.pop();
  const t = navStack[navStack.length - 1];
  openDetail(t.kind, t.key, {push: false});
};
/* `silent` is for the router, which is already acting on the hash it wants.
   Otherwise closing returns to whatever the panel was opened ON TOP OF: a
   tournament view stays put rather than being torn down with the panel that
   a team name inside it opened. */
function closeDetail(silent) {
  const was = cur;
  $('#detail').classList.remove('on'); $('#scrim').classList.remove('on');
  $('#tip').classList.remove('on'); $('#dback').classList.remove('on');
  navStack = []; cur = null;
  if (silent === true || !was || !location.hash) return;
  const back = curEvent !== null ? '#t/' + EVS[curEvent][0] : '#';
  if (location.hash !== back) location.hash = back;
}
$('#dclose').onclick = () => closeDetail();
$('#scrim').onclick = () => closeDetail();
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDetail(); });

$('#detail').addEventListener('mousemove', e => {
  const h = e.target.closest('[data-tip]'); const tip = $('#tip');
  if (!h) { tip.classList.remove('on'); return; }
  tip.textContent = h.dataset.tip;
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top = (e.clientY - 26) + 'px';
  tip.classList.add('on');
});

$('#ptb').addEventListener('click', e => {
  const el = e.target.closest('[data-pid]'); if (el) openDetail('p', el.dataset.pid);
});
$('#ctb').addEventListener('click', e => {
  const el = e.target.closest('[data-club]'); if (el) openDetail('c', el.dataset.club);
});
/* Every link and disclosure inside the panel: roster-size cells, event rows,
   affiliation cells, and the people inside an expanded roster. */
$('#dbody').addEventListener('click', e => {
  const tb = e.target.closest('.rtab');
  if (tb) {
    const sec = tb.closest('.rsec');
    sec.querySelectorAll('.rtab').forEach(x => x.classList.toggle('on', x === tb));
    sec.querySelector('#rpane').innerHTML =
      rosterPane(sec.dataset.ck, +tb.dataset.season);
    return;
  }
  const g = e.target.closest('[data-games]');
  if (g) { toggleGames(g); return; }
  const r = e.target.closest('[data-roster]');
  if (r) { toggleRoster(r); return; }
  const p = e.target.closest('[data-pid]');
  if (p) { openDetail('p', p.dataset.pid); return; }
  const c = e.target.closest('[data-club]');
  if (c) openDetail('c', c.dataset.club);
});

/* ---------- tournaments: recovered pools and brackets ---------- */
/* Everything USAU does not publish about an event's shape is recovered in
   analysis/tournaments.py — pools as cliques in the co-play graph, bracket
   rounds from the stage label with the feeders wired from the results. This
   half only draws it. Games are encoded against the event's own field:
   [homeLocal, awayLocal, homeScore, awayScore, dateIndex]. */
const TV = D.tourneys || {teams: [], series: [], events: [], detail: {},
                          rounds: [], tiers: []};
const EVS = TV.events, EDET = TV.detail, ESER = TV.series, ETM = TV.teams;
const EVBYID = new Map(EVS.map((e, i) => [e[0], i]));
const EDIVL = ["Club Men's", 'College', 'College D-III', 'Club Mixed',
               "Club Women's"];
const EYEARS = [...new Set(EVS.map(e => e[2]))].sort((a, b) => b - a);
// A bracket's key is the placing it decides; 'champ' is the title.
const BRLABEL = {champ: 'Championship bracket', gtg: 'Game to go'};
const brLabel = k => BRLABEL[k] ||
  (/^\d+(st|nd|rd|th)$/.test(k) ? k + ' place bracket'
                                : k.charAt(0).toUpperCase() + k.slice(1) + ' bracket');
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
/* "Aug 1-3, 2025", collapsing the month when a weekend does not cross one. */
function daterange(a, b) {
  if (!a) return '';
  const p = s => [MON[+s.slice(5, 7) - 1], +s.slice(8, 10), s.slice(0, 4)];
  const [m1, d1, y1] = p(a);
  if (!b || b === a) return `${m1} ${d1}, ${y1}`;
  const [m2, d2, y2] = p(b);
  if (y1 !== y2) return `${m1} ${d1}, ${y1} \u2013 ${m2} ${d2}, ${y2}`;
  return m1 === m2 ? `${m1} ${d1}\u2013${d2}, ${y1}`
                   : `${m1} ${d1} \u2013 ${m2} ${d2}, ${y1}`;
}

/* ---------- list ---------- */
/* Opens on the most recent season — "what happened lately" is the question
   this tab is usually asked. `selected` is explicit: relying on a browser
   auto-selecting the first option makes the default a side effect of the
   order the options happened to be inserted in. */
$('#eyear').innerHTML = '<option value="all">All years</option>' +
  EYEARS.map((y, i) => `<option value="${y}"${i ? '' : ' selected'}>${y}</option>`).join('');

function drawEvents() {
  const q = $('#eq').value.trim().toLowerCase(), div = $('#ediv').value;
  const yr = $('#eyear').value, tier = $('#etier').value;
  let rows = EVS;
  if (div !== 'all') rows = rows.filter(e => e[3] === +div);
  if (yr !== 'all') rows = rows.filter(e => e[2] === +yr);
  if (tier === 'series') rows = rows.filter(e => e[8] > 0);
  else if (tier !== 'all') rows = rows.filter(e => e[8] === +tier);
  if (q) rows = rows.filter(e => e[1].toLowerCase().includes(q) ||
                                 e[6].toLowerCase().includes(q) ||
                                 ESER[e[9]][0].toLowerCase().includes(q));
  const total = rows.length;
  // Most recent first. The cap is a DOM budget, not a filter: the count says
  // how many matched so a narrower search is an obvious next move.
  rows = rows.slice().sort((a, b) => (b[4] || '').localeCompare(a[4] || ''));
  const shown = rows.slice(0, 400);
  $('#etb').innerHTML = shown.map(e => {
    const i = EVBYID.get(e[0]), det = EDET[i], n = ESER[e[9]][1].length;
    const ch = e[10] >= 0 ? `<span class="crown">${esc(ETM[det.t[e[10]]])}</span>`
                          : '<span class="muted">\u2014</span>';
    // Division and championship tier are separate facts, so they get separate
    // marks: colouring the division tag by tier read as if "Club Men's" itself
    // meant something. Only Regionals and up carry a chip — Sectionals and
    // Conference are most of the corpus, and the filter already finds them.
    const chip = e[8] >= 3
      ? `<span class="tag t${e[8]}">${esc(TV.tiers[e[8]])}</span> ` : '';
    return `<tr class="ev" data-ev="${e[0]}"><td class="dt">${daterange(e[4], e[5])}</td>` +
      `<td>${chip}${esc(e[1])}` +
      `${e[6] ? ` <span class="muted">\u00b7 ${esc(e[6])}</span>` : ''}</td>` +
      `<td><span class="tag">${esc(EDIVL[e[3]] || '')}</span></td>` +
      `<td class="n">${e[7]}</td><td>${ch}</td>` +
      `<td class="n">${n > 1 ? n : '<span class="muted">1</span>'}</td></tr>`;
  }).join('');
  $('#ecount').textContent = `${total.toLocaleString()} tournament` +
    (total === 1 ? '' : 's') + (total > shown.length
      ? ` \u00b7 showing the ${shown.length} most recent` : '');
}
['#eq', '#ediv', '#eyear', '#etier'].forEach(s => {
  const el = $(s);
  el.oninput = drawEvents; el.onchange = drawEvents;
});
$('#etb').addEventListener('click', e => {
  const tr = e.target.closest('[data-ev]');
  if (tr) location.hash = '#t/' + tr.dataset.ev;
});

/* ---------- standings ---------- */
/* Wins first, then the head-to-head record INSIDE the tied group, then point
   differential. Unlike the U.S. Open tracker — which prices games that have
   not happened and so cannot use a margin — every game here has a score, so
   the real tiebreakers are available and rating never enters. */
function standingsOf(games, teams) {
  const st = new Map(teams.map(t => [t, {t, w: 0, l: 0, pf: 0, pa: 0}]));
  games.forEach(g => {
    const h = st.get(g[0]), a = st.get(g[1]);
    h.pf += g[2]; h.pa += g[3]; a.pf += g[3]; a.pa += g[2];
    if (g[2] > g[3]) { h.w++; a.l++; } else if (g[3] > g[2]) { a.w++; h.l++; }
  });
  const rows = [...st.values()];
  rows.forEach(r => r.pd = r.pf - r.pa);
  rows.sort((x, y) => (y.w - x.w) || (y.pd - x.pd) || (y.pf - x.pf));
  // Re-sort each equal-win block on the games its members played each other.
  for (let i = 0; i < rows.length; ) {
    let j = i;
    while (j < rows.length && rows[j].w === rows[i].w) j++;
    if (j - i > 1) {
      const grp = new Set(rows.slice(i, j).map(r => r.t)), h2h = new Map();
      grp.forEach(t => h2h.set(t, 0));
      games.forEach(g => {
        if (!grp.has(g[0]) || !grp.has(g[1]) || g[2] === g[3]) return;
        const w = g[2] > g[3] ? g[0] : g[1];
        h2h.set(w, h2h.get(w) + 1);
      });
      const blk = rows.slice(i, j).sort((x, y) =>
        (h2h.get(y.t) - h2h.get(x.t)) || (y.pd - x.pd) || (y.pf - x.pf));
      rows.splice(i, j - i, ...blk);
    }
    i = j;
  }
  return rows;
}

/* ---------- one tournament ---------- */
let curEvent = null;

function evTeamCell(name) {
  // Clubs the model tracks open their trajectory; anyone else is plain text.
  // Matched on the model KEY, since three divisions share printed names.
  const k = TK[name];
  return k && H.teams[k]
    ? `<span class="nmlink" data-club="${esc(k)}">${esc(name)}</span>` : esc(name);
}

function drawTournament(i) {
  const e = EVS[i], det = EDET[i];
  if (!det) return;
  const nm = l => ETM[det.t[l]];
  const ser = ESER[e[9]], sibs = ser[1];
  const facts = [daterange(e[4], e[5]), e[6], EDIVL[e[3]], TV.tiers[e[8]],
                 `${e[7]} teams`].filter(Boolean).map(esc);
  facts.push(e[10] >= 0 ? `champion <b>${esc(nm(e[10]))}</b>`
                        : 'no champion on record');
  $('#tvhead').innerHTML = `<h2>${esc(e[1])}</h2>` +
    `<div class="meta">${facts.join(' \u00b7 ')}</div>`;

  let html = '';
  // Pools, lettered in playing order. A pool whose teams have already played
  // in an earlier one is a placement round robin, and says so.
  if (det.p.length) {
    html += `<h3 class="sect">Pool play <span class="muted">\u2014 recovered ` +
      `from the results; USAU publishes no pool labels worth the name</span></h3>` +
      `<div class="grid">` + det.p.map(([later, gs], pi) => {
        const teams = [...new Set(gs.flatMap(g => [g[0], g[1]]))];
        const st = standingsOf(gs, teams);
        return `<div class="card"><h3>Pool ${POOLTAG[pi]}` +
          (later ? ' \u2014 placement' : '') + `</h3>` +
          gs.map(g => evGameRow(g, nm)).join('') +
          `<table class="stand">` + st.map((r, j) =>
            `<tr><td><span class="seed">${POOLTAG[pi]}${j + 1}</span>` +
            `${evTeamCell(nm(r.t))}</td><td class="w">${r.w}\u2013${r.l}</td>` +
            `<td class="pd">${r.pd > 0 ? '+' : ''}${r.pd}</td></tr>`).join('') +
          `</table></div>`;
      }).join('') + `</div>`;
  }
  // A bracket of one game is a placement decider, not a bracket. Drawing it
  // as a four-row grid headed "Final" wastes a section on "3rd place: DiG
  // beat Mooncatchers", so the singletons collapse into one table alongside
  // whatever the label placed nowhere at all.
  const gcount = b => b[2].reduce((n, rd) => n + rd.filter(Boolean).length, 0);
  const drawn = det.b.filter(b => gcount(b) > 1);
  const single = det.b.filter(b => gcount(b) === 1);
  html += drawn.map(b => evBracket(b[0], b[1], b[2], nm, det)).join('');

  const extra = single.map(([kind, root, rounds]) => {
    const g = rounds[rounds.length - 1].find(Boolean);
    return g ? [g, brLabel(kind).replace(/ bracket$/, '')] : null;
  }).filter(Boolean).concat(det.o);
  if (extra.length) {
    html += `<h3 class="sect">Placement &amp; other games <span class="muted">` +
      `\u2014 one-game deciders, crossovers, play-ins, and anything the ` +
      `organiser's own label does not put in a bracket</span></h3>` +
      `<table><thead><tr><th>Round</th><th>Result</th>` +
      `<th class="n">Score</th></tr></thead><tbody>` +
      extra.map(([g, stage]) => {
        const hw = g[2] >= g[3];
        return `<tr><td class="muted">${esc(stage || '\u2014')}</td><td>` +
          `${evTeamCell(nm(g[hw ? 0 : 1]))} ` +
          `<span class="muted">${g[2] === g[3] ? 'tied' : 'def.'}</span> ` +
          `${evTeamCell(nm(g[hw ? 1 : 0]))}</td>` +
          `<td class="n">${Math.max(g[2], g[3])}\u2013${Math.min(g[2], g[3])}</td></tr>`;
      }).join('') + `</tbody></table>`;
  }
  if (!det.p.length && !det.b.length && !det.o.length)
    html += `<p class="muted">No completed games on record for this event.</p>`;

  // The series history. Shown whenever there is more than this instance —
  // across seasons AND across divisions, since a Sectional's men's and
  // women's halves are one tournament run twice on the same weekend.
  if (sibs.length > 1) {
    html += `<h3 class="sect">${esc(ser[0])} <span class="muted">\u2014 ` +
      `${sibs.length} instances on record</span></h3>` +
      `<table class="evtbl"><thead><tr><th class="n">Year</th><th>Division</th>` +
      `<th>Event</th><th class="n">Teams</th><th>Champion</th></tr></thead><tbody>` +
      sibs.slice().reverse().map(j => {
        const s = EVS[j], sd = EDET[j];
        const ch = s[10] >= 0 ? `<span class="crown">${esc(ETM[sd.t[s[10]]])}</span>`
                              : '<span class="muted">\u2014</span>';
        return `<tr class="ev" data-ev="${s[0]}"${j === i ? ' style="font-weight:600"' : ''}>` +
          `<td class="n">${s[2]}</td><td><span class="tag">` +
          `${esc(EDIVL[s[3]] || '')}</span></td><td>${esc(s[1])}</td>` +
          `<td class="n">${s[7]}</td><td>${ch}</td></tr>`;
      }).join('') + `</tbody></table>`;
  }
  $('#tvbody').innerHTML = html;
  $('#tvnote').innerHTML =
    `USAU publishes a flat fixture list per event and nothing about its shape, ` +
    `so the shape here is <b>recovered</b>. A pool is a set of teams that have ` +
    `all played each other, found as a clique in the co-play graph and chosen ` +
    `to span the fewest calendar days — that is what separates an opening pool ` +
    `of three from a placement pool of four reusing two of its teams. Bracket ` +
    `ROUNDS come from the organiser's own stage label, never from the results: ` +
    `a win-chain through mislabelled pool play looks exactly like a nine-round ` +
    `bracket, and showing none beats inventing one. Only the feeders are ` +
    `inferred, a slot reading back to the game that team won. Anything the ` +
    `label does not place lands in Other games under whatever it was called. ` +
    `Standings break ties on head-to-head inside the tied group, then point ` +
    `differential. Team names in <span class="nmlink">this style</span> open ` +
    `that club's rating history.`;
  curEvent = i;
}
const POOLTAG = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');

function evGameRow(g, nm) {
  const hw = g[2] > g[3], tie = g[2] === g[3];
  const side = (l, sc, win) =>
    `<div class="t ${tie ? '' : win ? 'w' : 'l'} fact"><span>${esc(nm(l))}</span>` +
    `<span class="p">${sc}</span></div>`;
  return `<div class="g">${side(g[0], g[2], hw)}<span class="vs">v</span>` +
         `${side(g[1], g[3], !hw)}</div>`;
}

/* One bracket as a CSS grid. Rounds arrive outermost first and each is exactly
   twice the next, so a round of n games sits on a row step of ROWS/n and the
   incoming connector spans one pitch of the round before it.

   `root` is the rank the innermost column actually reached — 0 for a bracket
   that played its final, 1 for a Regional that stopped at two semifinals. The
   column headings count out from there, so a bracket with no final is headed
   Quarterfinals/Semifinals rather than having its rounds shifted in to
   manufacture one. */
function evBracket(kind, root, rounds, nm, det) {
  const MH = 50, RG = 8, PITCH = MH + RG;
  const ROWS = rounds[0].length, cols = rounds.length;
  const seed = evSeeds(det);
  const at = (c, r, span) =>
    `grid-column:${c};grid-row:${r}${span > 1 ? ' / span ' + span : ''}`;
  const head = rounds.map((rd, r) => {
    const rank = root + cols - 1 - r;
    return `<div class="round" style="${at(r + 1, 1)}"><h4>` +
      `${esc(TV.rounds[rank] || 'Round of ' + (2 << rank))}</h4></div>`;
  }).join('');
  const body = rounds.map((rd, r) => {
    const step = ROWS / rd.length;
    const span = r === 0 ? 0 : PITCH * (ROWS / rounds[r - 1].length);
    return rd.map((g, i) => {
      if (!g) return '';
      const line = (l, sc, other, win) =>
        `<div class="t ${sc === other ? '' : win ? 'w' : 'l'}">` +
        `<span class="sd">${seed.get(l) || ''}</span>` +
        `<span class="nm">${evTeamCell(nm(l))}</span><span class="p">${sc}</span></div>`;
      return `<div class="m${r < cols - 1 ? ' out' : ''}" style="${at(r + 1, i * step + 2, step)}">` +
        (span ? `<i class="cin" style="--span:${span}px"></i>` : '') +
        line(g[0], g[2], g[3], g[2] > g[3]) + line(g[1], g[3], g[2], g[3] > g[2]) +
        `</div>`;
    }).join('');
  }).join('');
  return `<h3 class="sect">${esc(brLabel(kind))}</h3>` +
    `<div class="tbr" style="grid-template-columns:repeat(${cols},minmax(150px,1fr));` +
    `grid-template-rows:auto repeat(${ROWS},${MH}px)">${head}${body}</div>`;
}

/* A team's pool finish, for the chip on each bracket line: which slot walked
   out of pool play into this game. */
function evSeeds(det) {
  const out = new Map();
  det.p.forEach(([later, gs], pi) => {
    if (later) return;
    const teams = [...new Set(gs.flatMap(g => [g[0], g[1]]))];
    standingsOf(gs, teams).forEach((r, j) => {
      if (!out.has(r.t)) out.set(r.t, POOLTAG[pi] + (j + 1));
    });
  });
  return out;
}

function openTournament(eid) {
  const i = EVBYID.get(eid);
  if (i === undefined) { closeTournament(); return; }
  if (curEvent !== i) drawTournament(i);
  $('#tlist').style.display = 'none';
  $('#tview').style.display = '';
  showTab('events');
  window.scrollTo(0, 0);
}
function closeTournament() {
  curEvent = null;
  $('#tview').style.display = 'none';
  $('#tlist').style.display = '';
}
$('#eback').onclick = () => { location.hash = '#'; };
$('#tvbody').addEventListener('click', ev => {
  const c = ev.target.closest('[data-club]');
  if (c) { openDetail('c', c.dataset.club); return; }
  const tr = ev.target.closest('[data-ev]');
  if (tr) location.hash = '#t/' + tr.dataset.ev;
});

/* ---------- deep links ---------- */
// Club keys carry spaces and punctuation ('rhino slam!', 'cookie mon$terz'),
// so the key is percent-encoded rather than concatenated raw.
const known = (kind, key) => kind === 'p'
  ? (!!H.players[key] || PBY.has(String(key)))
  : !!H.teams[TK[key] || key];
function routeHash() {
  const m = /^#([pct])\/(.+)$/.exec(location.hash || '');
  // A tournament is a VIEW, not the overlay panel: it owns the tab, and any
  // club panel opened from inside it is layered on top and closed first.
  if (m && m[1] === 't') {
    if (cur) closeDetail(true);
    openTournament(+m[2]);
    return;
  }
  // Only a bare hash tears the tournament down. A club panel opened from
  // inside one is layered ON it and must leave it standing.
  if (!m) {
    if (cur) closeDetail(true);
    if (curEvent !== null) closeTournament();
    return;
  }
  let key;
  try { key = decodeURIComponent(m[2]); } catch (err) { key = m[2]; }
  if (cur && cur.kind === m[1] && cur.key === key) return;
  if (!known(m[1], key)) { closeDetail(true); return; }
  openDetail(m[1], key);
}
window.addEventListener('hashchange', routeHash);

/* ---------- trends: season charts, everyone who reached a top 25 ---------- */
// Eight hues x eight dash patterns = 64 distinguishable strokes. The union of
// every season's top 25 runs to 164 series in the densest view, so past 64 a
// combination repeats and the legend, not the stroke, is the identifier.
const DASH = ['none', '5 3', '2 3', '8 3 2 3',
              '12 4', '1 3', '7 3 1 3', '3 3 9 3'];
const TOPN = 25;   // the per-season cut a subject must have made, once, ever
// Codes match the DIVCODE written into each event by analysis.rankings.
const DIVLABEL = {all: 'all divisions', 0: "club men's", 1: 'college',
                  2: 'college D-III', 3: 'club mixed', 4: "club women's"};
// Codes match the payload's gender map: 1 male-matching, 2 female-matching.
const GENLABEL = {all: '', 1: ' male-matching', 2: ' female-matching'};
const GEN = D.genders || {};
const trendCache = {};

function playerLabel(pid) {
  const row = PBY.get(String(pid));
  if (row) return row[0];
  const i = PPID.indexOf(String(pid));
  return i >= 0 ? PEOPLE[i] : String(pid);
}

/* One value per season: the rating after that season's LAST event. Points are
   already chronological, so a plain overwrite lands on the last one. */
function seasonData(kind, div, gen) {
  const ck = kind + '|' + div + '|' + gen;
  if (trendCache[ck]) return trendCache[ck];
  const src = kind === 'p' ? H.players : H.teams;
  const dv = div === 'all' ? null : +div;
  // Gender selects whole SUBJECTS, unlike division, which selects points: a
  // person does not change gender-matching group between events, and clubs
  // have no group at all, so the filter is inert on the club side.
  const gv = (gen === 'all' || kind !== 'p') ? null : +gen;
  const all = [];
  for (const key in src) {
    if (gv !== null && GEN[key] !== gv) continue;
    const pts = decode(src[key]);
    if (!pts.length) continue;
    const vals = SEASONS.map(() => null);
    for (const p of pts) {
      // A division selects POINTS, never whole subjects: 301 club keys play in
      // more than one division, so any per-subject verdict misfiles all of
      // them. Within a division the season value is the rating after that
      // season's last event IN that division, and a subject with no event
      // there drops out of the population entirely.
      if (dv !== null && p.div !== dv) continue;
      const si = SIX.get(p.season);
      if (si !== undefined) vals[si] = p.elo;
    }
    let peak = -Infinity;
    for (const v of vals) if (v !== null && v > peak) peak = v;
    if (peak === -Infinity) continue;
    all.push({key, vals, peak});
  }
  // Median over the WHOLE population, not the drawn 25: the mode answers "how
  // far above a typical subject", and the top 25 are typical of nothing.
  const med = SEASONS.map((_, i) => {
    const v = all.map(a => a.vals[i]).filter(x => x !== null).sort((a, b) => a - b);
    if (!v.length) return 0;
    const h = v.length >> 1;
    return v.length % 2 ? v[h] : (v[h - 1] + v[h]) / 2;
  });
  // Every subject that has ever CLOSED a season inside the top 25 of its
  // population, which is 67-164 lines rather than 25: a club that owned 2019
  // and has since folded is on the chart beside this year's best. The cut is
  // the 25th value, compared with >=, so a tie is never broken arbitrarily —
  // a season can contribute 26. A season with fewer than 25 active subjects
  // qualifies all of them.
  const cut = SEASONS.map((_, i) => {
    const v = all.map(a => a.vals[i]).filter(x => x !== null).sort((x, y) => y - x);
    return v.length ? v[Math.min(TOPN, v.length) - 1] : null;
  });
  const top = all.filter(a => SEASONS.some((_, i) =>
    a.vals[i] !== null && cut[i] !== null && a.vals[i] >= cut[i]));
  // Ordered on the CURRENT season, so the series index — and with it the
  // colour and dash — matches the default legend order and the strongest few
  // get the eight solid hues. Ordering by one season's raw value is identical
  // to ordering by that season's median-adjusted value, the median being a
  // constant per season, so this holds in both modes. All-time peak only
  // breaks ties among subjects that did not play it.
  const LAST = SEASONS.length - 1;
  top.sort((a, b) => {
    const x = a.vals[LAST], y = b.vals[LAST];
    if ((x === null) !== (y === null)) return x === null ? 1 : -1;
    if (x !== null && x !== y) return y - x;
    return b.peak - a.peak;
  });
  top.forEach(s => s.label = kind === 'p' ? playerLabel(s.key) : clubLabel(s.key));
  trendCache[ck] = {top, med, n: all.length};
  return trendCache[ck];
}

function trendChart(series) {
  const W = 900, Hh = 460, T = 18, Rm = 12, B = 34, L = 52;
  const n = SEASONS.length;
  const vof = seriesVal;
  let y0 = Infinity, y1 = -Infinity;
  series.forEach(s => SEASONS.forEach((_, i) => {
    const v = vof(s, i);
    if (v === null) return;
    if (v < y0) y0 = v;
    if (v > y1) y1 = v;
  }));
  if (!isFinite(y0)) return '<p class="muted" style="font-size:13px">Nothing to plot.</p>';
  const pad = (y1 - y0) * 0.06 || 40; y0 -= pad; y1 += pad;
  const px = i => L + (W - L - Rm) * (n > 1 ? i / (n - 1) : 0.5);
  const py = v => T + (Hh - T - B) * (1 - (v - y0) / (y1 - y0));
  let s = `<svg class="tchart" id="tsvg" viewBox="0 0 ${W} ${Hh}" ` +
          `preserveAspectRatio="none"` +
          (series.length > 40 ? ` data-dense=""` : ``) + `>`;
  const step = curMode === 'med' ? 100 : 200;
  for (let g = Math.ceil(y0 / step) * step; g < y1; g += step) {
    const y = py(g).toFixed(1);
    s += `<line class="gl" x1="${L}" x2="${W - Rm}" y1="${y}" y2="${y}"/>` +
         `<text x="4" y="${(py(g) + 3).toFixed(1)}">${g}</text>`;
  }
  s += `<line class="ax" x1="${L}" x2="${W - Rm}" y1="${Hh - B}" y2="${Hh - B}"/>`;
  SEASONS.forEach((yr, i) => {
    s += `<text class="sx" x="${px(i).toFixed(1)}" y="${Hh - B + 16}">${yr}</text>`;
  });
  // Per-season hit columns and guides, emitted BEFORE the series so a line
  // still wins the hover wherever one passes: over a line you isolate that
  // series, over empty space you reorder the legend by that season.
  const half = (W - L - Rm) / (n > 1 ? 2 * (n - 1) : 2);
  SEASONS.forEach((_, i) => {
    const a = Math.max(L, px(i) - half), b = Math.min(W - Rm, px(i) + half);
    s += `<rect class="xh" data-yi="${i}" x="${a.toFixed(1)}" y="${T}" ` +
         `width="${(b - a).toFixed(1)}" height="${(Hh - T - B).toFixed(1)}"/>` +
         `<line class="yg" data-yi="${i}" x1="${px(i).toFixed(1)}" ` +
         `x2="${px(i).toFixed(1)}" y1="${T}" y2="${Hh - B}"/>`;
  });
  series.forEach((sr, si) => {
    const dash = DASH[Math.floor(si / 8) % DASH.length];
    s += `<g class="sg" data-series="${si}" style="color:var(--s${si % 8 + 1})"` +
         (dash === 'none' ? '' : ` stroke-dasharray="${dash}"`) + `>`;
    // A missing season BREAKS the line: one polyline per contiguous run. SVG
    // has no null point, and interpolating across a year a club did not play
    // would invent a season it never had.
    let run = [];
    const flush = () => {
      if (run.length > 1) {
        s += `<polyline points="${run.join(' ')}"/>` +
             `<polyline class="hit" stroke-dasharray="none" points="${run.join(' ')}"/>`;
      } else if (run.length === 1) {
        const xy = run[0].split(',');
        s += `<circle cx="${xy[0]}" cy="${xy[1]}" r="2.4"/>`;
      }
      run = [];
    };
    SEASONS.forEach((_, i) => {
      const v = vof(sr, i);
      if (v === null) flush();
      else run.push(px(i).toFixed(1) + ',' + py(v).toFixed(1));
    });
    flush();
    s += `</g>`;
  });
  return s + `</svg>`;
}

let hotIdx = null, pinIdx = null, yearIdx = null;
// The season the legend falls back to: the most recent one. Hovering moves it,
// leaving the chart returns here rather than to an all-time ranking.
const DEFYEAR = SEASONS.length - 1;
// The chart, its baseline and its mode, so the legend can be re-sorted against
// a season without re-deriving anything.
let curSeries = [], curMed = [], curMode = 'elo';
const seriesVal = (s, i) => s.vals[i] === null ? null
  : (curMode === 'med' ? s.vals[i] - curMed[i] : s.vals[i]);

function drawTrends() {
  const kind = $('#tsub').value, mode = $('#tmode').value, div = $('#tdiv').value;
  const gen = $('#tgen').value;
  // Clubs carry no gender-matching group, so the control is disabled rather
  // than silently ignored when the subject is clubs.
  $('#tgen').disabled = kind !== 'p';
  const dat = seasonData(kind, div, gen);
  hotIdx = null; pinIdx = null; yearIdx = null;
  curMode = mode; curMed = dat.med; curSeries = dat.top;
  // Only used to break ties between subjects with no value in the ranked
  // season, which puts the better historical side first among the em dashes.
  curSeries.forEach(s => {
    let m = -Infinity;
    SEASONS.forEach((_, i) => {
      const v = seriesVal(s, i);
      if (v !== null && v > m) m = v;
    });
    s.best = m;
  });
  $('#tchart').innerHTML = trendChart(curSeries);
  $('#tlegend').innerHTML = curSeries.map((sr, i) => {
    const dash = DASH[Math.floor(i / 8) % DASH.length];
    const link = kind === 'p' ? `data-pid="${esc(sr.key)}"`
                              : `data-club="${esc(sr.key)}"`;
    return `<div class="lrow" data-series="${i}" style="color:var(--s${i % 8 + 1})">` +
      `<svg class="sw" viewBox="0 0 14 3"><line x1="0" y1="1.5" x2="14" y2="1.5" ` +
      `stroke="currentColor" stroke-width="3"` +
      (dash === 'none' ? '' : ` stroke-dasharray="${dash}"`) + `/></svg>` +
      `<span class="lbl nmlink" ${link} title="${esc(sr.label)}">` +
      `${esc(sr.label)}</span><span class="pk"></span></div>`;
  }).join('');
  $('#tlegend').classList.remove('dim');
  setYear(DEFYEAR);
  $('#tcount').textContent = `${curSeries.length} of ` +
    `${dat.n.toLocaleString()} ${kind === 'p' ? 'players' : 'clubs'} have closed ` +
    `a season in the top 25 · ${DIVLABEL[div]}${kind === 'p' ? GENLABEL[gen] : ''} · ` +
    `${SEASONS[0]}–${SEASONS[SEASONS.length - 1]}`;
}

/* Re-sorts the legend against the ranked season — whichever is hovered, or the
   most recent by default. Rows are MOVED, never rebuilt: data-series stays put,
   so a line keeps its colour and the hover machinery keeps working. One
   fragment append, so one reflow rather than 164. */
function orderLegend() {
  const leg = $('#tlegend'), rows = [...leg.children];
  if (!rows.length) return;
  const i = yearIdx === null ? DEFYEAR : yearIdx;
  rows.sort((a, b) => {
    const x = curSeries[+a.dataset.series], y = curSeries[+b.dataset.series];
    const vx = seriesVal(x, i), vy = seriesVal(y, i);
    // A subject that did not play that season sinks to the bottom rather than
    // sorting as zero, which would rank a club that skipped the year above one
    // that played badly.
    if (vx === null || vy === null) {
      if (vx === vy) return y.best - x.best;
      return vx === null ? 1 : -1;
    }
    if (vx !== vy) return vy - vx;
    return +a.dataset.series - +b.dataset.series;
  });
  const frag = document.createDocumentFragment();
  rows.forEach(r => {
    const v = seriesVal(curSeries[+r.dataset.series], i);
    const pk = r.querySelector('.pk');
    pk.textContent = v === null ? '—'
      : (curMode === 'med' && v > 0 ? '+' : '') + Math.round(v);
    pk.classList.toggle('na', v === null);
    frag.appendChild(r);
  });
  leg.appendChild(frag);
  $('#tlhead').textContent = `${rows.length} series · ranked on ${SEASONS[i]}` +
    (i === DEFYEAR ? ' (current)' : '');
}

function setYear(i) {
  if (i === yearIdx) return;
  const svg = $('#tsvg');
  const guide = j => svg && svg.querySelector(`.yg[data-yi="${j}"]`);
  if (yearIdx !== null) { const g = guide(yearIdx); if (g) g.classList.remove('on'); }
  yearIdx = i;
  if (i !== null) { const g = guide(i); if (g) g.classList.add('on'); }
  orderLegend();
}

// Two element writes plus one class on each root, never 25 inline styles.
const seriesEls = i => [
  $('#tsvg') && $('#tsvg').querySelector('.sg[data-series="' + i + '"]'),
  $('#tlegend').querySelector('.lrow[data-series="' + i + '"]')];
function setHot(i) {
  if (i === hotIdx) return;
  if (hotIdx !== null) seriesEls(hotIdx).forEach(e => e && e.classList.remove('hot'));
  hotIdx = i;
  if (i !== null) seriesEls(i).forEach(e => e && e.classList.add('hot'));
  const svg = $('#tsvg');
  if (svg) svg.classList.toggle('dim', i !== null);
  $('#tlegend').classList.toggle('dim', i !== null);
}
$('#tlegend').addEventListener('mouseover', e => {
  const r = e.target.closest('.lrow'); if (r) setHot(+r.dataset.series);
});
$('#tlegend').addEventListener('mouseleave', () => setHot(pinIdx));
$('#tchart').addEventListener('mouseover', e => {
  const g = e.target.closest('.sg');
  if (g) { setHot(+g.dataset.series); return; }
  // Background means a season column: drop any line isolation and rank on it.
  const h = e.target.closest('.xh');
  if (h) { setHot(pinIdx); setYear(+h.dataset.yi); }
});
$('#tchart').addEventListener('mouseleave',
  () => { setHot(pinIdx); setYear(DEFYEAR); });
$('#tlegend').addEventListener('click', e => {
  const lab = e.target.closest('[data-pid],[data-club]');
  if (lab) {
    if (lab.dataset.pid) openDetail('p', lab.dataset.pid);
    else openDetail('c', lab.dataset.club);
    return;
  }
  const r = e.target.closest('.lrow'); if (!r) return;
  const i = +r.dataset.series;
  pinIdx = pinIdx === i ? null : i;
  setHot(pinIdx);
});
$('#tsub').onchange = drawTrends;
$('#tdiv').onchange = drawTrends;
$('#tgen').onchange = drawTrends;
$('#tmode').onchange = drawTrends;
$('#tnote').textContent =
  `One point per season: the rating after that season's last event. Every subject ` +
  `that has ever closed a season inside the top 25 gets a line — 67 to 164 of them ` +
  `depending on the view, which is why the field thins out and hovering is how you ` +
  `read an individual. The legend is ranked on the most recent season, not on an ` +
  `all-time peak, so it opens as the current table; hover any other season in the ` +
  `chart and it re-ranks on that one, showing that season's rating, and leaving ` +
  `returns it to the current season. A subject that did not play the ranked ` +
  `season shows an em dash and sinks to the bottom rather than sorting as zero. ` +
  `Hover a line or a legend row to isolate it, click the row to pin, click the ` +
  `name to open its full history. A season a subject did not play breaks the line ` +
  `rather than being interpolated across. "Above season median" subtracts the ` +
  `median of every rated subject active that season. Narrowing the division keeps ` +
  `only events in it, so a season reads as the rating after that season's last ` +
  `event in that division — 301 club identities play in more than one, and each ` +
  `is ranked on its own record in whichever division you are looking at. Gender ` +
  `works the other way and selects whole people, since nobody changes group ` +
  `between events; it is inert for clubs. Both narrow the population, so the top ` +
  `25 is recomputed inside whatever you have selected. All five divisions share ` +
  `one rating scale, bridged by the ${GENDER_NOTE}`;

$('#enote').innerHTML =
  `Every event in the corpus with a completed game, ${EVS.length.toLocaleString()} of ` +
  `them across ${ESER.length.toLocaleString()} tournament series. Click a row for ` +
  `that event's pools and bracket, and for the other years the same tournament ` +
  `has run. <b>Editions</b> counts the instances on record — the same tournament ` +
  `in another division counts, since a Sectional's men's and women's halves are ` +
  `one weekend run twice. The champion is the winner of the championship ` +
  `bracket's final where the schedule names one; events that finished on pool ` +
  `play, or whose stage labels name no final, show a dash.`;

drawClubs(); drawPlayers(); drawUS(); drawEvents(); routeHash();
</script>
</html>
"""


if __name__ == "__main__":
    build()
