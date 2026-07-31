# USAU Player-Level Elo (Club Men's)

### → **[Live rankings and U.S. Open tracker](https://sethlnx.github.io/usau-player-elo/)**

Every player carries a personal Elo across seasons; a team's rating at an
event is the softmax-weighted mean of its event roster's player Elos (better
players weigh more, assuming more playing time). Game deltas are applied to
every rostered player equally. New seasons need no reset — a team's opening
rating falls out of whoever is on its roster.

Full plan: `USAU_by_player_elo.md`.

## Pipeline

```
.venv/bin/python -m scraper.build_db --division club       2017 2018 2019 2021 2022 2023 2024 2025 2026
.venv/bin/python -m scraper.build_db --division college    2017 2018 2019 2020 2021 2022 2023 2024 2025 2026
.venv/bin/python -m scraper.build_db --division college-d3 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026
.venv/bin/python -m identity.resolve      # names -> player IDs (+ data/ambiguities.csv)
.venv/bin/python -m analysis.backtest     # walk-forward eval; reports TEST 2024-25
.venv/bin/python -m analysis.rankings     # data/player_elo.csv, data/team_elo*.csv, data/history.json
.venv/bin/python -m analysis.site         # docs/index.html — standalone, no server
```

The scrape is slow, disk-cached and resumable; `data/usau.db` and the raw HTML
cache are gitignored and rebuilt by the first three commands. There is no club
2020 — COVID cancelled the series — but college 2020 exists and is kept: the
series was cancelled in March, the January-March regular season was not.

D-I and D-III share the College-Men competition level and the same schedule
URL, so `--division college` takes every event whose name is not D-III and
`--division college-d3` takes exactly those that are. The two sets are
disjoint; `events.url` is UNIQUE and `events.division` is one column, so
overlapping sets would have each division overwriting the other's rows.

## Front end

**https://sethlnx.github.io/usau-player-elo/** — a single self-contained
`docs/index.html`, no server and no network needed.

`analysis/site.py` emits it: club rankings on three roster bases, a searchable
player table, a **Trends** tab drawing one line per season for every player or
club that has ever closed a season in the top 25, filterable to a single
division, and a U.S. Open tracker whose bracket you fill in as games finish,
re-running a 40,000-sim Monte Carlo in the browser against whatever you have
entered. It reads only the published artifacts and never replays the model, so
the page cannot drift from `data/player_elo.csv` and `data/team_elo*.csv`.

That selection is a union across seasons, so it is 67–164 lines depending on the
view rather than a fixed 25 — the point being that a club that owned 2019 and has
since folded sits on the chart beside this year's best. At that density no
stroke can identify a line on its own (8 hues x 8 dash patterns gives 64
combinations), so the interaction carries it: hovering a line or legend row
isolates it and drops the rest, and **hovering anywhere else in the chart re-ranks
the legend on the season under the cursor**, showing that season's rating. A
subject that did not play that season sinks to the bottom rather than sorting as
zero, which would rank a club that skipped the year above one that played badly.

The division filter selects **events, not teams**. 301 club identities play in
more than one division, so classifying a whole identity would misfile every one
of them — Colorado College (Wasabi) has both club and college events in 2024, and
its 2024 value is 1449 in the club view against 1512 in the college view. Within
a division a season reads as the rating after that season's last event *in that
division*, the population is only the subjects with an event there (968 club, 980
college, 316 D-III of 1,953), and the "above season median" baseline is
recomputed over that population.

Clicking any name opens a drill-down: the rating curve, the full event history,
and — for a player — **which club he turned out for** at each event; for a club,
**rosters by season** behind a disclosure, tabbed newest-first. One table per
season, sorted strongest first, carrying each player's overall rank and Elo. A
club re-registers each tournament, so the season roster is the union of every
event's listed squad — Dark Star's 2026 is 30 players across three events of 22,
26 and 25 — and the `Ev` column says how many of them each player was listed for,
which is the only thing unioning loses. Every name in the panel is itself a link,
with a `‹ Back` stack, and the open subject is reflected in the URL hash
(`#p/<player_id>`, `#c/<club key>`) so it is shareable and the browser Back button
works. A player below the 30-game floor has no rank or Elo to show and is listed
as plain text: he was on the roster, but there is nothing to open.

Rosters and affiliations ride inside `data/history.json`, which keeps the page a
single file that works from `file://` — `fetch()` does not. That costs weight:
10.5 MB raw, 3.7 MB gzipped, which is what Pages actually serves. Roster members
are keyed on `(name, player_id)` pairs, never on name alone, for the reason in
the data notes below: display names are not unique.

## Layout

- `scraper/` — cached, rate-limited fetch (`fetch.py`), event enumeration via
  the WebForms search postback (`events.py`), schedule/bracket parsing
  (`event_detail.py`), roster parsing (`rosters.py`), orchestration
  (`build_db.py`).
