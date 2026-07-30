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

    players = [r for r in load_csv("player_elo.csv") if int(r["games"]) >= MIN_GAMES]
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

    payload = {
        "generated": ev[2] if ev else None,
        "minGames": MIN_GAMES,
        "scale": PUBLISHED["division_scale"]["club"],
        "players": [[r["player"], float(r["elo"]), float(r["lo90"]), float(r["hi90"]),
                     int(r["games"]), r["last_club"], int(r["last_season"])]
                    for r in players],
        "clubs": {k: [[int(r["rank"]), r["club"], float(r["elo"]),
                       int(r["roster_size"]), r["roster_event"]]
                      for r in v] for k, v in clubs.items()},
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
}
@media (prefers-color-scheme:dark){:root{
  --bg:#14150f; --surface:#1c1d16; --ink:#f4f4ee; --ink-2:#c3c3b8; --ink-3:#8b8c80;
  --line:#34352c; --line-strong:#45463b; --accent:#199e70; --warn:#d95926;
  --win:#199e70; --lose:#e66767; --chip:#2a2b22;
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
td.band{color:var(--ink-3);font-family:var(--mono);font-size:12px;white-space:nowrap}
.muted{color:var(--ink-3)} .note{font-size:12.5px;color:var(--ink-3);margin-top:10px;
  line-height:1.6;max-width:820px}
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
.bracket{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin-top:4px}
.round h4{margin:0 0 7px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--ink-3);font-weight:600}
.m{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:5px;
   margin-bottom:8px}
.m .t{display:flex;justify-content:space-between;gap:6px;padding:4px 6px;border-radius:5px;
  cursor:pointer;font-size:13px;align-items:baseline}
.m .t:hover{background:var(--chip)}
.m .t.w{background:color-mix(in srgb,var(--win) 15%,transparent);font-weight:600}
.m .t.l{opacity:.42}
.m .t.tbd{color:var(--ink-3);cursor:default;font-style:italic}
.m .t.tbd:hover{background:none}
.m .p{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.odds td.bar-c{width:38%;padding-right:12px}
.oddsbar{height:7px;border-radius:4px;background:var(--accent);min-width:1px}
.champ{background:color-mix(in srgb,var(--accent) 13%,transparent);border-color:var(--accent)}
@media (max-width:860px){.bracket{grid-template-columns:1fr}}
</style>

<header>
  <h1>USAU Club Men's — Player-Elo Rankings</h1>
  <div class="sub" id="sub"></div>
</header>
<nav>
  <button data-t="clubs" class="on">Clubs</button>
  <button data-t="players">Players</button>
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

<section id="usopen">
  <div class="bar">
    <button class="act" id="reset">Clear all results</button>
    <span class="count" id="ucount"></span>
  </div>
  <h3 style="font-size:13px;margin:14px 0 9px;color:var(--ink-2)">
    Pool play — click a team to record the winner</h3>
  <div class="grid" id="pools"></div>
  <h3 style="font-size:13px;margin:22px 0 9px;color:var(--ink-2)">Bracket</h3>
  <div class="bracket" id="bracket"></div>
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
    `<tr><td class="rk">${r[0]}</td><td>${esc(r[1])}</td>` +
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
  let rows = D.players.filter(p => p[4] >= ming);
  if (only26) rows = rows.filter(p => p[6] === 2026);
  if (q) rows = rows.filter(p => p[0].toLowerCase().includes(q) ||
                                 String(p[5]).toLowerCase().includes(q));
  const shown = rows.slice(0, 300);
  $('#ptb').innerHTML = shown.map((p, i) =>
    `<tr><td class="rk">${i+1}</td><td>${esc(p[0])}</td>` +
    `<td class="n">${p[1].toFixed(0)}</td>` +
    `<td class="band">[${p[2].toFixed(0)}, ${p[3].toFixed(0)}]</td>` +
    `<td class="n">${p[4]}</td><td class="muted" style="font-size:13px">${esc(p[5])}</td>` +
    `<td class="n">${p[6]}</td></tr>`).join('');
  $('#pcount').textContent =
    `${rows.length.toLocaleString()} match` + (rows.length > 300 ? ' — showing first 300' : '');
}
['input','change'].forEach(e => {
  $('#q').addEventListener(e, drawPlayers);
  $('#only26').addEventListener(e, drawPlayers);
  $('#ming').addEventListener(e, drawPlayers);
});
$('#pnote').textContent =
  `Ranks renumber with the filter. Bands are 90% intervals on the rating as an estimate ` +
  `of current skill. Players below ${D.minGames} games are omitted: under that the engine's ` +
  `provisional multiplier is still moving a rating faster than results justify. ` +
  `Ratings never decay, so an unfiltered list mixes eras — "2026 rosters only" is on by default.`;

/* ---------- U.S. Open ---------- */
const U = D.usopen, R = U.ratings, SCALE = D.scale;
const POOLS = U.pools, PK = Object.keys(POOLS).sort();
const SCHED = U.schedule;
const KEY = 'usopen2026.v1';
let S = load();
function load() {
  try { return JSON.parse(localStorage.getItem(KEY)) || {pool:{}, br:{}}; }
  catch (e) { return {pool:{}, br:{}}; }
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

/* Prequarter pairing: 2nd of one pool vs 3rd of the next, cross-pool.
   Quarters: pool winners against prequarter winners. This is an ASSUMPTION -
   USAU has not published the bracket - and it is flagged in the note below. */
const PQ = [['A',1,'D',2], ['B',1,'C',2], ['C',1,'B',2], ['D',1,'A',2]];
const QF = [['A',0,1], ['D',0,2], ['B',0,0], ['C',0,3]];

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
  const qf = QF.map(([p,si,pqi]) => [ord[p][si], pqw[pqi]]);
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
    const qfw = QF.map(([p,si,pqi],i) => rp('qf'+i, ord[p][si], pqw[pqi]));
    const s1 = rp('sf0', qfw[0], qfw[1]), s2 = rp('sf1', qfw[2], qfw[3]);
    semi[qfw[0]]++; semi[qfw[1]]++; semi[qfw[2]]++; semi[qfw[3]]++;
    title[rp('f0', s1, s2)]++;
  }
  return {pool, semi, title, n};
}

