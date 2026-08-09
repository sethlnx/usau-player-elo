"""Build the published rankings page.

Three tabs: combined USAU/EUF club and player rankings, per-season Trends, and
a USAU Tournaments browser showing every event's recovered pools and bracket
plus the history of the series it belongs to.

The page itself is one file and opens over file:// with no server. Two things
ride beside it rather than inside it, both as classic <script> tags because a
file:// page cannot fetch(): the rating trajectories, pulled in the background
after first paint, and the tournament shapes, faulted in one season at a time
when an event is opened. Inputs are the published artifacts only - this script
never replays the model, so the page can never disagree with the CSVs:

    data/player_elo.csv          player table (>= MIN_GAMES shown)
    data/team_elo.csv            clubs, most recent COMPLETED event roster
    data/team_elo_best.csv       clubs, best full-strength roster of 2026
    data/usau.db                 USAU schedules for the Tournaments browser
    data/euf.db                  European games and captured season rosters

Usage: python -m analysis.site   ->   docs/index.html + history.js + t/<season>.js
"""

import collections
import hashlib
import csv
import json
import re
import sqlite3
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.backtest import DB_PATH
from analysis.field_strength import TIER_NAMES as FIELD_TIER_NAMES
from analysis.field_strength import classify as classify_strength
from analysis.history_split import BUCKETS as HIST_BUCKETS
from analysis.history_split import multi_division_clubs, trend_series_range
from analysis.history_split import split as split_history
from analysis.rankings import DIVCODE, PUBLISHED, TEAM_DIVISIONS
from analysis.tournaments import build as build_tournaments

# docs/ rather than site/: GitHub Pages can serve a branch's root or its
# /docs folder and nothing else, so putting the page here makes the project
# URL itself the app. Environment overrides build a sister site without
# mutating the Elo publication.
DATA_DIR = Path(os.environ.get("RANKINGS_DATA_DIR", DB_PATH.parent))
OUT = Path(os.environ.get(
    "RANKINGS_SITE_OUT", DB_PATH.parent.parent / "docs" / "index.html"
))
RATING_NAME = os.environ.get("RATING_NAME", "Elo")

# Everything that is not needed to draw the first screen is emitted BESIDE the
# page as a classic <script>, never fetched: both work over https, but fetch()
# on a file:// page is blocked by CORS while a script from the same directory
# still loads, so this is the one mechanism that splits the file without
# breaking the "opens over file:// with no server" promise above.
#
# `history.js` is the resident core: event list, club trajectories, the naming
# tables, the precomputed Trends answers and the two indices that keep the
# panel drawable without faulting anything (`rostByClub`, `gameSides`). It
# arrives in the background right after first paint. Everything else waits to
# be asked for, one bucket per fault:
#
#   t/<season>.js   a tournament's pools and bracket
#   p/<pid % 32>.js one player's rating trajectory
#   r/<bucket>.js   one club's rosters, every season of them, plus the names
#   g/<season>.js   the games behind one expanded event row
#
# What this buys: the corpus was 16 MB raw / 5.0 MB gzipped and loaded in full
# on every visit. The core is 0.5 MB gzipped, and a reader who never opens a
# panel never pays for the rest. See analysis/history_split.py for why the
# split is possible at all — Trends and the expandable-row test both used to
# need the whole player corpus, and both are precomputed there now.
HIST_OUT = OUT.parent / "history.js"
HIST_GLOBAL = "__USAU_HISTORY__"

# Bucketed by SEASON, not by event. Per-event files would be 3,810 of them at
# ~170 bytes gzipped each, where request overhead costs more than the body and
# every rebuild churns the whole directory; per-season is ten files of 12-65 KB
# and a session that stays inside one year pays once.
TDET_DIR = OUT.parent / "t"
TDET_GLOBAL = "__USAU_TDET__"

# The three history tiers. Each bucket file assigns its own key on the tier's
# global, so nothing assumes load order and two buckets in flight cannot race.
PLAY_DIR = OUT.parent / "p"
PLAY_GLOBAL = "__USAU_PLAY__"
ROST_DIR = OUT.parent / "r"
ROST_GLOBAL = "__USAU_ROST__"
GAME_DIR = OUT.parent / "g"
GAME_GLOBAL = "__USAU_GAME__"

# The player table's display floor, matching the ranking convention: below 30
# games a rating still sits inside the engine's provisional window.
MIN_GAMES = 30


def load_csv(name):
    p = DATA_DIR / name
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def bucket_urls(directory, buckets, version):
    """Bucket key -> cache-busted URL, relative to the page."""
    return {
        str(k): f"{directory.name}/{k}.js?v={version}" for k in sorted(buckets)
    }

def content_version(paths):
    """Fingerprint every source that can change a sidecar's index contract."""
    digest = hashlib.sha256()
    for path in paths:
        if not path.exists():
            continue
        digest.update(path.name.encode())
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()[:12]


def write_buckets(directory, global_name, buckets):
    """One file per bucket. Each creates the tier's global itself and writes
    only its own key, so nothing assumes load order and two buckets in flight
    cannot clobber each other. Files for keys that no longer exist are swept:
    a stale bucket the page never asks for is still a stale bucket in git."""
    directory.mkdir(parents=True, exist_ok=True)
    for key, part in sorted(buckets.items()):
        (directory / f"{key}.js").write_text(
            f"(window.{global_name}=window.{global_name}||{{}})[{key}]="
            + json.dumps(part, separators=(",", ":")) + ";\n")
    live = {str(k) for k in buckets}
    for stale in directory.glob("*.js"):
        if stale.stem not in live:
            stale.unlink()



