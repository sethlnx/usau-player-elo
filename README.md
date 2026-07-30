# USAU Player-Level Elo (Club Men's)

Every player carries a personal Elo across seasons; a team's rating at an
event is the softmax-weighted mean of its event roster's player Elos (better
players weigh more, assuming more playing time). Game deltas are applied to
every rostered player equally. New seasons need no reset — a team's opening
rating falls out of whoever is on its roster.

Full plan: `USAU_by_player_elo.md`.

## Pipeline

```
.venv/bin/python -m scraper.build_db --division club    2017 2018 2019 2021 2022 2023 2024 2025 2026
.venv/bin/python -m scraper.build_db --division college 2021 2022 2023 2024 2025 2026
.venv/bin/python -m identity.resolve      # names -> player IDs (+ data/ambiguities.csv)
.venv/bin/python -m analysis.backtest     # walk-forward eval; reports TEST 2024-25
.venv/bin/python -m analysis.rankings     # data/player_elo.csv, data/team_elo*.csv
.venv/bin/python -m analysis.site         # docs/index.html — standalone, no server
```

The scrape is slow, disk-cached and resumable; `data/usau.db` and the raw HTML
cache are gitignored and rebuilt by the first two commands. 2020 does not exist
— COVID cancelled the club series.

## Front end

Live: **https://sethlnx.github.io/usau-player-elo/**

`analysis/site.py` emits a single self-contained `docs/index.html`: club
rankings on three roster bases, a searchable player table, and a U.S. Open
tracker whose bracket you fill in as games finish, re-running a 40,000-sim
Monte Carlo in the browser against whatever you have entered. It reads only
the published CSVs and never replays the model, so the page cannot drift from
`data/player_elo.csv` and `data/team_elo*.csv`.

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
- The site WAF blocks per-IP after a request budget (~50-150 live requests,
  occasionally 1000+), largely independent of pace. `fetch.py` paces via
  USAU_RATE_LIMIT/USAU_JITTER (default 1.5s+0-0.75s), probes briefly on 5xx,
  then raises SiteBlocked — switch VPN and re-run to resume from cache.
- Schedules cached on/before an event's last day are refetched once the
  event is over (mid-event fetches lack final scores); current-season event
  enumeration bypasses the postback cache so new events appear.
- 269 of 30,598 scored games (0.88%) carry a `games.date` outside their event's
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
- `team_elo.csv` is club-only and as-of completed events. Rosters for an
  upcoming tournament are posted weeks early; rating a team off one makes the
  table move on registration timing instead of on results.
- The listed-home side wins 68.7% of club and 69.0% of college games while the
  ratings alone expect ~61%: USAU lists the favoured/higher-seeded team first,
  and that ordering carries information the ratings do not. `home_advantage`
  (25 Elo) credits it in the prediction AND the update — omitted from the
  update, the bias quietly feeds rating to whoever is listed first, which is
  systematically the stronger team, inflating the whole scale. It cuts the
  club bias to +0.030 and the college bias to +0.040; tuning targets logloss,
  not zero bias, so a residue is expected.
- `division_scale` (290 club, 300 college) replaces the global 400. The two
  divisions never meet, and their rating-difference -> win-probability curves
  have different slopes, so no single scale calibrates both; 290 matches the
  286 `usau_baseline.py` fits independently. Together with `home_advantage`
  this moved held-out 2024-25 club logloss 0.4954 -> 0.4787 and accuracy
  0.7656 -> 0.7700, and college logloss 0.5133 -> 0.5029.
- `lo90`/`hi90` bound CURRENT SKILL and converge with games:
  `sigma(n) = sqrt(551^2/n + 53^2)`, ±87 asymptotically. They are not a
  forecast of future movement. The earlier `sqrt(110^2 + 445^2/n)` was fit to
  how far a rating still travels before settling, and its 110 was skill drift
  — a random walk of ~12 Elo/game, flat in `n`, scaling as `sqrt(horizon)`,
  which pinned every veteran at ±185 forever. For a drift band, state a
  horizon: `12 * sqrt(games_ahead)`. The 53 floor is identification, not
  drift: shared game deltas only separate teammates who appear in varied
  roster combinations, leaving a residue no game count removes. Both constants
  are properties of the replay — re-fit them when the engine changes.
- Join `player_elo.csv` on `player_id`, never on `player`. Names that survive
  as separate identities are still split per club, so 339 display names (1,288
  rows, 4% of the file) are non-unique.
- Rosters are public per event-team, names only (no stable player IDs).
  Nationals rosters include per-player stats (points/assists/Ds/turns) —
  unused by the model so far.