- `ufa/` — UFA (watchufa.com) integration: cached JSON API client (`api.py`),
  season scraper into `ufa_*` tables (`scrape.py` — teams, players, per-season
  stats incl. true points played, games), and name→identity linker with
  city/jersey/year corroboration (`link.py` → `data/ufa_links.csv` +
  `data/ufa_link_audit.csv` review queue).
- `identity/` — name→player resolution. A name on 2+ clubs in one
  (division, season) collides, but only SPLITS per club if the shards also
  contradict physically: different teams in overlapping date windows, which
  one body cannot do. Of 2,578 collisions only 364 conflict; the other 2,214
  auto-merge, since the raw rule keyed on (name, club) and so shattered a
  career over one messy season — each shard then re-debuting at its division
  base and re-burning a 6x provisional window, which inflated whichever shard
  sat on the strongest team. Every collision, split or merged, lands in
  `data/ambiguities.csv` with its verdict; college↔club bridges log to
  `data/cross_division_links.csv`; review verdicts in
  `data/link_overrides.csv` (`block` = two people, `merge` = one person,
  `confirm` = bridge reviewed OK). A conflict proves two people, its absence
  only fails to disprove them, so same-named players in different regions who
  never coincide do merge wrongly — `block` is the correction.
- `elo/` — the rating engine (`EloConfig` holds every knob). Debut players
  enter at their division's base rating (not teammate mean; the old
  context-init survives behind `EloConfig.context_init`), converging via an
  aggressive provisional multiplier. Offseason regression targets the
  division base of each player's last division.
  Per-player stats (G/A/D/T, reported at Nationals-level events) feed two
  mechanisms, ingested only after an event ends: `involvement_credit` splits
  each game delta by a shrunk usage index (roster-mean preserved), and
  `stat_transfer_beta` transfers rating zero-sum between teammates by net
  stat quality. Published rankings config lives in `analysis/rankings.py`
  (`PUBLISHED`): plain roster mean + both stat mechanisms. Linked UFA
  seasons feed the same mechanisms (true points-played as usage; counting
  stats scaled to tournament magnitude), ingested each Sept 1. UFA game
  *results* were measured as a third division (`load_ufa_games`,
  `k_scale={"ufa": ...}`) and hurt club prediction at every weight, so the
  published rankings exclude them; the fully intermingled variant lives in
  `data/*_elo_intermingled.csv`.
- `analysis/` — backtest (headline eval on club; slices for early-club and
  college), tuning grid, CSV exports, and `bridge_audit.py` — ranks bridge
  links by false-merge suspicion (height contradictions, rating shift,
  geography); `--write-overrides` auto-blocks hard height conflicts.
  `usau_baseline.py` reimplements USA Ultimate's own iterative rankings
  algorithm (v2.0) from our game data and scores it walk-forward on the
  same holdout — the plan's "comparison model 4".
  `usau_player.py` is that algorithm's rating form made player-level,
  stat-usage-weighted, and carried across seasons via a rolling date weight
  instead of a yearly reset — a batch fixed point solved with numpy rather
  than a sequential updater. Needs a prior pull toward the base rating to
  converge at all (see its module docstring); +10.1 accuracy points over the
  team-level original, still behind player Elo.

## Data notes

- Source: play.usaultimate.org. Raw HTML is cached under `data/raw/cache/`
  (404s too, as `.404` sentinels); DB rows are UPSERTed and finished events
  get `events.complete=1`, so any run can be interrupted and resumed cheaply.
- The site WAF blocks per-IP after a request budget, largely independent of
  pace. `fetch.py` paces via USAU_RATE_LIMIT/USAU_JITTER, or via a file named
  by USAU_RATE_FILE that it re-reads before every request so a long backfill
  can be ramped while it runs. On a block it raises SiteBlocked — switch exit
  and re-run to resume from cache.
- IMPORTANT: a lone failing URL is NOT a block. Some event pages reset the
  connection on every request from every exit (`Denver-Round-Robin-2019` does)
  while the rest of the site serves fine. `fetch.py` proves the site is down
  against a control URL before claiming a block, and otherwise raises
  UrlUnavailable so build_db skips that event and carries on. Without that
  check one broken row halts an entire backfill and sends you hunting a block
  that was never there.
- Schedules cached on/before an event's last day are refetched once the
  event is over (mid-event fetches lack final scores); current-season event
  enumeration bypasses the postback cache so new events appear.
- 491 of 58,080 scored games (0.85%) carry a `games.date` outside their event's
  `[start_date, end_date]` window — year typos like `2020-03-05` on a 2022
  event whose window is `[2022-03-05, 2022-03-06]`, and one 2024 regional game
  stamped `2001-09-23`. `load_games` clamps to the nearest bound. Untreated
  they sorted to the head of the corpus, so the replay's season order read
  2024 -> 2022 -> 2023 -> 2024 -> 2022 -> 2021 before the clean run: 34 games
  replayed years before games older than them, and `new_season` fired 10 times
  for 6 seasons, decaying every rating toward base five extra times. `replay`
  now regresses on a season ADVANCE only, so disorder cannot re-fire it.
  Clamping never moves a game later, so no stat event reaches the model earlier
  than before. Held-out club accuracy moved 0.7662 -> 0.7667 (published
  config); the fix is for correctness, not for the score.
