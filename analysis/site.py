"""Build a single self-contained HTML page for the published rankings.

Three tabs: club rankings, player rankings, and a U.S. Open tracker whose
bracket you fill in as games finish, re-simulating the title odds live.

Everything is embedded in one file so it opens over file:// with no server
and no network. Inputs are the published artifacts only - this script never
replays the model, so the page can never disagree with the CSVs:

    data/player_elo.csv          player table (>= MIN_GAMES shown)
    data/team_elo.csv            clubs, most recent COMPLETED event roster
    data/team_elo_best.csv       clubs, best full-strength roster of 2026
    data/team_elo_upcoming.csv   clubs, next event roster - the U.S. Open field
    data/usau.db                 the U.S. Open schedule

Usage: python -m analysis.site   ->   docs/index.html
"""

import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH
from analysis.rankings import PUBLISHED

# docs/ rather than site/: GitHub Pages can serve a branch's root or its
# /docs folder and nothing else, so putting the page here makes the project
# URL itself the app. The accompanying .nojekyll stops Pages running the
# output through Jekyll, which is pure overhead for a single static file.
OUT = DB_PATH.parent.parent / "docs" / "index.html"

# The player table's display floor, matching the ranking convention: below 30
# games a rating still sits inside the engine's provisional window.
MIN_GAMES = 30

# The U.S. Open field is 12 teams in 4 pools of 3. Every row of the schedule
# carries a mislabelled "Pool D" stage, so pools are derived from the co-play
# graph instead: each team plays exactly its own pool.
USOPEN_EVENT = "%U.S. Open%"


def load_csv(name):
    p = DB_PATH.parent / name
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def usopen(con):
    """(event row, [team names], [(date, time, home, away)]) for the men's ICC."""
    ev = con.execute(
        """SELECT event_id, name, start_date, end_date FROM events
           WHERE name LIKE ? AND season=2026 AND COALESCE(division,'club')='club'""",
        (USOPEN_EVENT,)).fetchone()
    if not ev:
        return None, [], []
    eid = ev[0]
    games = con.execute(
        """SELECT g.date, g.time, h.display_name, a.display_name
           FROM games g
           JOIN event_teams h ON h.event_team_id=g.home_id
           JOIN event_teams a ON a.event_team_id=g.away_id
           WHERE g.event_id=? ORDER BY g.date, g.time""", (eid,)).fetchall()
    teams = [r[0] for r in con.execute(
        """SELECT COALESCE(full_name, display_name) FROM event_teams
           WHERE event_id=? ORDER BY 1""", (eid,))]
    return ev, teams, [tuple(g) for g in games]


def derive_pools(games):
    """Pools from the co-play graph: pool play is a round robin, so the
    connected components of 'played each other' ARE the pools."""
    adj = {}
    for _d, _t, h, a in games:
        adj.setdefault(h, set()).add(a)
        adj.setdefault(a, set()).add(h)
    pools, seen = [], set()
    for t in sorted(adj):
        if t in seen:
            continue
        comp = {t} | adj[t]
        for u in list(comp):
            comp |= adj.get(u, set())
        pools.append(sorted(comp))
        seen |= comp
    return {chr(65 + i): p for i, p in enumerate(pools)}