def build():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    # Every event's recovered shape, for the Tournaments tab. Derived here
    # rather than replayed: analysis.tournaments reads the same schedule the
    # tracker does and infers pools and brackets from the results.
    tourneys = build_tournaments(con)

    all_players = load_csv("player_elo.csv")
    total_rated = len(all_players)
    players = [r for r in all_players if int(r["games"]) >= MIN_GAMES]
    clubs = {
        "completed": load_csv("team_elo.csv"),
        "best": load_csv("team_elo_best.csv"),
        "upcoming": load_csv("team_elo_upcoming.csv"),
    }


    # Trajectories for the drill-down, written by analysis.rankings from the
    # same replay that produced the CSVs. Optional: if it is missing the page
    # still builds, it just has nothing to open when a name is clicked.
    hist_path = DATA_DIR / "history.json"
    history = (json.loads(hist_path.read_text()) if hist_path.exists()
               else {"events": [], "players": {}, "teams": {}})
    asset_version = content_version((DB_PATH, hist_path))

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
    loo_path = DATA_DIR / "player_loo.csv"
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

    # Tournament detail out of the payload and into per-season files. Keys are
    # event indices into `tourneys.events`, which stays inline: the list, the
    # filters, the series table and the champion column all read it, so the
    # index never waits on a fault. Only `drawTournament` needs a bucket, and
    # only for the one season it is drawing.
    tdetail = tourneys.pop("detail")
    tbuckets = collections.defaultdict(dict)
    for ix, det in tdetail.items():
        tbuckets[tourneys["events"][int(ix)][2]][ix] = det

    # Field strength, appended to each event row rather than built into it:
    # tournaments.py recovers an event's SHAPE from the schedule and never
    # touches ratings, and that is worth keeping true. Rows gain [11] = score
    # and [12] = tier letter, or nothing at all where there is no answer.
    verdicts, strength_cuts = classify_strength(history, tourneys["events"])
    for row, verdict in zip(tourneys["events"], verdicts):
        row.extend(verdict if verdict else [None, ""])
    # [13] = 1 where the event has no PLAYED game yet, which is what the
    # Tournaments filter means by "upcoming". Keyed on results rather than on
    # the calendar deliberately: the mirror publishes scores for events dated
    # ahead of today (Vacationland 2026 carries 40 of them three days out), so
    # a date test would file a tournament you can already read results for
    # under "nothing has happened yet". Results are also what the page can
    # actually show, which is the question a reader is really asking.
    played = {eid for (eid,) in con.execute(
        """SELECT DISTINCT event_id FROM games
           WHERE home_score IS NOT NULL AND away_score IS NOT NULL""")}
    for row in tourneys["events"]:
        row.append(0 if row[0] in played else 1)
    tourneys["strengthNotes"] = FIELD_TIER_NAMES
    # division -> [[score, letter], ...]. Published because the bar is not the
    # same in every division and a letter with a hidden threshold is a riddle.
    tourneys["strengthCuts"] = strength_cuts

    # The same two facts, re-keyed by HISTORY event index so the drill-down can
    # print them per row without touching the tournaments payload. The champion
    # arrives as a display name and is stored as the model key the panel links
    # on, since three divisions share printed names.
    hev_by_key = {(e[0], e[1], e[2], e[3]): i
                  for i, e in enumerate(history.get("events", []))}
    alias = history.get("teamKey", {})
    event_meta = {}
    for row, verdict in zip(tourneys["events"], verdicts):
        i = hev_by_key.get((row[4][:10], row[1][:46], row[2], row[3]))
        if i is None:
            continue
        name = tourneys["teams"][row[10]] if row[10] >= 0 else None
        event_meta[i] = [(verdict or [None, ""])[0], (verdict or [None, ""])[1],
                         alias.get(name, name)]

    # The trajectory corpus, sliced into a resident core and three lazy tiers.
    # Player names come from the ranked table rather than history.json's own
    # name pool: the two key sets are identical (every rated trajectory is a
    # >= MIN_GAMES player), so the pool is only ever needed alongside a roster.
    hcore, hplay, hrost, hgame = split_history(
        history, {r["player_id"]: r["player"] for r in players}, genders,
        event_meta)
    metrics_path = DATA_DIR / "metrics.json"
    model_metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}

    payload = {
        "generated": date.today().isoformat(),
        "model": RATING_NAME,
        "metrics": model_metrics,
        "ratingPeriod": model_metrics.get("period"),
        "minGames": MIN_GAMES,
        "totalRated": total_rated,
        "scale": PUBLISHED["division_scale"]["club-men"],
        # The player tier keys on `pid % buckets`, which the page reproduces
        # directly. Clubs cannot — their bucket rides on rostByClub instead.
        "buckets": HIST_BUCKETS,
        # Which divisions the Rankings and Trends pickers may offer. Team
        # tables include the European divisions; the USAU tournament browser
        # has its own narrower eventDivs list.
        "divs": sorted(
            {ev[3] for ev in tourneys["events"]}
            | {DIVCODE.get(r["division"], 0)
               for rows in clubs.values() for r in rows}
        ),
        "eventDivs": sorted({ev[3] for ev in tourneys["events"]}),
        "players": [[r["player"], float(r["elo"]), float(r["lo90"]), float(r["hi90"]),
                     int(r["games"]), r["last_club"], int(r["last_season"]),
                     int(r["rank"]), r["player_id"], genders.get(r["player_id"], 0),
                     int(r["divisions"]), int(r["divisions_now"]),
                     loo_code(r["player_id"])]
                    for r in players],
        # `genders` used to ride here for Trends, which walked every
        # trajectory to find its own top 25. Trends is precomputed now, and
        # the table's own filter reads the code off each player row, so the
        # 356 KB map has no reader left.
        # Club rows carry both names: `club` as USAU prints it and `club_key`,
        # the model identity the drill-down opens on. Ranks are per division,
        # so the table shows one division at a time.
        "clubs": {k: [[int(r["rank"]), r["club"], float(r["elo"]),
                       int(r["roster_size"]), r["roster_event"],
                       DIVCODE.get(r["division"], 0), r["club_key"]]
                      for r in v] for k, v in clubs.items()},
        # All four of these load after first paint; see HIST_OUT and the
        # tier constants above. `history` is the inline escape hatch: set it
        # and the page skips the sidecar entirely.
        "history": None,
        "historyJs": f"{HIST_OUT.name}?v={asset_version}",
        "tourneys": tourneys,
        # Every sidecar URL carries one build fingerprint. Their payloads share
        # integer indices, so mixing a cached old core with new player buckets
        # produces plausible-looking but false events and branching curves.
        "tourneyJs": bucket_urls(TDET_DIR, tbuckets, asset_version),
        "playJs": bucket_urls(PLAY_DIR, hplay, asset_version),
        "rostJs": bucket_urls(ROST_DIR, hrost, asset_version),
        "gameJs": bucket_urls(GAME_DIR, hgame, asset_version),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    HIST_OUT.write_text(
        f"window.{HIST_GLOBAL}=" + json.dumps(hcore, separators=(",", ":")) + ";\n")
    write_buckets(TDET_DIR, TDET_GLOBAL, tbuckets)
    write_buckets(PLAY_DIR, PLAY_GLOBAL, hplay)
    write_buckets(ROST_DIR, ROST_GLOBAL, hrost)
    write_buckets(GAME_DIR, GAME_GLOBAL, hgame)
    # Two figures in the Trends note describe the payload, so they are measured
    # off it rather than typed into the prose. Both had gone stale by the time
    # college women's arrived — the drawn-line range read "67 to 164" while a
    # view now reaches 192, and the multi-division club count said 301 against
    # an actual 461. history_split owns the encoding, so it does the counting.
    lo, hi = trend_series_range(hcore)
    OUT.write_text(TEMPLATE
                   .replace("__DATA__", json.dumps(payload, separators=(",", ":")))
                   .replace("__TRENDN__", f"{lo} to {hi}")
                   .replace("__MULTIDIV__", f"{multi_division_clubs(hcore):,}")
                   .replace("Elo", RATING_NAME))
    (OUT.parent / ".nojekyll").write_text("")
    kb, hkb = OUT.stat().st_size / 1024, HIST_OUT.stat().st_size / 1024
    print(f"wrote {OUT} ({kb:,.0f} KB) + {HIST_OUT.name} ({hkb:,.0f} KB) + .nojekyll")
    print(f"  first paint needs {kb:,.0f} KB, then {hkb:,.0f} KB of core; "
          f"the rest is faulted in per panel:")
    for d in (TDET_DIR, PLAY_DIR, ROST_DIR, GAME_DIR):
        files = sorted(d.glob("*.js"))
        sizes = [p.stat().st_size / 1024 for p in files]
        print(f"    {d.name}/  {len(files):>3} buckets, "
              f"{sum(sizes):>6,.0f} KB total, {max(sizes):>4,.0f} KB worst fault")
    print(f"  {len(payload['players']):,} players (>={MIN_GAMES} games), "
          f"{len(clubs['completed'])} clubs")
    print(f"  {len(tourneys['events']):,} tournaments in "
          f"{len(tourneys['series']):,} series")
    con.close()


TEMPLATE = r"""<!doctype html>
<html lang="en" class="booting">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USAU + International Player-Elo Rankings</title>
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
/* Boot state. First paint lands in ~300ms but the inline payload is a couple
   of megabytes behind it, so without this the page sits there showing empty
   tables and reads as broken rather than as loading. The class is on <html>
   from the markup and removed once the data is drawn, which covers exactly
   the window where the document is still downloading. */
#boot{display:none;padding:40px 0 60px;max-width:560px}
html.booting #boot{display:block}
html.booting main > section{display:none !important}
html.booting nav button{pointer-events:none;opacity:.45}
#boot h2{font-size:15px;margin:0 0 6px;font-weight:600}
#boot p{margin:0;font-size:13px;color:var(--ink-3);line-height:1.6}
#boot .track{height:3px;border-radius:2px;background:var(--line);
  overflow:hidden;margin:16px 0 12px}
#boot .track i{display:block;height:100%;width:38%;border-radius:2px;
  background:var(--accent);animation:boot 1.15s ease-in-out infinite}
@keyframes boot{0%{transform:translateX(-105%)}100%{transform:translateX(305%)}}
@media (prefers-reduced-motion:reduce){#boot .track i{animation:none;width:100%}}
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
/* Pinned players: a shortlist built from the players table, shown as a
   sticky sidebar and carried in the URL so the exact list can be shared. */
.pwrap{display:flex;gap:16px;align-items:flex-start}
.ptblwrap{flex:1;min-width:0}
th.pin,td.pin{width:24px;padding:7px 2px;text-align:center}
button.pinbtn{background:none;border:0;cursor:pointer;font-size:14px;opacity:.32;
  padding:0;line-height:1}
button.pinbtn:hover{opacity:.75}
button.pinbtn.on{opacity:1}
#pinSidebar{width:270px;flex:none;background:var(--surface);border:1px solid var(--line);
  border-radius:10px;padding:12px 13px;position:sticky;top:14px}
#pinSidebar.collapsed{display:none}
.pinhead{display:flex;align-items:center;justify-content:space-between;gap:8px;
  margin-bottom:8px}
.pinhead h3{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--ink-3);font-weight:600}
.pinactions{display:flex;gap:6px}
.pinactions button.act{font-size:11.5px;padding:3px 8px}
.pinrow{display:flex;align-items:center;gap:8px;padding:5px 0;
  border-bottom:1px solid var(--line);font-size:13.5px}
.pinrow:last-child{border-bottom:0}
.pinrow .nmlink{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pinrow .n{font-family:var(--mono);font-size:12px;color:var(--ink-2)}
button.unpin{background:none;border:0;color:var(--ink-3);cursor:pointer;font-size:15px;
  line-height:1;padding:0 2px;flex:none}
button.unpin:hover{color:var(--lose)}
.pinaddwrap{position:relative;margin-top:10px}
#pinadd{width:100%;box-sizing:border-box;font-size:13px;padding:5px 8px}
#pinaddResults{display:none;position:absolute;left:0;right:0;top:100%;margin-top:2px;
  background:var(--surface);border:1px solid var(--line-strong);border-radius:7px;
  max-height:220px;overflow-y:auto;z-index:5;box-shadow:0 6px 18px rgba(0,0,0,.16)}
#pinaddResults.on{display:block}
.pinhit{padding:6px 9px;cursor:pointer;font-size:13px}
.pinhit:hover{background:var(--chip)}
.pinhit .muted{font-size:11.5px}
@media (max-width:860px){
  .pwrap{flex-direction:column}
  #pinSidebar{width:100%;position:static}
}
/* A simulated result is a coin flip, not a played game. Dashed and desaturated
   so it can never be mistaken for a real one you typed in. */
.t.w.simd{background:color-mix(in srgb,var(--ink-3) 13%,transparent) !important;
  border-style:dashed !important;border-color:var(--ink-3) !important}
.t.w.simd .nm::after,.g .t.w.simd > span:first-child::after{content:' ~';color:var(--ink-3)}
.count{font-size:12.5px;color:var(--ink-3)}
/* Pool cards and bracket lines, shared by every recovered tournament shape */
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
.dhead{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.dhead h2{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
button.dpin{flex:none}
button.dpin.on{border-color:var(--accent);color:var(--ink)}
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
.tchart .xh{fill:transparent;cursor:pointer}
.tchart .yg{stroke:var(--line-strong);stroke-width:1;opacity:0}
.tchart .yg.on{opacity:1}
.tchart .yg.locked{stroke:var(--accent);stroke-width:2;opacity:1}
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
/* Field strength reads as a LADDER, so unlike the series tag it is shaded by
   rank rather than flagged at the top: S is solid, D is barely there. The
   letter carries the meaning; the fill just makes a column of them scannable. */
.fs{display:inline-block;min-width:15px;text-align:center;font-size:10.5px;
  font-weight:700;padding:1px 5px;border-radius:4px;font-family:var(--mono);
  border:1px solid transparent}
.fsS{background:var(--accent);color:var(--bg)}
.fsA{background:color-mix(in srgb,var(--accent) 45%,transparent);color:var(--ink)}
.fsB{background:color-mix(in srgb,var(--accent) 22%,transparent)}
.fsC{background:var(--chip);color:var(--ink-3)}
.fsD{color:var(--ink-3);border-color:var(--line)}
/* The division's ladder under the header. Rungs the event did not reach are
   dimmed, so the eye lands on the one it did without hunting. */
.fsline{margin-top:5px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.fsbar{display:inline-flex;align-items:center;gap:3px;opacity:.5}
.fsbar .fs{min-width:13px;padding:0 4px;font-size:9.5px}
.fsbar .fs.now{outline:2px solid var(--accent);outline-offset:1px}
.fsbar i{font-style:normal;font-family:var(--mono);font-size:10px;
  color:var(--ink-3);margin-right:5px}
.crown{color:var(--accent);font-weight:600}
#tvhead{margin:0 0 14px}
#tvhead h2{font-size:20px;margin:0 0 3px;letter-spacing:-.01em}
#tvhead .meta{color:var(--ink-3);font-size:13px}
.sect{font-size:13px;margin:24px 0 9px;color:var(--ink-2)}
.sect .muted{font-weight:400}
/* The series-history division picker sits inside its own heading, so it takes
   the heading's scale rather than the filter bars' 14px. */
.sect select{font:inherit;font-size:12px;padding:2px 6px;margin-left:9px;
  vertical-align:baseline;border-radius:6px}
/* Bracket geometry, parameterised. A recovered bracket runs from one round to
   seven, so columns, rows and the connector pitch are all set inline per
   tournament. */
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
  <h1>USAU + International Player-Elo Rankings</h1>
  <div class="sub" id="sub"></div>
</header>
<nav>
  <button data-t="rankings" class="on">Rankings</button>
  <button data-t="events">Tournaments</button>
  <button data-t="trends">Trends</button>
</nav>
<main>
<div id="boot">
  <h2>Loading rankings&hellip;</h2>
  <div class="track"><i></i></div>
  <p>Every rating, roster and result is embedded in this page, so it works
  offline once loaded. Season-by-season trajectories arrive separately, just
  after the tables appear.</p>
</div>


<section id="rankings" class="on">
  <div class="bar">
    <select id="rtype">
      <option value="club">Club teams</option>
      <option value="player">Players</option>
    </select>
    <select id="rdiv"></select>
    <select id="ryear">
      <option value="all">All years</option>
    </select>
  </div>
  <div id="clubRankings">
    <div class="bar">
      <input type="search" id="cq" placeholder="Search club…" autocomplete="off">
      <select id="basis">
        <option value="completed">Most recent completed roster</option>
        <option value="best">Best full-strength roster of 2026</option>
        <option value="upcoming">Next event roster</option>
      </select>
      <span class="count" id="ccount"></span>
    </div>
    <table><thead><tr>
      <th class="n">#</th><th>Club</th><th>Division</th><th class="n">Elo</th>
      <th class="n">Roster</th><th>Rated off</th>
    </tr></thead><tbody id="ctb"></tbody></table>
    <p class="note" id="cnote"></p>
  </div>

  <div id="playerRankings" hidden>
    <div class="bar">
      <input type="search" id="q" placeholder="Search player or club…" autocomplete="off">
      <label class="chk"><input type="checkbox" id="only26" checked> Current 2026 rosters only</label>
      <select id="ming">
        <option value="30">30+ games</option>
        <option value="60">60+ games</option>
        <option value="120">120+ games</option>
        <option value="200">200+ games</option>
      </select>
      <select id="pgen">
        <option value="all">All genders</option>
        <option value="1">Male-matching</option>
        <option value="2">Female-matching</option>
      </select>
      <span class="count" id="pcount"></span>
      <button class="act" id="pinToggle" title="Show or hide your pinned players list">
        &#128204; Pinned <span id="pinbadge">0</span></button>
    </div>
    <div class="pwrap">
    <div class="ptblwrap">
    <table><thead><tr>
      <th class="pin"></th>
      <th class="n">#</th><th>Player</th><th class="n">Elo</th><th>90% band</th>
      <th class="n">G</th><th>Last club</th><th class="n">Yr</th>
    </tr></thead><tbody id="ptb"></tbody></table>
    <p class="note" id="pnote"></p>
    </div>
    <aside id="pinSidebar" class="collapsed">
      <div class="pinhead">
        <h3>Pinned players</h3>
        <div class="pinactions">
          <button class="act" id="pinlink">Copy link</button>
          <button class="act" id="pinclear">Clear</button>
        </div>
      </div>
      <div id="pinlist"></div>
      <div class="pinaddwrap">
        <input type="search" id="pinadd" placeholder="Add another player…" autocomplete="off">
        <div id="pinaddResults"></div>
      </div>
      <p class="note" style="margin:8px 0 0">Pins are saved in this browser and baked into
      the page URL, so copying the address bar shares this exact list.</p>
    </aside>
    </div>
  </div>
</section>

<section id="events">
  <div id="tlist">
    <div class="bar">
      <input type="search" id="eq" placeholder="Search tournament, city…" autocomplete="off">
      <select id="ediv"></select>
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
      <select id="estr">
        <option value="all">Any field</option>
        <option value="S">S — championship-grade</option>
        <option value="A">A — elite</option>
        <option value="B">B — strong</option>
        <option value="C">C — some ranked clubs</option>
        <option value="D">D — no ranked clubs</option>
      </select>
      <select id="esort">
        <option value="date">Newest first</option>
        <option value="str">Hardest field first</option>
        <option value="region">Region, then newest</option>
      </select>
      <select id="estate" aria-label="Filter by completion">
        <option value="all">All events</option>
        <option value="done">Completed</option>
        <option value="up">Upcoming</option>
      </select>
      <span class="count" id="ecount"></span>
    </div>
    <table class="evtbl"><thead><tr>
      <th>Dates</th><th>Tournament</th><th>Division</th>
      <th class="n">Teams</th><th class="n">Field</th><th>Champion</th>
      <th class="n">Editions</th>
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
    <select id="tdiv"></select>
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


</main>
<script>
const D = __DATA__;
const IS_GLICKO = D.model.startsWith('Glicko');
const PERIOD_LABEL = {
  game: 'one game',
  day: 'one tournament day',
  tournament: 'one tournament',
}[D.ratingPeriod] || '';
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct = v => (v*100).toFixed(1) + '%';
// Division wording lives HERE, once. NDIV describes the divisions a reader
// can actually select in Rankings and Trends, not registered-but-unplayed
// divisions. The Tournaments tab remains USAU-only.
const NDIV = 'eighteen';
const NDIV_LIST = "USAU club men's, mixed and women's; college men's and " +
  "women's and their D-III counterparts; men's, women's and mixed at both " +
  "masters and grand masters; great grand masters men's and women's; plus " +
  "European open, mixed and women's";

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
function drawRankings() {
  const players = $('#rtype').value === 'player';
  $('#clubRankings').hidden = players;
  $('#playerRankings').hidden = !players;
  if (players) drawPlayers(); else drawClubs();
}
$('#rtype').onchange = drawRankings;
$('#rdiv').onchange = drawRankings;
$('#ryear').onchange = drawRankings;

const heldOut = D.metrics.held_out_2024_2025;
$('#sub').textContent =
  `Every player carries a personal Elo across seasons; a club's rating is the ` +
  `softmax-weighted mean of its event roster. Rankings and Trends span ` +
  `${NDIV} USAU and EUF divisions — ${NDIV_LIST}. The Tournaments browser is ` +
  `USAU-only.` +
  (IS_GLICKO && PERIOD_LABEL ? ` Ratings update after ${PERIOD_LABEL}.` : '') +
  (heldOut?.n ? ` Held-out 2024–25: ${(heldOut.accuracy * 100).toFixed(1)}% ` +
    `accuracy · ${heldOut.logloss.toFixed(3)} log loss over ` +
    `${heldOut.n.toLocaleString()} games.` : '') +
  ` Generated ${D.generated || ''}.`;

/* ---------- clubs ---------- */
const CNOTE = {
  completed: 'Each club rated off the most recent event it has actually finished. ' +
    'This is the default published table.',
  best: "Each club rated off its strongest roster of 2026 that was at least 80% the size " +
    "of its own largest squad. The floor matters: without it, taking a max over rosters " +
    "picks the smallest one, because a mean over an elite subset beats a mean over a full squad.",
  upcoming: 'Each club rated off the roster it has registered for its next event. ' +
    'Rosters post weeks ahead, so these are provisional until the games are played — ' +
    'but for a club that fielded a B-squad last time out, this is the truer number. ' +
    'This table empties out between events: a club with nothing registered is not in it.'
};
/* EUCS does not distinguish these roster bases. */
const EU_ROSTER_NOTE =
  ' EUCS Ranking publishes season rosters rather than event-specific completed ' +
  'and upcoming rosters. Its captured season roster is used in the completed ' +
  'and best views; European teams are omitted from the next-event view.';
const COLLEGE_NOTE =
  `All ${NDIV} divisions share one rating scale, so the unfiltered view is one ` +
  'overall list. European teams enter through EUCS results and captured season ' +
  'rosters; a European name reuses a USAU identity only when that exact name is ' +
  'unique in both corpora and appears in both during the same season. Treat the ' +
  'cross-continent order as provisional: EUF publishes no stable player IDs. ' +
  'College roster bases also mean less because a squad is often registered for ' +
  'the season rather than the event.';
function drawClubs() {
  const basis = $('#basis').value;
  const year = $('#ryear').value;
  const historical = year !== 'all';
  const div = $('#rdiv').value;
  const q = $('#cq').value.trim().toLowerCase();
  $('#basis').disabled = historical;

  if (historical && !HREADY) {
    $('#ccount').textContent = 'Loading club history…';
    $('#cnote').textContent = 'Loading season data…';
    return;
  }

  // The number is the club's position in the selected population, assigned
  // before search. Searching is a lookup, not a re-ranking.
  let pop;
  if (historical) {
    const y = +year;
    pop = Object.entries(H.teams || {}).map(([key, entry]) => {
      let snap = null;
      for (const point of decode(entry)) {
        if (point.season === y && (div === 'all' || point.div === +div)) {
          snap = point;
        }
      }
      return snap ? {key, name: clubLabel(key), snap} : null;
    }).filter(Boolean);
    pop.sort((a, b) => b.snap.elo - a.snap.elo ||
      a.key.localeCompare(b.key));
  } else {
    pop = (D.clubs[basis] || [])
      .filter(r => div === 'all' || r[5] === +div)
      .slice().sort((a, b) => b[2] - a[2])
      .map(r => ({row: r}));
  }

  const rankOf = new Map();
  pop.forEach((r, i) => rankOf.set(r, i + 1));
  const rows = q ? pop.filter(r => {
    const name = historical ? r.name : r.row[1];
    const event = historical ? r.snap.event : r.row[4];
    return name.toLowerCase().includes(q) ||
      String(event).toLowerCase().includes(q);
  }) : pop;
  $('#ctb').innerHTML = rows.map(r => {
    if (historical) {
      const p = r.snap, rank = rankOf.get(r);
      return `<tr><td class="rk" title="#${rank} of ${pop.length.toLocaleString()} clubs in ${year}">` +
        `${rank}</td>` +
        `<td><span class="nmlink" data-club="${esc(r.key)}">${esc(r.name)}</span></td>` +
        `<td><span class="tag">${esc(EDIVL[p.div] || '')}</span></td>` +
        `<td class="n">${p.elo.toFixed(0)}</td><td class="n">${p.n ?? '—'}</td>` +
        `<td class="muted" style="font-size:13px">${esc(p.event)}</td></tr>`;
    }
    const p = r.row;
    const scope = div === 'all' ? 'the overall list' : `${DIVLABEL[p[5]]} clubs`;
    return `<tr><td class="rk" title="#${rankOf.get(r)} of ${scope}">` +
      `${rankOf.get(r)}</td>` +
      `<td><span class="nmlink" data-club="${esc(p[6])}">${esc(p[1])}</span></td>` +
      `<td><span class="tag">${esc(EDIVL[p[5]] || '')}</span></td>` +
      `<td class="n">${p[2].toFixed(0)}</td><td class="n">${p[3]}</td>` +
      `<td class="muted" style="font-size:13px">${esc(p[4])}</td></tr>`;
  }).join('');
  const what = div === 'all' ? 'clubs' : `${DIVLABEL[div]} clubs`;
  $('#ccount').textContent = q
    ? `${rows.length} of ${pop.length} ${what} match`
    : `${pop.length.toLocaleString()} ${what}`;
  $('#cnote').textContent = (historical
    ? `Showing each club after its last ${year} event in ` +
      `${div === 'all' ? 'all divisions' : EDIVL[div]}.`
    : CNOTE[basis] + ' ' + COLLEGE_NOTE) + EU_ROSTER_NOTE;
}
['input', 'change'].forEach(e => $('#cq').addEventListener(e, drawClubs));
$('#basis').onchange = drawClubs;

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
const PYEARS = new Map();
let ONLY26_BEFORE_YEAR = true;
function loadAllPlayerHistory(done) {
  const keys = Object.keys(D.playJs || {});
  let left = keys.length, ok = true;
  if (!left) { done(true); return; }
  keys.forEach(key => faultTier('p', key, good => {
    ok = ok && good;
    if (--left === 0) done(ok);
  }));
}
function playerYearMap(pid) {
  pid = String(pid);
  // A roster can ask for a season before this player's lazy bucket lands.
  // Do not memoise that temporary absence as an empty career.
  if (!Object.prototype.hasOwnProperty.call(PLAY, pid)) return new Map();
  if (PYEARS.has(pid)) return PYEARS.get(pid);
  const out = new Map();
  for (const point of decode(PLAY[pid])) {
    // Trajectories are chronological, so the last point wins for a
    // year/division and is the player's Elo after that year's last event.
    out.set(`${point.season}|${point.div}`, point);
    out.set(String(point.season), point);
  }
  PYEARS.set(pid, out);
  return out;
}
function historicalPlayer(p, year, div) {
  return playerYearMap(p[8]).get(div === 'all'
    ? String(year) : `${year}|${div}`) || null;
}
function drawPlayers() {
  const q = $('#q').value.trim().toLowerCase();
  const year = $('#ryear').value;
  const historical = year !== 'all';
  const only26Box = $('#only26');
  if (historical) {
    if (!only26Box.disabled) ONLY26_BEFORE_YEAR = only26Box.checked;
    only26Box.checked = false;
    only26Box.disabled = true;
  } else {
    if (only26Box.disabled) only26Box.checked = ONLY26_BEFORE_YEAR;
    only26Box.disabled = false;
  }
  const only26 = !historical && only26Box.checked;
  const ming = +$('#ming').value;
  const gen = $('#pgen').value, div = $('#rdiv').value;

  if (historical && !HREADY) {
    $('#pcount').textContent = 'Loading player history…';
    $('#pnote').textContent = 'Loading season data…';
    return;
  }
  if (historical &&
      !Object.keys(D.playJs || {}).every(k => tierState('p', k) === 'ok')) {
    $('#pcount').textContent = 'Loading player history…';
    $('#pnote').textContent = 'Loading season data…';
    loadAllPlayerHistory(ok => {
      if (ok) drawPlayers();
      else $('#pnote').textContent =
        'The season ratings could not be loaded.';
    });
    return;
  }

  // Rank is a property of the player within the POPULATION the toggles
  // define, so it is assigned before the search runs. Searching is a lookup,
  // not a re-ranking: results come back sparse.
  let pop = D.players.filter(p => p[4] >= ming);
  if (only26) pop = pop.filter(p => p[6] === 2026);
  if (gen !== 'all') pop = pop.filter(p => p[9] === +gen);

  if (historical) {
    const y = +year;
    pop = pop.map(p => ({p, snap: historicalPlayer(p, y, div)}))
      .filter(r => r.snap);
    pop.sort((a, b) => b.snap.elo - a.snap.elo ||
      String(a.p[8]).localeCompare(String(b.p[8]), undefined, {numeric: true}));
  } else {
    // Division is a bitmask of where the player turned out, and WHICH mask
    // depends on the current-roster toggle.
    if (div !== 'all') {
      const bit = 1 << +div;
      pop = pop.filter(p => (only26 ? p[11] : p[10]) & bit);
    }
    pop = pop.map(p => ({p, snap: null}));
  }

  const rankOf = new Map();
  pop.forEach((r, i) => rankOf.set(r, i + 1));
  const rows = q ? pop.filter(r => r.p[0].toLowerCase().includes(q) ||
                                   String(historical ? clubLabel(r.snap.club) : r.p[5])
                                     .toLowerCase().includes(q))
                 : pop;
  const shown = historical ? rows : rows.slice(0, 300);
  $('#ptb').innerHTML = shown.map(r => {
    const p = r.p, snap = r.snap;
    const elo = snap ? snap.elo : p[1];
    const club = snap ? clubLabel(snap.club) : p[5];
    const yr = snap ? snap.season : p[6];
    const rank = rankOf.get(r);
    const rankTitle = snap
      ? `#${rank} of ${pop.length.toLocaleString()} players in ${year}`
      : `#${p[7]} of all ${D.totalRated.toLocaleString()} rated players`;
    return `<tr><td class="pin"><button class="pinbtn${PINNED.has(String(p[8])) ? ' on' : ''}" ` +
      `data-pinid="${p[8]}" title="${PINNED.has(String(p[8])) ? 'Remove from pinned list' : 'Pin to sidebar list'}">` +
      `&#128204;</button></td>` +
      `<td class="rk" title="${rankTitle}">${rank}</td>` +
      `<td><span class="nmlink" data-pid="${p[8]}">${esc(p[0])}</span></td>` +
      `<td class="n">${elo.toFixed(0)}</td>` +
      (snap ? `<td class="band muted" title="Historical Elo; current uncertainty band not shown">—</td>` :
        `<td class="band ${LOOCLASS[p[12]] || ''}" title="${esc(LOOTIP[p[12]] || '')}">` +
        `[${p[2].toFixed(0)}, ${p[3].toFixed(0)}]${p[12] === 0 ? ' <span class="unsup">?</span>' : ''}</td>`) +
      `<td class="n">${p[4]}</td><td class="muted" style="font-size:13px">${esc(club)}</td>` +
      `<td class="n">${yr}</td></tr>`;
  }).join('');
  $('#pcount').textContent = q
    ? `${rows.length.toLocaleString()} of ${pop.length.toLocaleString()} match` +
      (!historical && rows.length > 300 ? ' — showing first 300' : '')
    : `${pop.length.toLocaleString()} players` +
      (!historical && pop.length > 300 ? ' — showing first 300' : '');
  if (historical) {
    const division = div === 'all' ? 'all divisions' : EDIVL[div];
    $('#pnote').textContent =
      `Showing each player's Elo after their last ${year} event in ${division}. ` +
      `Historical uncertainty bands are not available.`;
  }
}
['input','change'].forEach(e => {
  $('#q').addEventListener(e, drawPlayers);
  $('#only26').addEventListener(e, drawPlayers);
  $('#ming').addEventListener(e, drawPlayers);
  $('#pgen').addEventListener(e, drawPlayers);
});
$('#pnote').textContent = IS_GLICKO
  ? `Searching does not renumber anything — a player keeps the rank they hold in ` +
    `the current list. The toggles do change the population and therefore the rank; ` +
    `hover one to see its denominator. Ratings update after ${PERIOD_LABEL}; ` +
    `all games inside that period use its opening ratings. Bands are native 90% ` +
    `Glicko-2 intervals: rating deviation shrinks as results accumulate and ` +
    `expands during inactivity. ` +
    `Players below ${D.minGames} games are omitted because their estimates remain ` +
    `too uncertain for a useful ordered list. Ratings themselves do not decay, so ` +
    `\"2026 rosters only\" is on by default instead of mixing eras. A player carries ` +
    `one rating across every division; the division filters describe where they ` +
    `played, not separate ratings. All ${NDIV} divisions share one scale, bridged ` +
    `by the ${GENDER_NOTE}`
  : `Searching does not renumber anything — a player keeps the rank they hold in the ` +
    `current list, so results come back sparse. The toggles do change the rank, ` +
    `because they change who is being ranked; hover a rank to see the player's ` +
    `position across all ${D.totalRated.toLocaleString()} rated players. ` +
    `Bands are 90% intervals on the rating as an estimate of current skill. For ` +
    `the top 1,000 each rating was re-tested by dropping the player from every ` +
    `roster and replaying; the \"?\" marks results explained at least as well ` +
    `without that player. Players below ${D.minGames} games are omitted while ` +
    `the provisional multiplier is still moving their rating quickly. Ratings ` +
    `never decay, so \"2026 rosters only\" is on by default. A player carries one ` +
    `rating across every division; filters describe where they played. All ` +
    `${NDIV} divisions share one scale, bridged by the ${GENDER_NOTE}`;

/* ---------- drill-down: play history + rating curve ---------- */
/* The core arrives in a second file once the page is already usable, and the
   bulk — trajectories, rosters, games — waits to be asked for after that.
   That makes every binding here LATE: declared empty, filled by
   applyHistory() or by a bucket landing, and re-read by anything drawn in the
   meantime. HREADY is the guard each entry point checks; HFAILED means the
   core could not be loaded at all, which is worth saying out loud rather than
   showing an empty panel. */
let H = {events: [], teams: {}, teamKey: {}};
let TK = {};      // display name -> normalized club key
let TN = {};      // normalized club key -> current display spelling
let CN = [];      // affiliation index -> normalized club key
let BSEASON = null;   // the season BR applies to (the current one)
let HEV = [];     // [date, name, season, divisionCode]
let GC = [], GST = [];
let GCIX = new Map();
let GSIDE = {};   // eventIdx -> Set(club index), from the core
// eventIdx -> [field strength score, letter, champion club key]. Per event,
// so a club's history row can print the grade and a crown without any fault.
let EMETA = {};
let RBC = {};     // clubKey -> {b: roster bucket, evs: [eventIdx], has: Set}
let SEASONS = [], SIX = new Map(), DEFYEAR = 0;
let HREADY = false, HFAILED = false;

/* Filled by faulted buckets rather than by the core, and never reset: a
   bucket that has landed stays landed. PEOPLE/PPID GROW — each roster bucket
   carries its own slice of the name pool and mergeRosters rebases the indices
   as it appends, so a name in two buckets is simply stored twice. */
let PLAY = {};    // pid -> encoded trajectory              (tier p)
let ROST = {};    // "<clubKey>|<eventIdx>" -> people indices (tier r)
let BR = {};      // clubKey -> [event, date, people indices] (tier r)
let PEOPLE = [], PPID = [];
// Games behind each event, grouped by event index and stored once per game:
// [homeClubIx, awayClubIx, homeScore, awayScore, stageIx, homeDelta, awayDelta]
// against GC/GST. The deltas are what the game did to each CLUB's rating.
let GMS = {};     // eventIdx -> game rows                   (tier g)

function applyHistory(h) {
  H = h || H;
  TK = H.teamKey || {};
  TN = H.teamNames || {};
  CN = H.clubNames || [];
  BSEASON = H.bestSeason;
  HEV = H.events || [];
  GC = H.gameClubs || []; GST = H.gameStages || [];
  GCIX = new Map(GC.map((k, i) => [k, i]));
  // Who appears in an event's games. The event table marks a row expandable
  // only where the model scored games, and asking GMS for that would fault
  // every season of every panel just to draw the table.
  GSIDE = {};
  const sides = H.gameSides || {};
  for (const ev in sides) {
    const s = new Set();
    let i = 0;
    for (const d of sides[ev]) { i += d; s.add(i); }
    GSIDE[ev] = s;
  }
  EMETA = H.eventMeta || {};
  // A club's roster bucket rides on this entry rather than on a string hash
  // the emitter and the page both have to implement identically.
  RBC = {};
  const rbc = H.rostByClub || {};
  for (const ck in rbc) {
    const a = rbc[ck], evs = [];
    let i = 0;
    for (let k = 1; k < a.length; k++) { i += a[k]; evs.push(i); }
    RBC[ck] = {b: a[0], evs, has: new Set(evs)};
  }
  SEASONS = [...new Set(HEV.map(e => e[2]))].sort((a, b) => a - b);
  SIX = new Map(SEASONS.map((s, i) => [s, i]));
  DEFYEAR = SEASONS.length - 1;
  const ryear = $('#ryear');
  if (ryear) {
    const selected = ryear.value;
    ryear.innerHTML = '<option value="all">All years</option>' +
      [...SEASONS].reverse().map(y =>
        `<option value="${y}">${y}</option>`).join('');
    ryear.value = SEASONS.includes(+selected) ? selected : 'all';
  }
  for (const k in trendCache) delete trendCache[k];
  HREADY = true;
}

/* ---------- lazy tiers ---------- */
/* Four corpora sit beside the page and arrive only when something needs them:
   tournament shapes (t), player trajectories (p), club rosters (r) and the
   games behind an event row (g). All four load the same way — a classic
   <script>, because a file:// page cannot fetch() its own directory but can
   still load a script from it — and all four settle through faultTier.

   Each bucket file assigns its own key on the tier's global, so nothing
   assumes load order and two buckets in flight write different keys. */
const TIER = {
  t: {urls: 'tourneyJs', glob: '__USAU_TDET__', merge: p => Object.assign(EDET, p)},
  p: {urls: 'playJs',    glob: '__USAU_PLAY__', merge: p => Object.assign(PLAY, p)},
  r: {urls: 'rostJs',    glob: '__USAU_ROST__', merge: mergeRosters},
  g: {urls: 'gameJs',    glob: '__USAU_GAME__', merge: p => Object.assign(GMS, p)},
};
function playerBucket(pid) {
  const n = D.buckets || 1;
  return ((+pid % n) + n) % n;
}
const TSTATE = new Map();   // "<tier>/<key>" -> 'load' | 'ok' | 'fail'
const TWAIT = new Map();    // same id -> [callback], drained on settle
const tierState = (tier, key) => TSTATE.get(tier + '/' + key);

/* Roster buckets index their own name pool from 0, so merging one means
   appending its pool to the global arrays and rebasing every index against
   where that slice landed. Deltas are decoded here, once, rather than on
   every read. */
function mergeRosters(part) {
  const base = PEOPLE.length;
  for (const n of part.p) PEOPLE.push(n);
  for (const p of part.i) PPID.push(p);
  const abs = enc => {
    const out = [];
    let i = base;
    for (const d of enc) { i += d; out.push(i); }
    return out;
  };
  for (const rk in part.r) ROST[rk] = abs(part.r[rk]);
  for (const ck in part.b) {
    const e = part.b[ck];
    BR[ck] = [e[0], e[1], abs(e[2])];
  }
}

/* `done(ok)` fires once the bucket has settled, immediately if it already
   has. A settled-but-failed bucket is never re-requested: the callback would
   run synchronously and any caller that redraws on it would recurse. */
function faultTier(tier, key, done) {
  const id = tier + '/' + key, st = TSTATE.get(id);
  if (st === 'ok' || st === 'fail') { done(st === 'ok'); return; }
  if (!TWAIT.has(id)) TWAIT.set(id, []);
  TWAIT.get(id).push(done);
  if (st === 'load') return;
  const spec = TIER[tier], url = ((D[spec.urls] || {})[key]);
  const finish = ok => {
    TSTATE.set(id, ok ? 'ok' : 'fail');
    (TWAIT.get(id) || []).splice(0).forEach(fn => fn(ok));
  };
  if (!url) { finish(false); return; }
  TSTATE.set(id, 'load');
  const s = document.createElement('script');
  s.src = url;
  s.async = true;
  s.onload = () => {
    const bag = window[spec.glob], part = bag && bag[key];
    if (part) {
      spec.merge(part);
      delete bag[key];   // the live store owns it now
    }
    finish(!!part);
  };
  s.onerror = () => finish(false);
  document.head.appendChild(s);
}
// Per-event division tag in the drill-down table, indexed by DIVCODE. It used
// to be a 3-slot array with an `|| 'club'` fallback, which silently labelled
// every mixed and women's event as club men's the moment those divisions
// existed. Keep this the same length as DIVCODE.
const DIVTAG = ["men's", "college men's", "college men's D-III", 'mixed',
                "women's", "college women's", "college women's D-III",
                "masters men's", "masters women's", "masters mixed",
                "grand masters men's", "grand masters women's", "grand masters mixed",
                "great grand masters men's", "great grand masters women's",
                "great grand masters mixed", "Europe open", "Europe mixed",
                "Europe women's",
                'college mixed', 'high school boys', 'high school girls',
                'high school mixed', 'middle school boys', 'middle school girls',
                'middle school mixed', 'YCC U-20 boys', 'YCC U-20 girls',
                'YCC U-20 mixed', 'YCC U-17 boys', 'YCC U-17 girls',
                'YCC U-17 mixed', 'YCC U-15 boys', 'YCC U-15 girls',
                'YCC U-15 mixed', "beach men's", "beach women's", 'beach mixed',
                "beach masters men's", "beach masters women's", 'beach masters mixed',
                "beach grand masters men's", "beach grand masters women's",
                'beach grand masters mixed',
                "beach great grand masters men's", "beach great grand masters women's",
                'beach great grand masters mixed', 'beach legends mixed',
                "league men's", 'league mixed'];
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
/* Whether an event row is worth expanding, answered from the core index so
   that drawing a 60-row table costs no game buckets at all. */
function hasGames(ckey, evIdx) {
  const me = GCIX.get(ckey), s = GSIDE[evIdx];
  return me !== undefined && s !== undefined && s.has(me);
}
// player_id -> its row in the ranked table, so a drill-down rebuilds its own
// header instead of reading it off whichever element happened to be clicked.
const PBY = new Map(D.players.map(p => [String(p[8]), p]));
// H.teams is keyed on the lowercased model identity; never render that raw.
const clubLabel = k => TN[k] || k;

/* ---------- pinned players: a shortlist that travels in the URL ---------- */
// Pins live in `?p=id,id,id`, independent of the hash router (which owns
// #p//#c//#t deep links) — so a shared link can carry a shortlist and,
// separately, whatever hash the recipient's own click history lands on.
// localStorage is the "come back tomorrow" copy; the query string is what a
// shared link actually carries, and it wins over localStorage on first load.
let PINNED = new Set();
const PIN_CAP = 20;
const pinsFromUrl = new URLSearchParams(location.search).get('p');
(pinsFromUrl !== null ? pinsFromUrl : (localStorage.getItem('usau-pinned') || ''))
  .split(',').map(s => s.trim()).filter(Boolean).forEach(id => {
    if (PBY.has(id)) PINNED.add(id);
  });
function savePins() {
  const ids = [...PINNED];
  localStorage.setItem('usau-pinned', ids.join(','));
  const u = new URL(location.href);
  if (ids.length) u.searchParams.set('p', ids.join(',')); else u.searchParams.delete('p');
  history.replaceState(null, '', u.pathname + u.search + location.hash);
}
function setPinSidebarOpen(open) {
  $('#pinSidebar').classList.toggle('collapsed', !open);
  $('#pinToggle').classList.toggle('on', open);
  localStorage.setItem('usau-pinned-open', open ? '1' : '0');
}
function togglePin(id) {
  id = String(id);
  if (PINNED.has(id)) PINNED.delete(id);
  else { if (PINNED.size >= PIN_CAP || !PBY.has(id)) return; PINNED.add(id); }
  savePins();
  drawPlayers();
  renderPinSidebar();
  if (PINNED.size) setPinSidebarOpen(true);
}
function renderPinSidebar() {
  $('#pinbadge').textContent = PINNED.size;
  $('#pinlist').innerHTML = PINNED.size ? [...PINNED].map(id => {
    const p = PBY.get(id);
    return p ? `<div class="pinrow"><span class="nmlink" data-pid="${id}">${esc(p[0])}</span>` +
      `<span class="n">${p[1].toFixed(0)}</span>` +
      `<button class="unpin" data-unpin="${id}" title="Remove from list">&times;</button></div>` : '';
  }).join('') : `<p class="note" style="margin:2px 0 0">Nobody pinned yet. Click ` +
    `&#128204; beside a name in the table, or search below.</p>`;
}
$('#pinToggle').onclick = () => setPinSidebarOpen($('#pinSidebar').classList.contains('collapsed'));
$('#pinlist').addEventListener('click', e => {
  const un = e.target.closest('[data-unpin]'); if (un) { togglePin(un.dataset.unpin); return; }
  const nm = e.target.closest('[data-pid]'); if (nm) openDetail('p', nm.dataset.pid);
});
$('#pinclear').onclick = () => { PINNED.clear(); savePins(); drawPlayers(); renderPinSidebar(); };
$('#pinlink').onclick = () => {
  const b = $('#pinlink'), was = b.textContent;
  const flash = t => { b.textContent = t; setTimeout(() => { b.textContent = was; }, 1300); };
  navigator.clipboard.writeText(location.href).then(() => flash('Copied!'), () => {
    // Clipboard permission denied (older browser, non-secure context, or a
    // sandboxed embed) — select the address bar's worth of text instead of
    // failing silently, so the link can still be copied by hand.
    const r = document.createRange(); const sel = getSelection();
    const tmp = document.createElement('span'); tmp.textContent = location.href;
    tmp.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(tmp); r.selectNodeContents(tmp);
    sel.removeAllRanges(); sel.addRange(r);
    flash('Select & copy \u2318C');
    setTimeout(() => tmp.remove(), 1400);
  });
};
// Add-another-player: a small live search inside the sidebar itself, scoped
// to name only, so a pin can be added without touching the main table's
// filters — which may currently be hiding the very player being added.
$('#pinadd').addEventListener('input', () => {
  const q = $('#pinadd').value.trim().toLowerCase();
  const out = $('#pinaddResults');
  if (!q) { out.innerHTML = ''; out.classList.remove('on'); return; }
  const hits = D.players.filter(p => !PINNED.has(String(p[8])) &&
    p[0].toLowerCase().includes(q)).slice(0, 8);
  out.innerHTML = hits.length ? hits.map(p =>
    `<div class="pinhit" data-add="${p[8]}">${esc(p[0])} ` +
    `<span class="muted">${esc(p[5])}</span></div>`).join('')
    : `<div class="pinhit muted">No match</div>`;
  out.classList.add('on');
});
$('#pinaddResults').addEventListener('click', e => {
  const h = e.target.closest('[data-add]'); if (!h) return;
  togglePin(h.dataset.add);
  $('#pinadd').value = ''; $('#pinaddResults').innerHTML = ''; $('#pinaddResults').classList.remove('on');
});
document.addEventListener('click', e => {
  if (!e.target.closest('.pinaddwrap')) $('#pinaddResults').classList.remove('on');
});
renderPinSidebar();
// A pinned link should show its list on arrival without a click; a returning
// visitor's own open/closed choice is remembered instead.
setPinSidebarOpen(pinsFromUrl !== null ? PINNED.size > 0
  : localStorage.getItem('usau-pinned-open') === '1');
if (pinsFromUrl !== null && PINNED.size) {
  $('#rtype').value = 'player';
  drawRankings();
  showTab('rankings');
}

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
              n: Array.isArray(v) ? v[1] : null,
              // Clubs only: [elo, rosterSize, wins, losses]. A club that
              // played no scored game at an event has a size and no record.
              w: Array.isArray(v) ? v[2] : null,
              l: Array.isArray(v) ? v[3] : null, club: club});
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
      mid = RBC[ckey] && RBC[ckey].has.has(p.evIdx)
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
    const ev = gk && hasGames(gk, p.evIdx)
      ? `<span class="disc" data-games="${esc(gk + '|' + p.evIdx)}" ` +
        `data-kind="${isTeam ? 'c' : 'p'}" data-d="${d === null ? '' : d}">` +
        `${esc(p.event)}</span>`
      : esc(p.event);
    // A club's row says how the weekend actually went before it says what it
    // did to the rating: the record, a crown where they won the thing, and
    // how hard the field was. A player's row keeps the club column instead —
    // the record there would be the club's, not theirs.
    const meta = EMETA[p.evIdx] || [];
    const won = isTeam && meta[2] && meta[2] === ckey;
    const res = !isTeam ? ''
      : `<td class="n">` + (p.w === null || p.w === undefined
          ? `<span class="muted">\u2014</span>`
          : `${won ? '<span class="crown">\u25b2</span> ' : ''}${p.w}\u2013${p.l}`) +
        `</td>`;
    const fs = !isTeam ? '' : `<td class="n">${strChip(p.div, meta[0], meta[1])}</td>`;
    return `<tr><td class="d">${p.date}</td>` +
           `<td>${ev}<span class="muted" style="font-size:11.5px">` +
           ` ${DIVTAG[p.div] || DIVTAG[0]}</span></td>` + res + fs + mid +
           `<td class="r">${p.elo}</td><td class="dl">${dl}</td></tr>`;
  }).join('');
  return `<table class="hist"><thead><tr><th>Date</th><th>Event</th>` +
         (isTeam ? '<th class="n">Result</th><th class="n">Field</th>' +
                   '<th class="n">Roster</th>' : '<th>Team</th>') +
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
      ? (IS_GLICKO ? `${swing(tot)} event result update`
                   : `${swing(tot)} from results`) +
        (Math.abs(rest) >= 1 ? ` · ${swing(rest)} from a changed roster` : '')
      : `club ${swing(tot)}`);
  return `<p class="gsum">${head}</p>` +
         `<table class="gtbl"><tbody>${body}</tbody></table>`;
}

/* The games are the one thing an event row does NOT already have: the table
   knew the row was expandable from the core index, but the scores behind it
   live in that season's bucket. Open the row first, fill it when the bucket
   lands — the click is answered either way. */
function toggleGames(el) {
  const tr = el.closest('tr'), nx = tr.nextElementSibling;
  if (nx && nx.classList.contains('gms')) {
    nx.remove(); tr.classList.remove('open'); return;
  }
  // lastIndexOf, because a club key is free to contain the separator.
  const rk = el.dataset.games, c = rk.lastIndexOf('|');
  const ckey = rk.slice(0, c), evIdx = +rk.slice(c + 1);
  const d = el.dataset.d === '' ? null : +el.dataset.d;
  const kind = el.dataset.kind;
  const row = document.createElement('tr');
  row.className = 'gms';
  row.innerHTML = `<td colspan="5"><p class="gsum muted">Loading games\u2026</p></td>`;
  tr.after(row);
  tr.classList.add('open');
  faultTier('g', HEV[evIdx][2], ok => {
    if (!row.isConnected) return;
    row.innerHTML = `<td colspan="5">` + (ok
      ? gamesPane(ckey, evIdx, d, kind)
      : `<p class="gsum muted">The games behind this event could not be ` +
        `loaded.</p>`) + `</td>`;
  });
}

/* Roster people indices arrive absolute: mergeRosters decodes the bucket's
   deltas and rebases them as it appends the bucket's names to the pool. */
function rosterOf(rk) { return ROST[rk] || []; }
/* Names as links, or plain text for anyone below the trajectory floor: they
   were on the roster and are not dropped, there is just nothing to open. The
   floor is the ranked table's own — every rated trajectory belongs to a
   player with MIN_GAMES behind them, so PBY and the trajectory corpus hold
   exactly the same people, and PBY is the copy already in the page. */
function nameList(ids) {
  return ids.map(i => PBY.has(PPID[i])
    ? `<span class="nmlink" data-pid="${esc(PPID[i])}">${esc(PEOPLE[i])}</span>`
    : `<span class="muted">${esc(PEOPLE[i])}</span>`).join(', ');
}

/* Most recent season first, and most recent event first inside it. Which
   events a club has a roster for comes from the core index, so the tabs are
   drawable before the club's roster bucket has landed. */
function rosterSeasons(ckey) {
  const rb = RBC[ckey];
  if (!rb) return [];
  const by = new Map();
  for (const ei of rb.evs) {
    const ev = HEV[ei];
    if (!ev) continue;
    if (!by.has(ev[2])) by.set(ev[2], []);
    by.get(ev[2]).push(ei);
  }
  return [...by.entries()].sort((a, b) => b[0] - a[0]).map(e => ({
    season: e[0],
    eis: e[1].sort((a, b) => (HEV[b][0] || '').localeCompare(HEV[a][0] || ''))}));
}
function rosterDivision(grp) {
  const selected = $('#rdiv').value;
  const actual = new Set(grp.eis.map(ei => HEV[ei] && HEV[ei][3])
    .filter(d => d !== undefined));
  if (selected !== 'all' && (!actual.size || actual.has(+selected))) {
    return selected;
  }
  return actual.size === 1 ? String([...actual][0]) : 'all';
}

function loadRosterRatings(ckey, season, ids) {
  const keys = new Set();
  for (const i of ids) {
    const pid = String(PPID[i]);
    if (!PBY.has(pid) || Object.prototype.hasOwnProperty.call(PLAY, pid)) continue;
    const key = playerBucket(pid);
    const st = tierState('p', key);
    if (st !== 'ok' && st !== 'fail') keys.add(key);
  }
  if (!keys.size) return;
  let left = keys.size;
  const settled = () => {
    if (--left) return;
    const activeClub = cur && cur.kind === 'c' ? (TK[cur.key] || cur.key) : null;
    const sec = $('#dbody .rsec'), tab = sec && sec.querySelector('.rtab.on');
    if (activeClub !== ckey || !sec || !tab || +tab.dataset.season !== season) return;
    const pane = sec.querySelector('#rpane');
    pane.innerHTML = rosterPane(ckey, season);
    pane.dataset.season = season;
  };
  keys.forEach(key => faultTier('p', key, settled));
}

function rosterPlayerRating(pid, season, div) {
  pid = String(pid);
  if (!PBY.has(pid)) return {elo: null, loading: false};
  if (!Object.prototype.hasOwnProperty.call(PLAY, pid)) {
    const st = tierState('p', playerBucket(pid));
    return {elo: null, loading: st !== 'ok' && st !== 'fail'};
  }
  const point = playerYearMap(pid).get(
    div === 'all' ? String(season) : `${season}|${div}`);
  return {elo: point ? point.elo : null, loading: false};
}

/* ONE roster per season. For past seasons that is the union of every played
   event's listed squad. For the CURRENT season it is the best full-strength
   roster reported by the source — the same squad team_elo_best.csv rates the
   club off — which may be registered for an event not yet played; the Ev
   column then says how many of the season's played events each person was
   actually listed for, 0 meaning registered only. */
function rosterPane(ckey, season) {
  const grp = rosterSeasons(ckey).find(g => g.season === season) || {eis: []};
  const br = season === BSEASON ? BR[ckey] : null;
  const seen = new Map();
  grp.eis.forEach(ei => rosterOf(ckey + '|' + ei).forEach(
    i => seen.set(i, (seen.get(i) || 0) + 1)));
  const ids = br ? br[2] : [...seen.keys()];
  if (!ids.length) return '';
  const div = rosterDivision(grp);
  loadRosterRatings(ckey, season, ids);
  const rows = ids.map(i => {
    const pid = String(PPID[i]);
    const rating = rosterPlayerRating(pid, season, div);
    return {pid, name: PEOPLE[i], ev: seen.get(i) || 0,
            elo: rating.elo, loading: rating.loading, linked: PBY.has(pid)};
  });
  // Strongest for THIS season first. Missing trajectories go last
  // alphabetically rather than inheriting a current rating or becoming zero.
  rows.sort((a, b) => {
    if ((a.elo === null) !== (b.elo === null)) return a.elo === null ? 1 : -1;
    if (a.elo !== null && a.elo !== b.elo) return b.elo - a.elo;
    return a.name.localeCompare(b.name) || a.pid.localeCompare(b.pid);
  });
  const body = rows.map((r, i) =>
    `<tr><td class="rk">${i + 1}</td><td>` +
    (r.linked
      ? `<span class="nmlink" data-pid="${esc(r.pid)}">${esc(r.name)}</span>`
      : `<span class="muted">${esc(r.name)}</span>`) +
    `</td><td class="n" title="Elo after this player's last ${season} ` +
    `${div === 'all' ? 'event' : EDIVL[div] + ' event'}">` +
    `${r.loading ? '…' : r.elo === null ? '—' : r.elo.toFixed(0)}</td>` +
    `<td class="n">${r.ev}</td></tr>`).join('');
  const rosterSource = ckey.startsWith('euf:') ? 'EUCS Ranking' : 'USAU';
  const sum = br
    ? `${rows.length} players — best full-strength roster reported by ` +
      `${rosterSource}, registered for ${esc(br[0])} (${br[1]}). This is the ` +
      `squad the "best" club table rates off; Ev counts the ${grp.eis.length} ` +
      `played event${grp.eis.length === 1 ? '' : 's'} this season, 0 meaning ` +
      `registered but not yet played with.`
    : `${rows.length} players · ${grp.eis.length} event` +
      `${grp.eis.length === 1 ? '' : 's'}: ` +
      grp.eis.map(ei => esc(HEV[ei][1])).join(', ');
  const scope = div === 'all' ? season : `${season} ${EDIVL[div]}`;
  return `<p class="rsum">${sum} Elo is after each player's last ` +
    `${esc(scope)} event.</p>` +
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
  const requested = $('#ryear').value;
  const matched = requested !== 'all' && groups.some(g => g.season === +requested);
  const selected = matched ? +requested : groups[0].season;
  return `<details class="rsec" data-ck="${esc(ckey)}"${matched ? ' open' : ''}>` +
    `<summary>Rosters by season</summary><div class="rtabs">` + groups.map(g =>
      `<button class="rtab${g.season === selected ? ' on' : ''}" ` +
      `data-season="${g.season}">${g.season}</button>`).join('') +
    `</div><div id="rpane" data-season="${selected}">` +
    (matched ? rosterPane(ckey, selected) : '') + `</div></details>`;
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
   and history-table links have no name/elo/rank to read off.

   Everything below the header comes out of a sidecar — the core, then the one
   bucket holding this subject — so if either is still in flight the panel
   opens on a waiting state and re-renders itself the moment it lands. Opening
   regardless, rather than swallowing the click, is what keeps a deep link
   honest while the corpus is on its way. */
let pendingDetail = null;

/* The bucket a panel cannot be drawn without: a player's trajectory, or a
   club's rosters. A club with no roster on record anywhere — 14 of them —
   has no entry in the core index and so needs nothing faulted. */
function detailDep(kind, key) {
  if (kind === 'p') return ['p', playerBucket(key)];
  const rb = RBC[TK[key] || key];
  return rb ? ['r', rb.b] : null;
}

function openDetail(kind, key, opts) {
  opts = opts || {};
  const dep = HREADY ? detailDep(kind, key) : null;
  const st = dep ? tierState(dep[0], dep[1]) : 'ok';
  if (!HREADY || st !== 'ok') {
    // A player row knows its own name from the ranked table. A club key is
    // only spellable once the core has landed with the naming tables; before
    // that the key itself is all there is.
    const title = kind === 'p'
      ? ((PBY.get(String(key)) || [key])[0])
      : (HREADY ? clubLabel(TK[key] || key) : key);
    const dead = HFAILED || st === 'fail';
    pendingDetail = (HREADY || HFAILED) ? null : {kind, key, opts};
    $('#dbody').innerHTML = `<h2>${esc(title)}</h2>` +
      `<p class="note">` + (dead
        ? `The history behind this panel could not be loaded. Rankings, ` +
          `Tournaments do not need it and are unaffected.`
        : `Loading rating histories\u2026 this panel will fill in by itself.`) +
      `</p>`;
    cur = {kind, key};
    $('#detail').classList.add('on'); $('#scrim').classList.add('on');
    const h0 = '#' + kind + '/' + encodeURIComponent(key);
    if (location.hash !== h0) location.hash = h0;
    // Re-open only if the panel is still the one that asked. Clicking through
    // three names while a bucket is in flight redraws the last, not all three.
    if (!dead && dep) faultTier(dep[0], dep[1], () => {
      if (cur && cur.kind === kind && cur.key === key) openDetail(kind, key, opts);
    });
    return;
  }
  let ckey = key, title, parts = [];
  if (kind === 'p') {
    const row = PBY.get(String(key));
    if (row) {
      title = row[0];
      parts.push(`Elo <b>${row[1].toFixed(0)}</b>`, `${row[4]} games`,
                 `#${row[7]} of ${D.totalRated.toLocaleString()} rated`);
    } else {
      // Every rated trajectory belongs to a player in the ranked table, so
      // this is a hand-typed pid rather than anything the page ever linked.
      title = 'Player ' + key;
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
  const pts = decode(kind === 'p' ? PLAY[key] : H.teams[ckey]);
  const peak = pts.length ? Math.max(...pts.map(p => p.elo)) : null;
  const peakAt = pts.find(p => p.elo === peak);
  if (peak !== null) parts.push(
    `peak <b>${peak}</b> after ${esc(peakAt.event)} (${peakAt.date})`);
  const pinHead = kind === 'p'
    ? `<button class="act dpin${PINNED.has(String(key)) ? ' on' : ''}" data-pinid="${esc(String(key))}">` +
      `${PINNED.has(String(key)) ? '\u{1F4CC} Pinned' : '\u{1F4CC} Pin'}</button>`
    : '';
  $('#dbody').innerHTML =
    `<div class="dhead"><h2>${esc(title)}</h2>${pinHead}</div>` +
    `<div class="meta">${parts.join(' · ')}</div>` + chart(pts) +
    `<p class="note" style="margin:0 0 14px">Each point is the rating after that ` +
    `event — a weekend tournament is one step, not one point per game. ` +
    `${pts.length} event${pts.length === 1 ? '' : 's'} on record; click one in ` +
    `the table for the games behind it` +
    (kind === 'p'
      ? (IS_GLICKO
        ? ', which are the games of the club they turned out for. Glicko-2 updates ' +
          `after ${PERIOD_LABEL}; each game line gets an equal share of its period’s ` +
          'club move, while the Δ on the event row is the player’s own'
        : ', which are the games of the club they turned out for. The per-game move ' +
          'shown there is the club’s; provisional players can move by a different ' +
          'amount, so the Δ on the event row is their own')
      : '') + `.</p>` +
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

$('#detail').addEventListener('click', e => {
  const pin = e.target.closest('[data-pinid]');
  if (!pin) return;
  togglePin(pin.dataset.pinid);
  const on = PINNED.has(pin.dataset.pinid);
  pin.classList.toggle('on', on);
  pin.textContent = on ? '\u{1F4CC} Pinned' : '\u{1F4CC} Pin';
});
$('#detail').addEventListener('mousemove', e => {
  const h = e.target.closest('[data-tip]'); const tip = $('#tip');
  if (!h) { tip.classList.remove('on'); return; }
  tip.textContent = h.dataset.tip;
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top = (e.clientY - 26) + 'px';
  tip.classList.add('on');
});

$('#ptb').addEventListener('click', e => {
  const pin = e.target.closest('[data-pinid]'); if (pin) { togglePin(pin.dataset.pinid); return; }
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
    const pane = sec.querySelector('#rpane');
    pane.dataset.season = tb.dataset.season;
    pane.innerHTML = rosterPane(sec.dataset.ck, +tb.dataset.season);
    return;
  }
  const summary = e.target.closest('.rsec > summary');
  if (summary) {
    const sec = summary.closest('.rsec');
    setTimeout(() => {
      const pane = sec.querySelector('#rpane');
      if (sec.open && !pane.innerHTML) {
        pane.innerHTML = rosterPane(sec.dataset.ck, +pane.dataset.season);
      }
    }, 0);
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
const TV = D.tourneys || {teams: [], series: [], events: [], rounds: [],
                          tiers: []};
const EVS = TV.events, ESER = TV.series, ETM = TV.teams;
/* Event index -> shape, faulted one SEASON at a time through faultTier.
   Nothing in the list needs it: the champion column reads a global team index
   off the event row precisely so that 400 rendered rows pull no games. */
const EDET = {};
const EVBYID = new Map(EVS.map((e, i) => [e[0], i]));
const EDIVL = ["Club Men's", "College Men's", "College Men's D-III",
               'Club Mixed', "Club Women's",
               "College Women's", "College Women's D-III",
               "Masters Men's", "Masters Women's", "Masters Mixed",
               "Grand Masters Men's", "Grand Masters Women's", "Grand Masters Mixed",
               "Great Grand Masters Men's", "Great Grand Masters Women's",
               "Great Grand Masters Mixed", "Europe Open", "Europe Mixed",
               "Europe Women's",
               'College Mixed', 'High School Boys', 'High School Girls',
               'High School Mixed', 'Middle School Boys', 'Middle School Girls',
               'Middle School Mixed', 'YCC U-20 Boys', 'YCC U-20 Girls',
               'YCC U-20 Mixed', 'YCC U-17 Boys', 'YCC U-17 Girls',
               'YCC U-17 Mixed', 'YCC U-15 Boys', 'YCC U-15 Girls',
               'YCC U-15 Mixed', "Beach Men's", "Beach Women's", 'Beach Mixed',
               "Beach Masters Men's", "Beach Masters Women's", 'Beach Masters Mixed',
               "Beach Grand Masters Men's", "Beach Grand Masters Women's",
               'Beach Grand Masters Mixed',
               "Beach Great Grand Masters Men's", "Beach Great Grand Masters Women's",
               'Beach Great Grand Masters Mixed', 'Beach Legends Mixed',
               "League Men's", 'League Mixed'];

/* Event geography is only city/state in the upstream record. Translate the
   venue state through USA Ultimate's division-specific region boundaries;
   college and club do not use the same map. This is a venue region, not a
   claim that every team in an invitational belongs to that region. */
function regionIndex(groups) {
  const out = {};
  groups.forEach(([region, states]) =>
    states.split(' ').forEach(state => { out[state] = region; }));
  return out;
}
const COLLEGE_DIVS = new Set([1, 2, 5, 6]);
const COLLEGE_REGIONS = regionIndex([
  ['Atlantic Coast', 'DC DE MD NC SC VA'],
  ['Great Lakes', 'IL IN KY MI'],
  ['Metro East', 'CT NJ NY'],
  ['New England', 'MA ME NH RI VT'],
  ['North Central', 'IA MN ND NE SD WI'],
  ['Northwest', 'AK ID MT OR UT WA'],
  ['Ohio Valley', 'OH PA WV'],
  ['South Central', 'AR CO KS MO OK TX WY'],
  ['Southeast', 'AL FL GA LA MS TN'],
  ['Southwest', 'AZ CA HI NM NV'],
]);
const CLUB_REGIONS = regionIndex([
  ['Great Lakes', 'IL IN KY MI OH'],
  ['Mid-Atlantic', 'DC DE MD NJ PA VA WV'],
  ['North Central', 'IA KS MN MO ND NE SD WI'],
  ['Northeast', 'CT MA ME NH NY RI VT'],
  ['Northwest', 'AK ID MT OR UT WA'],
  ['South Central', 'AR CO NM OK TX WY'],
  ['Southeast', 'AL FL GA LA MS NC SC TN'],
  ['Southwest', 'AZ CA HI NV'],
]);
function eventGeo(e) {
  const match = /(?:^|,\s*)([A-Z]{2})$/.exec(e[6] || '');
  const state = match ? match[1] : '';
  const regions = COLLEGE_DIVS.has(e[3]) ? COLLEGE_REGIONS : CLUB_REGIONS;
  return [regions[state] || 'Unknown region', state];
}
const EVENT_GEO = new Map(EVS.map(e => [e, eventGeo(e)]));
/* Rankings and Trends include the European divisions. Tournaments are backed
   by the USAU event corpus and therefore use the narrower eventDivs set. */
const DIVS_PRESENT = new Set(D.divs || EDIVL.map((_, i) => i));
const EVENT_DIVS_PRESENT = new Set(D.eventDivs || D.divs || []);
function divisionOptions(present) {
  return '<option value="all">All divisions</option>' +
    EDIVL.map((label, i) => [label, i])
      .filter(([, i]) => present.has(i))
      .map(([label, i]) => `<option value="${i}">${esc(label)}</option>`).join('');
}
['#rdiv', '#tdiv'].forEach(sel => {
  $(sel).innerHTML = divisionOptions(DIVS_PRESENT);
});
$('#ediv').innerHTML = divisionOptions(EVENT_DIVS_PRESENT);
const EYEARS = [...new Set(EVS.map(e => e[2]))].sort((a, b) => b - a);
// Field strength, appended to each event row by site.py: [11] score, [12]
// tier letter. Scored in analysis/field_strength.py against the division's
// OWN season, so an S in club women's means what an S means in college.
const STRNOTE = TV.strengthNotes || {};
// division -> [[score, letter], ...], high to low. Not the same ladder in
// every division: see analysis/field_strength.py.
const STRCUT = TV.strengthCuts || {};
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

/* A field-strength chip: the letter, with the score and the bar it cleared
   behind it on hover. Something the model never rated carries neither, and
   says so with a dash rather than a D — "no ranked clubs came" and "we have
   no idea" are not the same claim. Shared by the tournament list and the
   club drill-down, which is why it takes the parts rather than a row. */
function strChip(div, score, letter) {
  if (!letter) return `<span class="muted">\u2014</span>`;
  const cut = (STRCUT[div] || []).find(c => c[1] === letter);
  return `<span class="fs fs${letter}" title="field ${score} average Elo \u2014 ` +
    `${esc(STRNOTE[letter] || '')}` +
    (cut && cut[0] !== null ? ` (${EDIVL[div]} ${letter} starts at ${cut[0]})`
                            : '') +
    `">${letter}</span>`;
}
const strCell = e => strChip(e[3], e[11], e[12]);

/* The division's own ladder, with the event's rung marked. A letter whose
   threshold is invisible is a riddle, and these thresholds MOVE: an S is the
   weakest championship that division has held, a different average Elo in
   college than in club men's. D has no threshold — it is "below C". */
function strBar(div, letter) {
  const cuts = STRCUT[div];
  if (!cuts) return '';
  return ` <span class="fsbar">` + cuts.map(([at, t]) =>
    `<span class="fs fs${t}${t === letter ? ' now' : ''}" ` +
    `title="${esc(EDIVL[div])} ${t}${at === null ? ': below C'
                                    : ': ' + at + ' and up'}">${t}</span>` +
    (at === null ? '' : `<i>${at}</i>`)).join('') + `</span>`;
}

function drawEvents() {
  const q = $('#eq').value.trim().toLowerCase(), div = $('#ediv').value;
  const yr = $('#eyear').value, tier = $('#etier').value;
  const str = $('#estr').value, sort = $('#esort').value;
  const state = $('#estate').value;
  let rows = EVS;
  if (div !== 'all') rows = rows.filter(e => e[3] === +div);
  if (yr !== 'all') rows = rows.filter(e => e[2] === +yr);
  if (tier === 'series') rows = rows.filter(e => e[8] > 0);
  else if (tier !== 'all') rows = rows.filter(e => e[8] === +tier);
  if (str !== 'all') rows = rows.filter(e => e[12] === str);
  if (state === 'done') rows = rows.filter(e => !e[13]);
  else if (state === 'up') rows = rows.filter(e => e[13]);
  if (q) rows = rows.filter(e => e[1].toLowerCase().includes(q) ||
                                 e[6].toLowerCase().includes(q) ||
                                 ESER[e[9]][0].toLowerCase().includes(q));
  const total = rows.length;
  // The cap is a DOM budget, not a filter: the count says how many matched so
  // a narrower search is an obvious next move. An unrated event sorts last on
  // strength rather than as a zero — it is unmeasured, not weak.
  if (sort === 'str') {
    rows = rows.slice().sort((a, b) =>
      (b[11] ?? -1) - (a[11] ?? -1) ||
      (b[4] || '').localeCompare(a[4] || ''));
  } else if (sort === 'region') {
    rows = rows.slice().sort((a, b) => {
      const ag = EVENT_GEO.get(a), bg = EVENT_GEO.get(b);
      return ag[0].localeCompare(bg[0]) || ag[1].localeCompare(bg[1]) ||
        (a[6] || '').localeCompare(b[6] || '') ||
        (b[4] || '').localeCompare(a[4] || '');
    });
  } else {
    rows = rows.slice().sort((a, b) =>
      (b[4] || '').localeCompare(a[4] || ''));
  }
  const shown = rows.slice(0, 400);
  $('#etb').innerHTML = shown.map(e => {
    const n = ESER[e[9]][1].length;
    const ch = e[10] >= 0 ? `<span class="crown">${esc(ETM[e[10]])}</span>`
                          : '<span class="muted">\u2014</span>';
    // Division, championship tier and field strength are three separate facts,
    // so they get three separate marks: colouring the division tag by tier read
    // as if "Club Men's" itself meant something. Only Regionals and up carry a
    // series chip — Sectionals and Conference are most of the corpus, and the
    // filter already finds them.
    const chip = e[8] >= 3
      ? `<span class="tag t${e[8]}">${esc(TV.tiers[e[8]])}</span> ` : '';
    const region = EVENT_GEO.get(e)[0];
    return `<tr class="ev" data-ev="${e[0]}"><td class="dt">${daterange(e[4], e[5])}</td>` +
      `<td>${chip}${esc(e[1])}` +
      `${e[6] ? ` <span class="muted">\u00b7 ${esc(e[6])}` +
        `${region === 'Unknown region' ? '' : ` \u00b7 ${esc(region)}`}</span>` : ''}</td>` +
      `<td><span class="tag">${esc(EDIVL[e[3]] || '')}</span></td>` +
      `<td class="n">${e[7]}</td><td class="n">${strCell(e)}</td><td>${ch}</td>` +
      `<td class="n">${n > 1 ? n : '<span class="muted">1</span>'}</td></tr>`;
  }).join('');
  $('#ecount').textContent = `${total.toLocaleString()} tournament` +
    (total === 1 ? '' : 's') + (total > shown.length
      ? ` \u00b7 showing ${shown.length}` : '');
}
['#eq', '#ediv', '#eyear', '#etier', '#estr', '#esort', '#estate'].forEach(s => {
  const el = $(s);
  el.oninput = drawEvents; el.onchange = drawEvents;
});
$('#etb').addEventListener('click', e => {
  const tr = e.target.closest('[data-ev]');
  if (tr) location.hash = '#t/' + tr.dataset.ev;
});

/* ---------- standings ---------- */
/* Wins first, then the head-to-head record INSIDE the tied group, then point
   differential. Every game here has a score, so the real tiebreakers are
   available and rating never enters. */
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

/* The header is drawable from the event row alone — date, place, division,
   tier, size and champion all live there — so it paints at once and only the
   games wait on the season bucket. `curEvent` is claimed up front rather than
   at the end: a bucket landing checks it before redrawing, so clicking through
   three events while one season is in flight redraws the last, not all three. */
function drawTournament(i) {
  const e = EVS[i], det = EDET[i];
  curEvent = i;
  const ser = ESER[e[9]], sibs = ser[1];
  const facts = [daterange(e[4], e[5]), e[6], EDIVL[e[3]], TV.tiers[e[8]],
                 `${e[7]} teams`].filter(Boolean).map(esc);
  // The grade gets the same chip it has in the list, and says out loud that
  // the bar it cleared is this DIVISION's, since that bar moves.
  const grade = e[12]
    ? `<span class="fs fs${e[12]}">${e[12]}</span> ${e[11]} ` +
      `<span class="muted">average Elo \u00b7 ${EDIVL[e[3]]} field</span>`
    : `<span class="muted">field ungraded</span>`;
  facts.push(e[10] >= 0 ? `champion <b>${esc(ETM[e[10]])}</b>`
                        : 'no champion on record');
  $('#tvhead').innerHTML = `<h2>${esc(e[1])}</h2>` +
    `<div class="meta">${facts.join(' \u00b7 ')}</div>` +
    `<div class="meta fsline">${grade}${strBar(e[3], e[12])}</div>`;
  if (!det) {
    // Settled-but-absent means the bucket 404'd or is missing this event, and
    // re-requesting would call back synchronously — straight into a loop.
    const st = tierState('t', e[2]), settled = st === 'ok' || st === 'fail';
    $('#tvbody').innerHTML = settled
      ? `<p class="muted">This tournament's games come from <code>` +
        `${esc((D.tourneyJs || {})[e[2]] || 't/' + e[2] + '.js')}</code>, which ` +
        `could not be loaded. The tournament list does not need it and is ` +
        `unaffected.</p>`
      : `<p class="muted">Loading ${e[2]} games\u2026</p>`;
    $('#tvnote').innerHTML = '';
    if (!settled)
      faultTier('t', e[2], () => { if (curEvent === i) drawTournament(i); });
    return;
  }
  const nm = l => ETM[det.t[l]];
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
  // EVERY bracket is drawn, including the one-game ones. They used to collapse
  // into the table below on the grounds that a four-row grid headed "Final" is
  // a waste of a section for "3rd place: DiG beat Mooncatchers" — but that hid
  // most of what an event actually decided. The 2026 Lehigh Valley Invite
  // publishes TEN men's brackets and six of them are single games, so the old
  // rule drew four and buried the rest in a flat list that never said they
  // were brackets at all. A one-game bracket renders as a 1x1 grid under its
  // own heading, which is short and says what it is.
  // One kind can legitimately appear twice: an event running two flights off
  // one schedule yields two trees deciding the same placement, and collapsing
  // them would throw a flight away. So repeats are NUMBERED rather than
  // merged — the 2026 Lehigh men's draw shows "Championship bracket" and
  // "Championship bracket · flight 2" instead of the same heading twice.
  const seen = {};
  html += det.b.map(b => {
    const n = (seen[b[0]] = (seen[b[0]] || 0) + 1);
    const total = det.b.filter(x => x[0] === b[0]).length;
    return evBracket(b[0], b[1], b[2], nm, det, total > 1 ? n : 0);
  }).join('');

  // What is left is only what no bracket claimed: crossovers, play-ins, and
  // anything the label placed nowhere.
  const extra = det.o;
  if (extra.length) {
    html += `<h3 class="sect">Placement &amp; other games <span class="muted">` +
      `\u2014 crossovers, play-ins, and anything the organiser's own label ` +
      `does not put in a bracket</span></h3>` +
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
  //
  // A series that spans divisions gets a picker, defaulting to the one being
  // viewed. Pro Elite Challenge West runs men's, mixed and women's on one
  // weekend, so an undivided list interleaved three tournaments and asked the
  // reader to filter twenty rows by eye to answer "who won this before?".
  let seriesRows = null;
  if (sibs.length > 1) {
    const divs = [...new Set(sibs.map(j => EVS[j][3]))].sort((a, b) => a - b);
    seriesRows = d => sibs.slice().reverse()
      .filter(j => d === 'all' || EVS[j][3] === d)
      .map(j => {
        const s = EVS[j];
        const ch = s[10] >= 0 ? `<span class="crown">${esc(ETM[s[10]])}</span>`
                              : '<span class="muted">\u2014</span>';
        return `<tr class="ev" data-ev="${s[0]}"${j === i ? ' style="font-weight:600"' : ''}>` +
          `<td class="n">${s[2]}</td><td><span class="tag">` +
          `${esc(EDIVL[s[3]] || '')}</span></td><td>${esc(s[1])}</td>` +
          `<td class="n">${s[7]}</td><td class="n">${strCell(s)}</td>` +
          `<td>${ch}</td></tr>`;
      }).join('');
    // One division in the series means nothing to pick between, and the column
    // already says which it is.
    const start = divs.length > 1 ? e[3] : 'all';
    const pick = divs.length > 1
      ? ` <select id="sdiv">` +
        divs.map(d => `<option value="${d}"${d === e[3] ? ' selected' : ''}>` +
                      `${esc(EDIVL[d] || '')}</option>`).join('') +
        `<option value="all">All divisions</option></select>`
      : '';
    html += `<h3 class="sect">${esc(ser[0])} <span class="muted">\u2014 ` +
      `<span id="scount">${sibs.length}</span> instances on record</span>` +
      `${pick}</h3>` +
      `<table class="evtbl"><thead><tr><th class="n">Year</th><th>Division</th>` +
      `<th>Event</th><th class="n">Teams</th><th class="n">Field</th>` +
      `<th>Champion</th></tr></thead><tbody id="sbody">${seriesRows(start)}</tbody></table>`;
  }
  $('#tvbody').innerHTML = html;
  // Re-rendered rather than re-drawn: drawTournament rebuilds the whole view
  // and would reset the picker to the viewed division on every change. Row
  // clicks survive because #tvbody delegates them.
  const sd = $('#sdiv');
  if (sd) sd.onchange = () => {
    const d = sd.value === 'all' ? 'all' : +sd.value;
    $('#sbody').innerHTML = seriesRows(d);
    $('#scount').textContent = $('#sbody').children.length;
  };
  if (sd) $('#scount').textContent = $('#sbody').children.length;
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
    `differential. The series history opens on the division you are looking at ` +
    `and the picker beside it switches to the others, since a tournament ` +
    `running three divisions on one weekend is three separate histories. Team ` +
    `names in <span class="nmlink">this style</span> open that club's rating ` +
    `history.`;
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
function evBracket(kind, root, rounds, nm, det, flight) {
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
  return `<h3 class="sect">${esc(brLabel(kind))}` +
    (flight ? ` <span class="muted">\u00b7 flight ${flight}</span>` : '') + `</h3>` +
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
// Until the trajectory file lands nothing can be validated, so a deep link is
// taken at face value; openDetail shows the waiting state and re-resolves it.
const known = (kind, key) => !HREADY ? true : kind === 'p'
  ? PBY.has(String(key))
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
  if (curEvent === null && (m[1] === 'p' || m[1] === 'c')) {
    $('#rtype').value = m[1] === 'p' ? 'player' : 'club';
    drawRankings();
    showTab('rankings');
  }
  openDetail(m[1], key);
}
window.addEventListener('hashchange', routeHash);

/* ---------- trends: season charts, everyone who reached a top 25 ---------- */
// Eight hues x eight dash patterns = 64 distinguishable strokes. The union of
// every season's top 25 runs to 164 series in the densest view, so past 64 a
// combination repeats and the legend, not the stroke, is the identifier.
const DASH = ['none', '5 3', '2 3', '8 3 2 3',
              '12 4', '1 3', '7 3 1 3', '3 3 9 3'];
// Codes match the DIVCODE written into each event by analysis.rankings.
const DIVLABEL = {all: 'all divisions', 0: "club men's", 1: "college men's",
                  2: "college men's D-III", 3: 'club mixed',
                  4: "club women's", 5: "college women's",
                  6: "college women's D-III",
                  7: "masters men's", 8: "masters women's", 9: 'masters mixed',
                  10: "grand masters men's", 11: "grand masters women's",
                  12: 'grand masters mixed',
                  13: "great grand masters men's", 14: "great grand masters women's",
                  15: 'great grand masters mixed',
                  16: 'Europe open', 17: 'Europe mixed', 18: "Europe women's",
                  19: 'college mixed', 20: 'high school boys', 21: 'high school girls',
                  22: 'high school mixed', 23: 'middle school boys',
                  24: 'middle school girls', 25: 'middle school mixed',
                  26: 'YCC U-20 boys', 27: 'YCC U-20 girls', 28: 'YCC U-20 mixed',
                  29: 'YCC U-17 boys', 30: 'YCC U-17 girls', 31: 'YCC U-17 mixed',
                  32: 'YCC U-15 boys', 33: 'YCC U-15 girls', 34: 'YCC U-15 mixed',
                  35: "beach men's", 36: "beach women's", 37: 'beach mixed',
                  38: "beach masters men's", 39: "beach masters women's",
                  40: 'beach masters mixed',
                  41: "beach grand masters men's", 42: "beach grand masters women's",
                  43: 'beach grand masters mixed',
                  44: "beach great grand masters men's",
                  45: "beach great grand masters women's",
                  46: 'beach great grand masters mixed', 47: 'beach legends mixed',
                  48: "league men's", 49: 'league mixed'};
// Codes match the payload's gender map: 1 male-matching, 2 female-matching.
const GENLABEL = {all: '', 1: ' male-matching', 2: ' female-matching'};
const trendCache = {};

// Every rated trajectory belongs to a player in the ranked table, so this
// never has to reach past it.
function playerLabel(pid) {
  const row = PBY.get(String(pid));
  return row ? row[0] : String(pid);
}

/* One value per season: the rating after that season's LAST event, per
   subject, plus the population median and the population count.

   Precomputed in analysis/history_split.py rather than derived here. The
   median and the top-25 cut are statistics over the WHOLE population, so no
   subset computes them — which meant this function, and this function alone,
   pinned all 39,325 trajectories in the page. There are only 24 answers it
   can ever give, so all 24 ship in the core at 40 KB gzipped against the
   1.9 MB corpus they replace. Clubs carry no gender-matching group, so that
   side has one answer per division and the control is normalized away. */
function seasonData(kind, div, gen) {
  const ck = kind + '|' + div + '|' + (kind === 'c' ? 'all' : gen);
  if (trendCache[ck]) return trendCache[ck];
  const raw = (H.trends || {})[ck];
  // Mapped to objects once and kept: drawTrends writes `best` onto each.
  trendCache[ck] = raw
    ? {top: raw.top.map(t => ({key: t[0], label: t[1], vals: t[2], peak: t[3]})),
       med: raw.med, n: raw.n}
    : {top: [], med: SEASONS.map(() => 0), n: 0};
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

let hotIdx = null, pinIdx = null, yearIdx = null, yearLocked = null;
// The season the legend falls back to is the most recent one, and it moves
// with the history file: DEFYEAR is set in applyHistory alongside SEASONS.
// The chart, its baseline and its mode, so the legend can be re-sorted against
// a season without re-deriving anything.
let curSeries = [], curMed = [], curMode = 'elo';
const seriesVal = (s, i) => s.vals[i] === null ? null
  : (curMode === 'med' ? s.vals[i] - curMed[i] : s.vals[i]);

function drawTrends() {
  // Trends is built entirely out of the trajectory file. Until that lands
  // there is nothing to plot, so say which state we are in and come back.
  if (!HREADY) {
    $('#tchart').innerHTML = `<p class="muted" style="font-size:13px">` + (HFAILED
      ? `Trends needs <code>${esc(D.historyJs || 'history.js')}</code>, which ` +
        `could not be loaded.`
      : `Loading season trajectories\u2026`) + `</p>`;
    $('#tlegend').innerHTML = ''; $('#tlhead').textContent = '';
    $('#tcount').textContent = '';
    return;
  }
  const kind = $('#tsub').value, mode = $('#tmode').value, div = $('#tdiv').value;
  const gen = $('#tgen').value;
  // Clubs carry no gender-matching group, so the control is disabled rather
  // than silently ignored when the subject is clubs.
  $('#tgen').disabled = kind !== 'p';
  const dat = seasonData(kind, div, gen);
  hotIdx = null; pinIdx = null; yearIdx = null; yearLocked = null;
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
    (yearLocked === i ? ' (locked)' : i === DEFYEAR ? ' (current)' : '');
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
// A locked season survives leaving the chart: clicking a column pins the
// legend's ranking to that season until it is clicked again (unlock) or a
// different column is clicked (relock). The hover guide (.on) still tracks
// the pointer for the live preview; .locked marks the sticky one so the two
// read as distinct even when they coincide.
function setYearLock(i) {
  const svg = $('#tsvg');
  const guide = j => svg && svg.querySelector(`.yg[data-yi="${j}"]`);
  if (yearLocked !== null) { const g = guide(yearLocked); if (g) g.classList.remove('locked'); }
  yearLocked = yearLocked === i ? null : i;
  if (yearLocked !== null) { const g = guide(yearLocked); if (g) g.classList.add('locked'); }
  setYear(yearLocked === null ? DEFYEAR : yearLocked);
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
  () => { setHot(pinIdx); setYear(yearLocked === null ? DEFYEAR : yearLocked); });
$('#tchart').addEventListener('click', e => {
  const h = e.target.closest('.xh');
  if (h) setYearLock(+h.dataset.yi);
});
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
  `that has ever closed a season inside the top 25 gets a line — __TRENDN__ of them ` +
  `depending on the view, which is why the field thins out and hovering is how you ` +
  `read an individual. Click a season column to lock the legend's ranking to ` +
  `it — the guide line turns solid and stays until you click it again or ` +
  `another column. The legend is ranked on the most recent season, not on an ` +
  `all-time peak, so it opens as the current table; hover any other season in the ` +
  `chart and it re-ranks on that one, showing that season's rating, and leaving ` +
  `returns it to the locked season, or the current one if none is locked. ` +
  `A subject that did not play the ranked season shows an em dash and sinks ` +
  `to the bottom rather than sorting as zero. ` +
  `Hover a line or a legend row to isolate it, click the row to pin, click the ` +
  `name to open its full history. A season a subject did not play breaks the line ` +
  `rather than being interpolated across. "Above season median" subtracts the ` +
  `median of every rated subject active that season. Narrowing the division keeps ` +
  `only events in it, so a season reads as the rating after that season's last ` +
  `event in that division — __MULTIDIV__ club identities play in more than one, and each ` +
  `is ranked on its own record in whichever division you are looking at. Gender ` +
  `works the other way and selects whole people, since nobody changes group ` +
  `between events; it is inert for clubs. Both narrow the population, so the top ` +
  `25 is recomputed inside whatever you have selected. All ${NDIV} divisions share ` +
  `one rating scale, bridged by the ${GENDER_NOTE}`;

$('#enote').innerHTML =
  `Every event in the corpus with a completed game, plus scheduled future ` +
  `events whose games have not started: ${EVS.length.toLocaleString()} across ` +
  `${ESER.length.toLocaleString()} tournament series. Click a row for that ` +
  `event's pools and bracket, and for the other years the same tournament has ` +
  `run. <b>Editions</b> counts the instances on record — the same tournament ` +
  `in another division counts, since a Sectional's men's and women's halves are ` +
  `one weekend run twice. The champion is the winner of the championship ` +
  `bracket's final where the schedule names one; events that finished on pool ` +
  `play, or whose stage labels name no final, show a dash. Regions are inferred ` +
  `from the event venue's state using the division's USA Ultimate region map.`;

$('#enote').innerHTML +=
  ` <b>Field</b> is the average Elo in the room: how hard the tournament was ` +
  `to be at, from who turned up rather than from what the event is called. A ` +
  `Regional can outrank a Nationals feeder, and Florida Warm Up outranks the ` +
  `championship it feeds. Each club is counted at <b>the rating it walked in ` +
  `with</b> — its last result before the event started — so nothing done at ` +
  `the event, or after it, moves the number, and a club with no rating yet is ` +
  `left out rather than guessed at. The average is taken against half a ` +
  `Nationals field of merely typical clubs, which barely touches a 16-team ` +
  `draw and drags a two-team showcase most of the way back to ordinary: two ` +
  `results are not evidence of a tournament.<br>` +
  `<b>The bars are per division</b>, pinned at both ends to that division's ` +
  `own record. <span class="fs fsS">S</span> is the <i>weakest national ` +
  `championship it has ever held</i>, so every Nationals is S and stays one — ` +
  `a thinner championship later becomes the new floor rather than dropping ` +
  `out. <span class="fs fsC">C</span> is its <i>median event</i>, and A and B ` +
  `split the gap. So an S says "the average team here was as strong as the ` +
  `average team at this division's Nationals" and a D says "a below-average ` +
  `field for this division" — the same claims everywhere, at different ` +
  `numbers. ` +
  Object.keys(STRCUT).sort().map(d =>
    `<span style="white-space:nowrap">${esc(EDIVL[d])} ` +
    STRCUT[d].slice(0, 4).map(([at, t]) =>
      `<span class="fs fs${t}">${t}</span>\u2009${at}`).join(' ') +
    `</span>`).join(' \u00b7 ') +
  `. The cut is a label on a continuous number — a high C is a low B — so the ` +
  `rating rides on the chip. A dash is not a D: it means not one club in the ` +
  `field had a rating yet.`;

/* ---------- boot ---------- */
/* Everything above needs only the inline payload, so the page is fully usable
   the moment this runs. The trajectory corpus is then pulled in behind it. */
drawRankings(); drawEvents();
document.documentElement.classList.remove('booting');
routeHash();

/* Anything already on screen that was drawn without the trajectory file gets
   redrawn now: a tournament view gains its club links, an open panel fills in,
   and Trends becomes drawable. */
function onHistoryReady() {
  if (curEvent !== null) drawTournament(curEvent);
  const p = pendingDetail;
  pendingDetail = null;
  if (p && cur && cur.kind === p.kind && cur.key === p.key) {
    openDetail(p.kind, p.key, Object.assign({}, p.opts, {push: false}));
  } else if (cur) {
    openDetail(cur.kind, cur.key, {push: false});
  }
  if ($('#trends').classList.contains('on')) drawTrends();
  if ($('#rankings').classList.contains('on')) drawRankings();
}

/* A <script> tag rather than fetch(): a classic script loads from a file://
   page, XHR and fetch do not, and this page is meant to work from disk. */
(function loadHistory() {
  if (D.history) { applyHistory(D.history); onHistoryReady(); return; }
  if (!D.historyJs) { HFAILED = true; return; }
  const s = document.createElement('script');
  s.src = D.historyJs;
  s.async = true;
  s.onload = () => {
    const h = window.__USAU_HISTORY__;
    if (!h) { HFAILED = true; onHistoryReady(); return; }
    applyHistory(h);
    delete window.__USAU_HISTORY__;   // the live bindings own it now
    onHistoryReady();
  };
  s.onerror = () => { HFAILED = true; onHistoryReady(); };
  document.head.appendChild(s);
})();
</script>
</html>
"""


if __name__ == "__main__":
    build()