function gameRow(i) {
  const g = SCHED[i], r = S.pool[i];
  const ph = P(g.home, g.away);
  const cls = s => r ? (r === s ? 't w' : 't l') : 't';
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

function slot(id, a, b, champ) {
  const t = (x, other) => {
    if (!x) return `<div class="t tbd">TBD</div>`;
    const w = S.br[id], cls = w ? (w === x ? 't w' : 't l') : 't';
    const p = other ? `<span class="p">${pct(P(x, other))}</span>` : '';
    return `<div class="${cls}" data-b="${id}" data-w="${esc(x)}">` +
           `<span>${esc(x)}</span>${p}</div>`;
  };
  return `<div class="m${champ ? ' champ' : ''}">${t(a,b)}${t(b,a)}</div>`;
}

function drawBracket() {
  const B = bracketFrom(S.pool, S.br);
  const cols = [
    ['Prequarters', B.pq.map((m,i) => slot('pq'+i, m[0], m[1]))],
    ['Quarters',    B.qf.map((m,i) => slot('qf'+i, m[0], m[1]))],
    ['Semis',       B.sf.map((m,i) => slot('sf'+i, m[0], m[1]))],
    ['Final',       [slot('f0', B.fin[0], B.fin[1], !!B.champ)]],
  ];
  $('#bracket').innerHTML = cols.map(([h, ms]) =>
    `<div class="round"><h4>${h}</h4>${ms.join('')}</div>`).join('');
  if (B.champ) {
    $('#bracket').innerHTML += `<div class="round"><h4>Champion</h4>` +
      `<div class="m champ"><div class="t w"><span>${esc(B.champ)}</span></div></div></div>`;
  }
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
  $('#ucount').textContent = `${N.toLocaleString()} simulations · ${done} result${done===1?'':'s'} entered`;
}

function drawUS() { drawPools(); drawBracket(); drawOdds(); }

$('#pools').addEventListener('click', e => {
  const el = e.target.closest('[data-g]'); if (!el) return;
  const i = el.dataset.g;
  S.pool[i] = (S.pool[i] === el.dataset.s) ? undefined : el.dataset.s;
  if (!S.pool[i]) delete S.pool[i];
  S.br = {};                       // pool change can invalidate every bracket slot
  save(); drawUS();
});
$('#bracket').addEventListener('click', e => {
  const el = e.target.closest('[data-b]'); if (!el) return;
  const id = el.dataset.b, w = el.dataset.w;
  S.br[id] = (S.br[id] === w) ? undefined : w;
  if (!S.br[id]) delete S.br[id];
  save(); drawUS();
});
$('#reset').onclick = () => { S = {pool:{}, br:{}}; save(); drawUS(); };

$('#unote').innerHTML =
  `Probabilities come from the published player-Elo model at club scale ${SCALE}, with ` +
  `each club rated off <b>the roster it registered for this event</b> — not off its last ` +
  `completed tournament. Neutral throughout: <code>home_advantage</code> is 0, so no ` +
  `seeding information enters.<br><br>` +
  `<b>The bracket pairing is an assumption.</b> Pool winners bye to quarters and 2nd plays ` +
  `3rd cross-pool in prequarters; USAU has not published the pairing. Pool-win probabilities ` +
  `do not depend on it, title odds do.<br><br>` +
  `Three-way pool ties break on rating, because point differential is not modelled. ` +
  `Warao and EVOLUTION are international entrants with no USAU history, so their ratings ` +
  `encode absence of evidence rather than measured weakness. Results you enter are kept in ` +
  `this browser only.`;

drawClubs(); drawPlayers(); drawUS();
</script>
</html>
"""


if __name__ == "__main__":
    build()