def build():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ev, field, sched = usopen(con)
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

    # Pool LETTERS are load-bearing: the bracket pairing rule below is written
    # in terms of them, so an arbitrary labelling silently picks an arbitrary
    # bracket. derive_pools returns components in alphabetical-team order,
    # which is meaningless. Relabel so A holds the strongest team, B the next,
    # and sort within each pool strongest first. Still an assumption about the
    # pairing, but a deterministic and seed-coherent one.
    raw_pools = derive_pools(sched) if sched else {}
    ordered = sorted(raw_pools.values(),
                     key=lambda ts: -max((ratings.get(t) or 0) for t in ts))
    pools = {}
    for i, ts in enumerate(ordered):
        pools[chr(65 + i)] = sorted(ts, key=lambda t: -(ratings.get(t) or 0))

    # Trajectories for the drill-down, written by analysis.rankings from the
    # same replay that produced the CSVs. Optional: if it is missing the page
    # still builds, it just has nothing to open when a name is clicked.
    hist_path = DB_PATH.parent / "history.json"
    history = (json.loads(hist_path.read_text()) if hist_path.exists()
               else {"events": [], "players": {}, "teams": {}})

    payload = {
        "generated": ev[2] if ev else None,
        "minGames": MIN_GAMES,
        "totalRated": total_rated,
        "scale": PUBLISHED["division_scale"]["club"],
        "players": [[r["player"], float(r["elo"]), float(r["lo90"]), float(r["hi90"]),
                     int(r["games"]), r["last_club"], int(r["last_season"]),
                     int(r["rank"]), r["player_id"]]
                    for r in players],
        "clubs": {k: [[int(r["rank"]), r["club"], float(r["elo"]),
                       int(r["roster_size"]), r["roster_event"]]
                      for r in v] for k, v in clubs.items()},
        "history": history,
        "usopen": {
            "name": ev[1] if ev else "",
            "start": ev[2] if ev else "",
            "end": ev[3] if ev else "",
            "pools": pools,
            "ratings": ratings,
            "schedule": [{"date": d, "time": t, "home": h, "away": a}
                         for d, t, h, a in sched],
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"))))
    (OUT.parent / ".nojekyll").write_text("")
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} ({kb:,.0f} KB) + .nojekyll")
    print(f"  {len(payload['players']):,} players (>={MIN_GAMES} games), "
          f"{len(clubs['completed'])} clubs, {len(field)} U.S. Open teams, "
          f"{len(sched)} scheduled games, {len(pools)} pools")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USAU Club Men's — Player-Elo Rankings</title>
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
.legend{display:flex;flex-direction:column;gap:1px}
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
</style>

<div id="scrim"></div>
<div id="detail"><button class="back" id="dback">&lsaquo; Back</button>
  <button class="close" id="dclose">&times;</button>
  <div id="dbody"></div></div>
<div id="tip"></div>
<header>
  <h1>USAU Club Men's — Player-Elo Rankings</h1>
  <div class="sub" id="sub"></div>
</header>
<nav>
  <button data-t="clubs" class="on">Clubs</button>
  <button data-t="players">Players</button>
  <button data-t="trends">Trends</button>
  <button data-t="usopen">U.S. Open 2026</button>
</nav>
<main>

<section id="clubs" class="on">
  <div class="bar">
    <select id="basis">
      <option value="completed">Most recent completed roster</option>
      <option value="best">Best full-strength roster of 2026</option>
      <option value="upcoming">Next event roster</option>
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
    <span class="count" id="pcount"></span>
  </div>
  <table><thead><tr>
    <th class="n">#</th><th>Player</th><th class="n">Elo</th><th>90% band</th>
    <th class="n">G</th><th>Last club</th><th class="n">Yr</th>
  </tr></thead><tbody id="ptb"></tbody></table>
  <p class="note" id="pnote"></p>
</section>

<section id="trends">
  <div class="bar">
    <select id="tsub">
      <option value="p">Players</option>
      <option value="c">Clubs</option>
    </select>
    <select id="tdiv">
      <option value="all">All divisions</option>
      <option value="0">Club</option>
      <option value="1">College</option>
      <option value="2">College D-III</option>
    </select>
    <select id="tmode">
      <option value="elo">Elo</option>
      <option value="med">Above season median</option>
    </select>
    <span class="count" id="tcount"></span>
  </div>
  <div class="tgrid">
    <div id="tchart"></div>
    <div class="legend" id="tlegend"></div>
  </div>
  <p class="note" id="tnote"></p>
</section>

<section id="usopen">
  <div class="bar">
    <button class="act prim" id="simGame">Simulate next game</button>
    <button class="act prim" id="simRound">Simulate next round</button>
    <button class="act" id="reset">Clear all results</button>
    <span class="count" id="ucount"></span>
  </div>
  <h3 style="font-size:13px;margin:14px 0 9px;color:var(--ink-2)">
    Pool play — click a team to record the winner</h3>
  <div class="grid" id="pools"></div>
  <h3 style="font-size:13px;margin:22px 0 9px;color:var(--ink-2)">Bracket</h3>
  <div class="bracket" id="bracket"></div>
  <div class="champline" id="champline"></div>
  <h3 style="font-size:13px;margin:22px 0 9px;color:var(--ink-2)">
    Title odds — re-simulated from whatever you have entered</h3>
  <table class="odds"><thead><tr>
    <th class="n">#</th><th>Team</th><th class="n">Elo</th>
    <th class="n">Win pool</th><th class="n">Reach SF</th><th class="n">Title</th>
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

/* ---------- tabs ---------- */
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('section').forEach(s => s.classList.toggle('on', s.id === b.dataset.t));
  // Reducing 26k trajectories to season maps is ~370k operations, so it runs on
  // first activation of the tab rather than at load. seasonData memoises.
  if (b.dataset.t === 'trends' && !$('#tsvg')) drawTrends();
});

