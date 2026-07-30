# USAU Player-Level Elo — Project Plan

**Scope:** Club Men's division, ~5 seasons of history (2021–2025, skipping the 2020 COVID gap), plus the current 2026 season as it happens.
**Deliverable:** Rankings + analysis — player Elo table, team Elo table, and a backtest showing whether player-level Elo predicts games better than team-level Elo and USAU's own rankings.

## The core idea

Every player carries a personal Elo across seasons. A team's Elo at any event is an aggregate of the players on its **event roster**. After each game, the team-level Elo update is pushed down to the rostered players. When a new season starts, a team's rating is simply whatever its current roster of players brings — no arbitrary reset, no cold start. Roster turnover (pickups, retirements, players switching teams) is reflected immediately.

## What we know about the data (Phase 0 findings, 2026-07-16)

The spike answered every open question:

- **Schedules/results:** every event has a division schedule page (`/schedule/Men/Club-Men/`) with clean, machine-friendly markup — pool games as `tr[data-game]` rows, bracket games as `div.bracket_game` blocks, both with team links, scores, status, and dates.
- **Rosters are public for regular-season events too**, not just the Championship Series (verified on Boston Invite 2024: 19 teams, 20-27 players each). No season-roster fallback needed.
- **No stable player IDs** — rosters are plain name strings with jersey number/position/height. Identity resolution (Phase 2) is name+team based, as planned.
- **Bonus:** Nationals rosters include per-player stats (points, assists, Ds, turns). Unused by the v1 model, but a future weighting signal.
- **Event enumeration** works via the site's WebForms search postback (competition level = Club-Men, season dropdown covers 2006-2027). ~56-88 Club-Men events per season for 2021/2024.
- **The hard constraint is the site's WAF:** after ~700 requests at 1 req/s, the server returns 500 on *everything* for an extended period. The scraper now paces at ~4-5s/request with multi-minute backoff on 5xx, caches all raw HTML, and resumes cleanly — but the full backfill takes hours and may need multiple sessions.
- The old [usau-scraper](https://github.com/erin2722/usau-scraper) library informed the postback approach but wasn't reusable directly (no caching, no season filter, no bracket parsing); we wrote our own.

An honest limitation, stated up front: **there is no per-game participation or stats data.** We know who was rostered for a tournament, not who played which points. So a player's Elo is *inferred* — two players who attend all the same events on the same team will have identical ratings. Individual signal comes only from roster differences: players missing events, playing multiple teams/events, and moving between teams across seasons. That's still enough to solve the stated problem (season carryover through roster turnover), but this is a "shared credit" model, not a measure of individual skill. The backtest in Phase 4 is what tells us whether the inferred player ratings carry real predictive information.

## The rating model (v1)

- **Player rating:** every player has one Elo, initialized at a global baseline (e.g., 1500) when first seen.
- **Team rating for a game:** a **weighted mean** of the rostered players' Elos, with higher-rated players weighted more — the assumption being that better players get more playing time, so they should make up more of the team's effective strength. Two candidate weighting schemes, both tunable:
  - *Softmax:* wᵢ ∝ exp(Rᵢ / τ). The temperature τ controls concentration — large τ recovers the plain mean, small τ approaches "team = its best players." Smooth, and handles the common case of teammates with identical ratings gracefully (they get equal weight).
  - *Rank-based playing-time curve:* sort players by Elo and assign weights from a fixed curve shaped like real club rotations (top ~7–10 heavy, bench tail light), splitting weight evenly among ties. More stable — weights don't shift with small rating changes — but needs tie handling and a hand-designed curve.

  Default to softmax for v1; the rank curve is the ablation.
- **Game update:** standard Elo expected-score from the two team ratings, with a margin-of-victory multiplier (USAU's own algorithm uses score ratio; we borrow that shape so 15–5 moves ratings more than 15–13). The team's delta *D* is applied to **every rostered player equally**. The weighting affects only how the team's strength is computed, not how credit is distributed — and since the weights are normalized to sum to 1, adding *D* to every player still moves the weighted team mean by exactly *D*. Within a team, rating separation therefore comes only from differing event attendance and team changes, never from the update rule itself — which avoids any rich-get-richer feedback between rating and weight.
- **New/provisional players:** higher K-factor for a player's first ~10 games so they converge quickly without dragging an established team around (FIDE-style provisional ratings).
- **Offseason:** no reset. Optionally regress every player a small fraction toward the baseline (tunable, maybe ~10–25%) to account for aging and rust; inactive players decay slowly. Whether this helps is an empirical question for Phase 4.

All the knobs — K, provisional K, MOV curve, the weighting scheme and its temperature τ, offseason regression — get tuned against the backtest, not chosen by feel. In particular, the backtest should include an **unweighted (plain mean) ablation**: if weighting by rating doesn't beat the plain mean out of sample, the playing-time assumption isn't earning its complexity.

## Phases

### Phase 0 — Feasibility spike (do this before anything else)
Scrape **one** recent event end-to-end by hand: 2025 Club Men's Sectionals or Nationals. Get the game list with scores and every team's event roster into a dataframe. This answers the known unknowns above and tests whether usau-scraper still works or we write our own parser. **Exit criteria:** one event's games + rosters parsed cleanly, and a decision on public-roster coverage and player IDs.

### Phase 1 — Data layer
- Enumerate all sanctioned Club Men's events per season (2021–2025) from the event schedule pages.
- For each event: fetch schedule/results pages and roster pages. **Cache raw HTML to disk first, parse separately** — the site is slow and we don't want to re-fetch when a parser bug surfaces. Rate-limit politely (~1 req/sec).
- Parse into SQLite (or parquet) tables: `events`, `teams`, `games` (event, round, team_a, team_b, score_a, score_b, date), `rosters` (event, team, player_name, raw attrs).
- Data-quality report: events missing rosters, games missing scores, forfeits/byes.

### Phase 2 — Player identity resolution
Map roster name strings to canonical player IDs across events and seasons. If Phase 0 found stable IDs in the HTML, this is easy. If it's names only: normalize (case, accents, nicknames like Mike/Michael), then match with team-continuity heuristics — "J. Smith on Ring of Fire 2023" and "John Smith on Ring of Fire 2024" are almost certainly the same person; two John Smiths on different regions' teams the same weekend are not. Keep an explicit `aliases` table and a review file of ambiguous cases rather than silently guessing. This phase is the biggest correctness risk in the whole project — a bad merge corrupts two players' histories.

### Phase 3 — Elo engine
A small, pure library: replay all games in chronological order, maintaining player ratings, emitting a rating history (player, date, rating) and per-game predictions. Deterministic and fast (a few thousand games/season — trivial), so the whole 5-season history re-runs in seconds when a knob changes.

### Phase 4 — Backtest & tuning
Walk-forward evaluation: for every game, predict the winner from pre-game ratings, then score accuracy, Brier score, and log-loss. Compare four models:
1. Player-level Elo (this project)
2. Plain team-level Elo with yearly reset (the baseline this project claims to beat)
3. Plain team-level Elo with naive carryover (same team name = same rating)
4. USAU's published rankings, where available, as a sanity reference

Tune hyperparameters on 2021–2023, evaluate on 2024–2025. **This phase is the point of the project** — if (1) doesn't beat (2) and (3), the shared-credit inference isn't extracting real signal, and that's a finding worth having, not a failure.

### Phase 5 — Outputs & analysis
- Current player Elo table and team Elo table (CSV + notebook).
- Season-opening team ratings for 2026 derived purely from rosters — the payoff demo.
- Player trajectory charts; case studies (a star player switching teams, a team that lost half its roster).
- Writeup of backtest results and model limitations.

## Tech choices

Python; `requests` + BeautifulSoup for scraping; SQLite for storage; pandas + a notebook for analysis. Suggested layout:

```
usau-player-elo/
  scraper/        # fetch + cache + parse
  data/raw/       # cached HTML (gitignored)
  data/usau.db    # parsed tables
  identity/       # name resolution + aliases table
  elo/            # rating engine
  analysis/       # backtest + notebooks
```

## Risks, honestly ranked

1. **Roster availability for regular-season events** — could force the season-roster fallback for much of the data. Resolved in Phase 0.
2. **Name collisions / identity resolution errors** — the quiet corruptor. Mitigated by conservative matching + human-reviewable ambiguity log.
3. **Scraping fragility** — old pages differ, site changes mid-project. Mitigated by caching raw HTML so parsing is repeatable.
4. **The model just might not beat team-level Elo** — possible, measurable, and the backtest is designed to say so cleanly.

## RESULTS (built 2026-07-16)

Full pipeline complete. Scraped **2021–2026: 12,702 scored games, 97,423 roster entries, 20,633 resolved players.**

**Backtest — walk-forward, tuned on 2021–2023, evaluated on the 2024–2025 holdout:**

All numbers below are from a single consistent re-run on 2026-07-20, after
the college backfill enlarged the corpus to 27,857 scored games (15,584
college). **Every model gets the same treatment: a logistic temperature
fitted on the 2021–2023 club train seasons.** That matters — see the
calibration note below.

| Model | Accuracy | Brier | Log-loss |
|---|---|---|---|
| Player Elo (weighted) | **0.7612** | **0.1600** | **0.4869** |
| Team Elo, naive carryover | 0.7457 | 0.1700 | 0.5112 |
| USAU-form player batch re-rating | 0.7411 | 0.1716 | 0.5142 |
| Team Elo, yearly reset | 0.6677 | 0.2041 | 0.5950 |
| USAU published algorithm (team-level, faithful) | 0.6405 | 0.1890 | 0.5593 |

(The earlier 2026-07-16 table read 0.7614 / 0.1640 / 0.5007 for player Elo.
Accuracy is unchanged; the probabilistic metrics moved because the corpus
grew and because temperature is now fitted uniformly.)

**Comparison model 4 (added 2026-07-19):** USAU's own rankings algorithm
(v2.0, as published) reimplemented from scratch in
`analysis/usau_baseline.py` — the iterative opponent-rating average with its
score-differential curve, date/score weights, and blowout-ignore rule —
evaluated walk-forward with ratings recomputed from strictly earlier games
at each game date within a season (USAU resets every team to 1000 each
season). Formula spot-checks matched USAU's published example (15-11 →
x=381) and the boundary cases (one-point games → 125, W>2L → capped at 600).
At **0.6405 it is the weakest model here**, behind even yearly-reset team
Elo. That is not a knock on the algorithm: it optimizes season-long
strength-of-schedule for bid allocation, not per-game forecasting, and has
no cross-season carryover by design.

**Model 5 — USAU-form player batch re-rating (added 2026-07-20):**
`analysis/usau_player.py` keeps USAU's rating *form* (self-consistent
"your rating is the weighted average of the game ratings you earned") but
makes it player-level, stat-usage-weighted, and — the key change — lets the
exponential date weight decay *across* the season boundary instead of
resetting, so strength carries into a new year through the roster. Result:
**0.7411, a +10.1 accuracy-point gain over the faithful team-level version
of the same algorithm** (0.6405 → 0.7411), and comfortably past both
reset-based models. It still loses to player Elo on all three metrics.