- `player_id` is NOT stable across `identity.resolve` runs: it drops and
  rebuilds the `players` table, and ids fall out of an unordered scan. Join
  `player_elo.csv` to a DB from the same run, and re-export after re-resolving.
- Event search renders two independent grids — upcoming (25 rows/page) and
  past (20 rows/page) — each with its own pager. Both are walked, keyed on the
  printed page number: a page link's `ctlNN` postback index shifts with the
  page you are standing on, so target strings are not stable identifiers.
  Walking only the past grid (as the enumerator did until 2026-07) silently
  truncates an in-progress season to its first 25 upcoming events.
- The team tables are club-only and come in three bases. `team_elo.csv` rates
  each club off its most recent COMPLETED event — the results-grounded default.
  `team_elo_upcoming.csv` uses the roster registered for the next event, which
  is not yet in the books; that means a future registration OR one being played
  right now, since an earlier `start_date > today` rule emptied the table on
  the morning of the tournament it existed for. `team_elo_best.csv` takes the
  club's strongest roster of the season that is at least 80% the size of its
  own largest squad — without that floor, a max over rosters picks the
  SMALLEST, because a mean over an elite subset beats a mean over a full squad.
  The bases genuinely disagree: Truck Stop's U.S. Open roster rates 181 Elo
  above the B-squad it last completed an event with.
- `home_advantage` is 0 — the knob is REMOVED. There is no home field:
  ultimate is played at neutral sites, and `games.home_id` is merely the team
  USAU prints first, which is the seed. Listed-first wins 70.1% of club games,
  but 78-81% in pools A-D against ~62% by the semis, which no venue effect
  would do. Its entire value was in the cold-start seasons where the ratings
  knew nothing and the seed did; on 2022-25 it is worth nothing (paired
  Δlogloss -0.00003, 90% CI [-0.00211, +0.00197]). Dropping it also means no
  prediction needs to know the schedule's listing order.
- `division_scale` is 260 for every division, replacing the global 400. The
  divisions never meet, so one global scale cannot calibrate them all — though
  once fitted on the full corpus they landed on the same number.
- `division_bases` — club 1500, college 1350, D-III 1250, UFA 1550 — is where
  a debutant enters, and it is the ONLY place the "this division is a weaker
  level" prior lives, since the pools do not play each other. Score each base
  on its OWN division's games: club logloss cannot see a pool it barely
  touches, and judging D-III there had VAL and TEST disagreeing by 800 Elo
  while D-III's own games give a sharp optimum all three metrics agree on.
- The corpus splits THREE ways: FIT 2017-2021 accumulates ratings, VAL
  2022-2023 selects hyperparameters, TEST 2024-2025 is reported only. The
  two-way split it replaces chose on the cold-start seasons and mis-selected
  three parameters, each scoring well only while the ratings knew nothing.
  Print a candidate's PER-SEASON gain: helping only 2017-2019 and decaying
  toward zero is the signature of a cold-start artifact.
- TEST 2024-25 club: accuracy 0.7855, Brier 0.1465, logloss 0.4511 — against
  0.7625 for team-level Elo with carryover and 0.7019 for a reimplementation
  of USAU's own v2.0 algorithm. The weak spot is championship events, where
  the model delivers 0.674 accuracy against the 0.76 its own probabilities
  imply; more data at other tiers has not moved it.
- `lo90`/`hi90` bound CURRENT SKILL, not future movement, and they DO NOT
  converge with games: `rating_sigma` is a flat 112, a ±184 band whether a
  player has 50 games or 300. Split-half reads 108.9 / 109.7 / 112.5 / 115.1
  at mean n = 63 / 97 / 145 / 215. The cause is structural: the hyperbolic
  provisional multiplier never decays to 1, so every game still moves a
  veteran, and `offseason_regression=0` removed the only pull toward an
  anchor. That is the price of the configuration's predictive gain, and
  `reg > 0` is the only lever that buys convergence back. Re-fit whenever the
  update dynamics change — it is a property of the replay, not of the sport.
- Join `player_elo.csv` on `player_id`, never on `player`. Names that survive
  as separate identities are still split per club, so display names are not
  unique. Four names also contain commas (`Gregory Plaia, Jr`), so parse the
  file as CSV — splitting on commas corrupts those rows.
- Rosters are public per event-team, names only (no stable player IDs).
  Nationals rosters include per-player stats (points/assists/Ds/turns) —
  unused by the model so far.