$('#sub').textContent =
  `Every player carries a personal Elo across seasons; a club's rating is the ` +
  `softmax-weighted mean of its event roster. Generated ${D.generated || ''}.`;

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
  const basis = $('#basis').value, rows = D.clubs[basis] || [];
  $('#ctb').innerHTML = rows.map(r =>
    `<tr><td class="rk">${r[0]}</td>` +
    `<td><span class="nmlink" data-club="${esc(r[1])}">${esc(r[1])}</span></td>` +
    `<td class="n">${r[2].toFixed(0)}</td><td class="n">${r[3]}</td>` +
    `<td class="muted" style="font-size:13px">${esc(r[4])}</td></tr>`).join('');
  $('#ccount').textContent = `${rows.length} clubs`;
  $('#cnote').textContent = CNOTE[basis];
}
$('#basis').onchange = drawClubs;

/* ---------- players ---------- */
function drawPlayers() {
  const q = $('#q').value.trim().toLowerCase();
  const only26 = $('#only26').checked, ming = +$('#ming').value;
  // Rank is a property of the player within the POPULATION the toggles define,
  // so it is assigned before the search runs. Searching is a lookup, not a
  // re-ranking: find a player and his number is the one he actually holds,
  // and results come back sparse (#12, #47, #103) rather than renumbered 1..n.
  let pop = D.players.filter(p => p[4] >= ming);
  if (only26) pop = pop.filter(p => p[6] === 2026);
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
    `<td class="band">[${p[2].toFixed(0)}, ${p[3].toFixed(0)}]</td>` +
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
});
$('#pnote').textContent =
  `Searching does not renumber anything — a player keeps the rank he holds in the ` +
  `current list, so results come back sparse. The two toggles do change the rank, ` +
  `because they change who is being ranked; hover a rank to see the player's ` +
  `position across all ${D.totalRated.toLocaleString()} rated players. ` +
  `Bands are 90% intervals on the rating as an estimate of current skill. Players ` +
  `below ${D.minGames} games are omitted: under that the engine's provisional ` +
  `multiplier is still moving a rating faster than results justify. Ratings never ` +
  `decay, so an unfiltered list mixes eras — "2026 rosters only" is on by default.`;

/* ---------- U.S. Open ---------- */
const U = D.usopen, R = U.ratings, SCALE = D.scale;
const POOLS = U.pools, PK = Object.keys(POOLS).sort();
const SCHED = U.schedule;
// v2: prequarter/quarter slot ids were renumbered so column i feeds column
// i+1, which changes what 'pq0' refers to. Bumping the key discards saved
// state rather than silently reinterpreting it against the new bracket.
const KEY = 'usopen2026.v2';
let S = load();
function load() {
  let v;
  try { v = JSON.parse(localStorage.getItem(KEY)); } catch (e) { v = null; }
  v = v || {};
  // `sim` marks which results came from the simulate buttons rather than from
  // you. Defaulted here so state saved before it existed still loads.
  return {pool: v.pool || {}, br: v.br || {}, sim: v.sim || {}};
}
function save() { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} }

const P = (a,b) => 1 / (1 + Math.pow(10, ((R[b]||1500) - (R[a]||1500)) / SCALE));
const poolOf = t => PK.find(k => POOLS[k].includes(t));

/* Pool standings from entered results. Returns {wins, order} where order is
   the pool sorted by wins then rating - rating breaks ties because we do not
   model point differential. */
function standings(k, res) {
  const ts = POOLS[k], w = {};
  ts.forEach(t => w[t] = 0);
  SCHED.forEach((g, i) => {
    if (poolOf(g.home) !== k) return;
    const r = res[i];
    if (r === 'h') w[g.home]++; else if (r === 'a') w[g.away]++;
  });
  const order = ts.slice().sort((x,y) => (w[y]-w[x]) || (R[y]-R[x]));
  return {w, order};
}
function poolComplete(k) {
  return SCHED.every((g,i) => poolOf(g.home) !== k || S.pool[i]);
}

/* Prequarter pairing: 2nd of one pool vs 3rd of another, cross-pool; quarters
   are pool winners against prequarter winners. This is an ASSUMPTION - USAU
   has not published the pairing - and it is flagged in the note below.

   The arrays are ordered so prequarter i feeds quarter i. That is purely a
   presentation concern and it is the whole reason the columns line up: the
   bracket is identical to the previous indexing, just renumbered so row i of
   one column is the input to row i of the next. */
const PQ = [['B',1,'C',2], ['C',1,'B',2], ['A',1,'D',2], ['D',1,'A',2]];
const QF = [['A',0], ['D',0], ['B',0], ['C',0]];
const PQL = PQ.map(([p1,i1,p2,i2]) => [p1+(i1+1), p2+(i2+1)]);
const QFL = QF.map(([p,si],i) => [p+(si+1), 'W PQ'+(i+1)]);
const SFL = [['W QF1','W QF2'], ['W QF3','W QF4']];
const FL  = ['W SF1','W SF2'];