Two findings from building it that are worth more than the ranking:

- **A batch fixed point at player level does not converge without a prior.**
  Row-stochasticity buys spectral radius 1, not convergence. The first
  implementation diverged to ratings of −34,000, growing snapshot over
  snapshot, for two reasons specific to going player-level: the all-ones
  eigenvector gets one copy per *connected component* of the game graph (a
  730-day player-level window fragments into many; a USAU division-season is
  one blob), and isolated pairs oscillate with period 2. A prior of 2
  pseudo-games at 1000 per player makes the map strictly substochastic —
  unique fixed point, guaranteed convergence, every component anchored — and
  incidentally made it ~100× faster, since it now converges instead of
  grinding to the iteration cap. It is also the statistically right thing:
  two games should not place a player 600 points out.
- **Elo's native logistic (scale=400) is substantially miscalibrated on this
  data.** Fitting the temperature on the train seasons — which changes no
  ranking and no accuracy, only the probabilities — improves player Elo's
  log-loss from 0.5354 to 0.4869 and its Brier from 0.1769 to 0.1600. That
  is a free improvement available to the published rankings today, and it is
  independent of everything else in this project. Before this was applied
  uniformly, the batch model appeared to have the best calibration of any
  model; it was the only one whose temperature had been fitted. Level the
  playing field and that edge disappears entirely.