function bracketFrom(res, br) {
  // A pool's finishing order is a FACT only once its games are all played.
  // Before that, standings() is just a projection ordered by rating, so the
  // displayed bracket must say TBD rather than invent a matchup.
  const ord = {};
  PK.forEach(k => {
    const o = standings(k, res).order;
    ord[k] = poolComplete(k) ? o : [null, null, null];
  });
  const pq = PQ.map(([p1,i1,p2,i2]) => [ord[p1][i1], ord[p2][i2]]);
  const pick = (id, a, b, rnd) => {
    if (!a || !b) return null;
    const locked = br[id];
    if (locked === a || locked === b) return locked;
    return rnd ? (Math.random() < P(a,b) ? a : b) : null;
  };
  const pqw = pq.map((m,i) => pick('pq'+i, m[0], m[1], false));
  const qf = QF.map(([p,si],i) => [ord[p][si], pqw[i]]);
  const qfw = qf.map((m,i) => pick('qf'+i, m[0], m[1], false));
  const sf = [[qfw[0], qfw[1]], [qfw[2], qfw[3]]];
  const sfw = sf.map((m,i) => pick('sf'+i, m[0], m[1], false));
  const fin = [sfw[0], sfw[1]];
  const champ = pick('f0', fin[0], fin[1], false);
  return {ord, pq, pqw, qf, qfw, sf, sfw, fin, champ};
}

/* Monte Carlo conditioned on every result entered so far. */
function simulate(n) {
  const teams = Object.keys(R);
  const pool = {}, semi = {}, title = {};
  teams.forEach(t => { pool[t]=0; semi[t]=0; title[t]=0; });
  const poolIdx = SCHED.map((g,i) => i).filter(i => poolOf(SCHED[i].home));
  for (let s = 0; s < n; s++) {
    const res = {};
    poolIdx.forEach(i => {
      const g = SCHED[i];
      res[i] = S.pool[i] || (Math.random() < P(g.home, g.away) ? 'h' : 'a');
    });
    const ord = {}; PK.forEach(k => ord[k] = standings(k, res).order);
    PK.forEach(k => pool[ord[k][0]]++);
    const rp = (id,a,b) => {
      const lk = S.br[id];
      if (lk === a || lk === b) return lk;
      return Math.random() < P(a,b) ? a : b;
    };
    const pqw = PQ.map(([p1,i1,p2,i2],i) => rp('pq'+i, ord[p1][i1], ord[p2][i2]));
    const qfw = QF.map(([p,si],i) => rp('qf'+i, ord[p][si], pqw[i]));
    const s1 = rp('sf0', qfw[0], qfw[1]), s2 = rp('sf1', qfw[2], qfw[3]);
    semi[qfw[0]]++; semi[qfw[1]]++; semi[qfw[2]]++; semi[qfw[3]]++;
    title[rp('f0', s1, s2)]++;
  }
  return {pool, semi, title, n};
}

function gameRow(i) {
  const g = SCHED[i], r = S.pool[i];
  const ph = P(g.home, g.away);
  const sd = S.sim['p'+i] ? ' simd' : '';
  const cls = s => r ? (r === s ? 't w' + sd : 't l') : 't';
  return `<div class="g">` +
    `<div class="${cls('h')}" data-g="${i}" data-s="h">` +
      `<span>${esc(g.home)}</span><span class="p">${pct(ph)}</span></div>` +
    `<span class="vs">v</span>` +
    `<div class="${cls('a')}" data-g="${i}" data-s="a">` +
      `<span>${esc(g.away)}</span><span class="p">${pct(1-ph)}</span></div></div>`;
}

function drawPools() {
  $('#pools').innerHTML = PK.map(k => {
    const st = standings(k, S.pool);
    const idx = SCHED.map((g,i)=>i).filter(i => poolOf(SCHED[i].home) === k);
    const played = idx.filter(i => S.pool[i]).length;
    return `<div class="card"><h3>Pool ${k} — ${played}/${idx.length} played</h3>` +
      idx.map(gameRow).join('') +
      `<table class="stand">` + st.order.map((t,j) =>
        `<tr><td><span class="seed">${j+1}</span>${esc(t)}` +
        `<span class="muted" style="font-size:11.5px"> ${R[t] ? R[t].toFixed(0) : '—'}</span></td>` +
        `<td class="w">${st.w[t]}W</td></tr>`).join('') + `</table></div>`;
  }).join('');
}