**What it means:**
- **The project's core claim holds decisively.** Player-level Elo beats yearly-reset team Elo by **+9.4 points of accuracy** (76.1% vs 66.8%) and a large log-loss margin. Carrying strength through players across the season boundary genuinely solves the cold-start problem.
- **Vs naive team-carryover, the edge is modest** (+1.6 pts accuracy, better Brier and log-loss). So most of the gain over "reset" is carryover *per se*. The per-player mechanism's real advantage is *qualitative*: it can rate brand-new teams built from veterans, teams that lost half their roster, and players who switch clubs — none of which team-name carryover can do. That's exactly what makes the 2026 season-opening ratings meaningful.
- **The rating-weighting (higher Elo → more weight) helps calibration, not accuracy.** Weighted vs plain-mean: better Brier/log-loss, essentially tied on raw accuracy. Tuning picked τ=150 (moderate weighting).
- **Offseason regression tuned to 0** — regressing toward the mean didn't improve prediction. Absolute ratings drift upward year-over-year, but relative order (what drives predictions) is unaffected, so it doesn't matter for forecasting. The ~2500 top ratings are an artifact of K=60 + no regression; they're on an internal scale, not comparable to a 1500-centered ladder.

Tuned config: K=60, τ=150, MOV on, provisional K×2 for 10 games, no offseason regression.
Outputs: `data/player_elo_final.csv`, `data/team_elo_2026_final.csv`.

**Known limitations (unchanged, now quantified):**
- Shared-credit ceiling: teammates with identical event attendance get identical ratings; individual signal only comes from roster differences and team changes.
- Club-identity fragmentation (e.g. "Dark Star" vs "Dark Star-D") isn't resolved — the player-identity work has no club-level equivalent yet.
- Per-player Nationals stats (points/assists/Ds) now feed both the Elo model
  (involvement credit + stat transfer) and the batch re-rater's usage
  weights, but they move the holdout metrics very little — only 224 club
  team-events report stat lines, so most rosters fall back to neutral usage.
- Two games (2024 Northeast Regionals) carry a source-data date typo of
  `2001-09-23`. Harmless — they fall outside every rolling window and get
  predicted at 0.5 — but any date-windowed model should guard against it.

## Operational notes (the scrape was the hard part)

play.usaultimate.org's WAF blocks by **instantaneous concurrency**, not cumulative volume: a single sequential process survives ~200+ requests/several minutes, but 3 parallel processes on one IP trip in ~20–30 seconds. Conclusion: **single-process is optimal on one IP; parallelism only helps with one exit IP per worker.** The scraper self-heals via an auto-resume loop that waits out blocks and resumes from the content-addressed HTML cache on VPN reset.