/* One match box. `lbl` is the pair of seed descriptors ("A1" / "W PQ2") so a
   slot still says what feeds it while the teams are unknown. `place` is the
   grid position, `span` the height of the incoming vertical connector in px -
   0 for a one-to-one feed, one row pitch for a semi, two for the final. */
function slot(id, a, b, lbl, place, span, opts) {
  opts = opts || {};
  const line = (x, other, seed) => {
    const sd = `<span class="sd">${seed}</span>`;
    if (!x) return `<div class="t tbd">${sd}<span class="nm">TBD</span></div>`;
    const w = S.br[id], sm = S.sim['b'+id] ? ' simd' : '';
    const cls = w ? (w === x ? 't w' + sm : 't l') : 't';
    const p = other ? `<span class="p">${pct(P(x, other))}</span>` : '';
    return `<div class="${cls}" data-b="${id}" data-w="${esc(x)}">` +
           `${sd}<span class="nm">${esc(x)}</span>${p}</div>`;
  };
  const cin = span === null ? '' :
    `<i class="cin" style="--span:${span}px"></i>`;
  return `<div class="m${opts.champ ? ' champ' : ''}${opts.out ? ' out' : ''}" ` +
         `style="${place}">${cin}${line(a,b,lbl[0])}${line(b,a,lbl[1])}</div>`;
}

function drawBracket() {
  const B = bracketFrom(S.pool, S.br);
  // Row pitch must match the CSS custom properties --mh and --rg.
  const PITCH = 66 + 10;
  const at = (col, row, span) =>
    `grid-column:${col};grid-row:${row}${span ? ' / span ' + span : ''}`;
  const head = ['Prequarters','Quarters','Semis','Final'].map((h,i) =>
    `<div class="round" style="${at(i+1,1)}"><h4>${h}</h4></div>`).join('');
  const pq = B.pq.map((m,i) =>
    slot('pq'+i, m[0], m[1], PQL[i], at(1, i+2), null, {out:true})).join('');
  const qf = B.qf.map((m,i) =>
    slot('qf'+i, m[0], m[1], QFL[i], at(2, i+2), 0, {out:true})).join('');
  const sf = B.sf.map((m,i) =>
    slot('sf'+i, m[0], m[1], SFL[i], at(3, 2*i+2, 2), PITCH, {out:true})).join('');
  const fin = slot('f0', B.fin[0], B.fin[1], FL, at(4, 2, 4), 2*PITCH,
                   {champ: !!B.champ});
  $('#bracket').innerHTML = head + pq + qf + sf + fin;
  $('#champline').innerHTML = B.champ
    ? `Champion: <b>${esc(B.champ)}</b>`
    : `<span class="muted">Record pool results above, then click through the ` +
      `bracket. Each slot shows the seed that feeds it until the teams are known.</span>`;
}

function drawOdds() {
  const N = 40000, r = simulate(N);
  const rows = Object.keys(R).sort((a,b) => r.title[b]-r.title[a] || R[b]-R[a]);
  const top = r.title[rows[0]] / N || 1;
  $('#otb').innerHTML = rows.map((t,i) =>
    `<tr><td class="rk">${i+1}</td><td>${esc(t)}</td>` +
    `<td class="n">${R[t] ? R[t].toFixed(0) : '—'}</td>` +
    `<td class="n">${pct(r.pool[t]/N)}</td><td class="n">${pct(r.semi[t]/N)}</td>` +
    `<td class="n"><b>${pct(r.title[t]/N)}</b></td>` +
    `<td class="bar-c"><div class="oddsbar" style="width:${100*(r.title[t]/N)/top}%"></div></td>` +
    `</tr>`).join('');
  const done = Object.keys(S.pool).length + Object.keys(S.br).length;
  $('#ucount').textContent =
    `${N.toLocaleString()} simulations · ${done} result${done===1?'':'s'} entered`;
}

function drawUS() { drawPools(); drawBracket(); drawOdds(); updateSimButtons(); }

/* ---- simulation controls ----
   "Next" means the earliest undecided game in playing order: pool play first
   in schedule order, then prequarters, quarters, semis, final. A bracket slot
   only counts as playable once both its participants are known, which is why
   this walks the rounds in order rather than scanning a flat list. */
const ROUNDS = ['Pool play', 'Prequarters', 'Quarters', 'Semis', 'Final'];

function pending() {
  const poolIdx = SCHED.map((g,i) => i)
                       .filter(i => poolOf(SCHED[i].home) && !S.pool[i]);
  if (poolIdx.length) return {round:0, items: poolIdx.map(i => ({kind:'pool', i}))};
  const B = bracketFrom(S.pool, S.br);
  const stages = [['pq', B.pq], ['qf', B.qf], ['sf', B.sf], ['f', [B.fin]]];
  for (let r = 0; r < stages.length; r++) {
    const [pfx, ms] = stages[r], items = [];
    ms.forEach((m, i) => {
      const id = pfx === 'f' ? 'f0' : pfx + i;
      if (m[0] && m[1] && !S.br[id]) items.push({kind:'br', id, a:m[0], b:m[1]});
    });
    if (items.length) return {round: r+1, items};
  }
  return null;
}

/* Draw one result from the model's own probability, not the favourite. */
function playOne(it) {
  if (it.kind === 'pool') {
    const g = SCHED[it.i];
    S.pool[it.i] = Math.random() < P(g.home, g.away) ? 'h' : 'a';
    S.sim['p'+it.i] = 1;
    S.br = {};            // a pool result can change who is in the bracket
    Object.keys(S.sim).forEach(k => { if (k[0] === 'b') delete S.sim[k]; });
  } else {
    S.br[it.id] = Math.random() < P(it.a, it.b) ? it.a : it.b;
    S.sim['b'+it.id] = 1;
  }
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
  const nm = it.kind === 'pool' ? `${SCHED[it.i].home} v ${SCHED[it.i].away}`
                                : `${it.a} v ${it.b}`;
  g.textContent = `Simulate next game — ${nm}`;
  r.textContent = `Simulate ${ROUNDS[p.round]} — ${p.items.length} game` +
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

$('#pools').addEventListener('click', e => {
  const el = e.target.closest('[data-g]'); if (!el) return;
  const i = el.dataset.g;
  S.pool[i] = (S.pool[i] === el.dataset.s) ? undefined : el.dataset.s;
  if (!S.pool[i]) delete S.pool[i];
  delete S.sim['p'+i];             // typed by hand, so no longer simulated
  S.br = {};                       // pool change can invalidate every bracket slot
  Object.keys(S.sim).forEach(k => { if (k[0] === 'b') delete S.sim[k]; });
  save(); drawUS();
});
$('#bracket').addEventListener('click', e => {
  const el = e.target.closest('[data-b]'); if (!el) return;
  const id = el.dataset.b, w = el.dataset.w;
  S.br[id] = (S.br[id] === w) ? undefined : w;
  if (!S.br[id]) delete S.br[id];
  delete S.sim['b'+id];
  save(); drawUS();
});
$('#reset').onclick = () => { S = {pool:{}, br:{}, sim:{}}; save(); drawUS(); };

$('#unote').innerHTML =
  `Probabilities come from the published player-Elo model at club scale ${SCALE}, with ` +
  `each club rated off <b>the roster it registered for this event</b> — not off its last ` +
  `completed tournament. Neutral throughout: <code>home_advantage</code> is 0, so no ` +
  `seeding information enters.<br><br>` +
  `<b>The bracket pairing is an assumption.</b> Pool winners bye to quarters and 2nd plays ` +
  `3rd cross-pool in prequarters; USAU has not published the pairing. Pool-win probabilities ` +
  `do not depend on it, title odds do.<br><br>` +
  `<b>Simulated results are dashed and marked ~.</b> The two simulate buttons draw ` +
  `each result from the model's own probability, not from the favourite — so a 55% ` +
  `game goes the other way about 45% of the time, and running the same round twice ` +
  `will not always agree. Clicking a team yourself overrides the simulated pick and ` +
  `clears the mark, so a real scoreline always beats a coin flip.<br><br>` +
  `Three-way pool ties break on rating, because point differential is not modelled. ` +
  `Warao and EVOLUTION are international entrants with no USAU history, so their ratings ` +
  `encode absence of evidence rather than measured weakness. Results you enter are kept in ` +
  `this browser only.`;

/* ---------- drill-down: play history + rating curve ---------- */
const H = D.history || {events:[], players:{}, teams:{}, teamKey:{}};
const TK = H.teamKey || {};    // display name -> normalized club key
const TN = H.teamNames || {};  // normalized club key -> current display spelling
const CN = H.clubNames || [];  // affiliation index -> normalized club key
const ROST = H.rosters || {};  // "<clubKey>|<eventIdx>" -> delta-encoded people
const PEOPLE = H.people || [], PPID = H.peoplePid || [];
const HEV = H.events;   // [date, name, season, divisionCode]
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
    return `<tr><td class="d">${p.date}</td>` +
           `<td>${esc(p.event)}<span class="muted" style="font-size:11.5px">` +
           ` ${['club','college','D-III'][p.div] || 'club'}</span></td>` + mid +
           `<td class="r">${p.elo}</td><td class="dl">${dl}</td></tr>`;
  }).join('');
  return `<table class="hist"><thead><tr><th>Date</th><th>Event</th>` +
         (isTeam ? '<th class="n">Roster</th>' : '<th>Team</th>') +
         `<th class="n">Elo after</th><th class="n">Δ</th></tr></thead>` +
         `<tbody>${rows}</tbody></table>`;
}

/* Roster indices are delta-encoded and ascending; `people` is name-sorted, so
   ascending index is already ascending name. */
function rosterOf(rk) {
  const enc = ROST[rk];
  if (!enc) return [];
  const out = []; let i = 0;
  for (let k = 0; k < enc.length; k++) { i += enc[k]; out.push(i); }
  return out;
}
function toggleRoster(el) {
  const tr = el.closest('tr'), nx = tr.nextElementSibling;
  if (nx && nx.classList.contains('rost')) { nx.remove(); return; }
  const ids = rosterOf(el.dataset.roster);
  // A member below the trajectory floor has nothing to open, so he renders as
  // plain text — but he was on the roster and is not dropped from the list.
  const html = ids.map(i => H.players[PPID[i]]
    ? `<span class="nmlink" data-pid="${esc(PPID[i])}">${esc(PEOPLE[i])}</span>`
    : `<span class="muted">${esc(PEOPLE[i])}</span>`).join(', ');
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
    // it still has a trajectory, so show that and omit the rank.
    const tbl = D.clubs[$('#basis').value] || [];
    const row = tbl.find(r => r[1] === title);
    if (row) parts.push(`Elo <b>${row[2].toFixed(0)}</b>`, `roster ${row[3]}`,
                        `#${row[0]} of ${tbl.length} clubs`);
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
    `${pts.length} event${pts.length === 1 ? '' : 's'} on record.</p>` +
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
function closeDetail() {
  const was = cur;
  $('#detail').classList.remove('on'); $('#scrim').classList.remove('on');
  $('#tip').classList.remove('on'); $('#dback').classList.remove('on');
  navStack = []; cur = null;
  if (was && location.hash && location.hash !== '#') location.hash = '#';
}
$('#dclose').onclick = closeDetail;
$('#scrim').onclick = closeDetail;
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
/* Every link inside the panel: roster-size cells, affiliation cells, and the
   people inside an expanded roster. */
$('#dbody').addEventListener('click', e => {
  const r = e.target.closest('[data-roster]');
  if (r) { toggleRoster(r); return; }
  const p = e.target.closest('[data-pid]');
  if (p) { openDetail('p', p.dataset.pid); return; }
  const c = e.target.closest('[data-club]');
  if (c) openDetail('c', c.dataset.club);
});

/* ---------- deep links ---------- */
// Club keys carry spaces and punctuation ('rhino slam!', 'cookie mon$terz'),
// so the key is percent-encoded rather than concatenated raw.
const known = (kind, key) => kind === 'p'
  ? (!!H.players[key] || PBY.has(String(key)))
  : !!H.teams[TK[key] || key];
function routeHash() {
  const m = /^#([pc])\/(.+)$/.exec(location.hash || '');
  if (!m) { if (cur) closeDetail(); return; }
  let key;
  try { key = decodeURIComponent(m[2]); } catch (err) { key = m[2]; }
  if (cur && cur.kind === m[1] && cur.key === key) return;
  if (!known(m[1], key)) { closeDetail(); return; }
  openDetail(m[1], key);
}
window.addEventListener('hashchange', routeHash);

/* ---------- trends: 25-line season charts ---------- */
// Eight hues x four dash patterns = 32 distinguishable strokes for 25 lines.
const DASH = ['none', '5 3', '2 3', '8 3 2 3'];
const TOPN = 25;
// Codes match the DIVCODE written into each event by analysis.rankings.
const DIVLABEL = {all: 'all divisions', 0: 'club', 1: 'college', 2: 'college D-III'};
const trendCache = {};

function playerLabel(pid) {
  const row = PBY.get(String(pid));
  if (row) return row[0];
  const i = PPID.indexOf(String(pid));
  return i >= 0 ? PEOPLE[i] : String(pid);
}

/* One value per season: the rating after that season's LAST event. Points are
   already chronological, so a plain overwrite lands on the last one. */
function seasonData(kind, div) {
  const ck = kind + '|' + div;
  if (trendCache[ck]) return trendCache[ck];
  const src = kind === 'p' ? H.players : H.teams;
  const dv = div === 'all' ? null : +div;
  const all = [];
  for (const key in src) {
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
  // Highest SINGLE-SEASON rating, which is exactly 25 lines and keeps a
  // subject who was great in 2019 and has since folded. The union of each
  // season's top 25 would be an unbounded, unreadable number of lines.
  const top = all.sort((a, b) => b.peak - a.peak).slice(0, TOPN);
  top.forEach(s => s.label = kind === 'p' ? playerLabel(s.key) : clubLabel(s.key));
  trendCache[ck] = {top, med, n: all.length};
  return trendCache[ck];
}

function trendChart(series, mode, med) {
  const W = 900, Hh = 460, T = 18, Rm = 12, B = 34, L = 52;
  const n = SEASONS.length;
  const vof = (s, i) => s.vals[i] === null ? null
    : (mode === 'med' ? s.vals[i] - med[i] : s.vals[i]);
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
          `preserveAspectRatio="none">`;
  const step = mode === 'med' ? 100 : 200;
  for (let g = Math.ceil(y0 / step) * step; g < y1; g += step) {
    const y = py(g).toFixed(1);
    s += `<line class="gl" x1="${L}" x2="${W - Rm}" y1="${y}" y2="${y}"/>` +
         `<text x="4" y="${(py(g) + 3).toFixed(1)}">${g}</text>`;
  }
  s += `<line class="ax" x1="${L}" x2="${W - Rm}" y1="${Hh - B}" y2="${Hh - B}"/>`;
  SEASONS.forEach((yr, i) => {
    s += `<text class="sx" x="${px(i).toFixed(1)}" y="${Hh - B + 16}">${yr}</text>`;
  });
  series.forEach((sr, si) => {
    const dash = DASH[Math.floor(si / 8)];
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

let hotIdx = null, pinIdx = null;
function drawTrends() {
  const kind = $('#tsub').value, mode = $('#tmode').value, div = $('#tdiv').value;
  const dat = seasonData(kind, div);
  hotIdx = null; pinIdx = null;
  $('#tchart').innerHTML = trendChart(dat.top, mode, dat.med);
  $('#tlegend').innerHTML = dat.top.map((sr, i) => {
    const dash = DASH[Math.floor(i / 8)];
    const link = kind === 'p' ? `data-pid="${esc(sr.key)}"`
                              : `data-club="${esc(sr.key)}"`;
    const shown = mode === 'med'
      ? Math.max(...SEASONS.map((_, j) => sr.vals[j] === null ? -Infinity
                                          : sr.vals[j] - dat.med[j]))
      : sr.peak;
    return `<div class="lrow" data-series="${i}" style="color:var(--s${i % 8 + 1})">` +
      `<svg class="sw" viewBox="0 0 14 3"><line x1="0" y1="1.5" x2="14" y2="1.5" ` +
      `stroke="currentColor" stroke-width="3"` +
      (dash === 'none' ? '' : ` stroke-dasharray="${dash}"`) + `/></svg>` +
      `<span class="lbl nmlink" ${link} title="${esc(sr.label)}">` +
      `${esc(sr.label)}</span>` +
      `<span class="pk">${mode === 'med' && shown > 0 ? '+' : ''}` +
      `${Math.round(shown)}</span></div>`;
  }).join('');
  $('#tlegend').classList.remove('dim');
  $('#tcount').textContent = `top ${dat.top.length} of ` +
    `${dat.n.toLocaleString()} ${kind === 'p' ? 'players' : 'clubs'} · ` +
    `${DIVLABEL[div]} · ${SEASONS[0]}–${SEASONS[SEASONS.length - 1]}`;
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
  const g = e.target.closest('.sg'); if (g) setHot(+g.dataset.series);
});
$('#tchart').addEventListener('mouseleave', () => setHot(pinIdx));
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
$('#tmode').onchange = drawTrends;
$('#tnote').textContent =
  `One point per season: the rating after that season's last event. The lines are ` +
  `the 25 subjects with the highest single-season rating — not the union of each ` +
  `season's top 25, which would be an unbounded number of lines. A season a subject ` +
  `did not play breaks the line rather than being interpolated across. Hover a ` +
  `legend row to isolate it, click the row to pin, click the name to open its ` +
  `full history. "Above season median" subtracts the median of every rated ` +
  `subject active that season. Narrowing the division keeps only events in it, ` +
  `so a season reads as the rating after that season's last event in that ` +
  `division — 301 club identities play in more than one, and each is ranked on ` +
  `its own record in whichever division you are looking at.`;

drawClubs(); drawPlayers(); drawUS(); routeHash();
</script>
</html>
"""


if __name__ == "__main__":
    build()
