# USAU Player-Level Elo

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
USAU_DB=data/usau_mixed.db scraper/backfill.sh club-mixed data/usau_mixed.db 2017 … 2026
USAU_DB=data/usau_women.db scraper/backfill.sh club-women data/usau_women.db 2017 … 2026
.venv/bin/python -m scraper.merge_divisions  # fold the two into data/usau.db
.venv/bin/python -m identity.resolve      # names -> player IDs (+ data/ambiguities.csv)
.venv/bin/python -m analysis.backtest     # walk-forward eval; reports TEST 2024-25
.venv/bin/python -m analysis.rankings     # data/player_elo.csv, data/team_elo*.csv, data/history.json
.venv/bin/python -m analysis.identify    # OPTIONAL, ~42 min: data/player_loo.csv
.venv/bin/python -m analysis.site         # docs/index.html + history.js + t,p,r,g/*.js — no server
```

The scrape is slow, disk-cached and resumable; `data/usau.db` and the raw HTML
cache are gitignored and rebuilt by the commands above. There is no club
2020 — COVID cancelled the series — but college 2020 exists and is kept: the
series was cancelled in March, the January-March regular season was not.

**Five divisions, 90,284 games.** club men's (19,682), club mixed (23,851),
club women's (8,426), college (33,375) and college D-III (4,950), 2017-2026.
Mixed and women's are scraped into their own DBs and merged in afterwards.
They have to be: `events.url` is UNIQUE per division because a tournament
cross-listed across divisions is one url (260 club/mixed urls collide), and
`event_id` is a local autoincrement, so the three files number unrelated
events identically. `scraper/merge_divisions.py` re-keys with a per-source
offset and is idempotent. Running the two scrapes into `data/usau.db`
directly would have each division overwrite the other's tag.

D-I and D-III share the College-Men competition level and the same schedule
URL, so `--division college` takes every event whose name is not D-III and
`--division college-d3` takes exactly those that are. The two sets are
disjoint by name, which is what keeps them one row each.

## Gender-matching

Every division shares ONE rating scale, bridged by mixed: 10,814 names appear
in both mixed and a men's division, 4,981 in both mixed and women's. Men's and
women's do NOT bridge to each other — 279 names appear in both and
`identity.resolve` splits every one of them, since no body plays both series.

Read a cross-gender gap carefully. Club men's and club women's teams never
play, so the only thing making their ratings commensurable is the mixed
division both feed. That is an inference through a bridge, not a head-to-head
result.

Each identity gets a group, in this order: division play where it exists
(women's → female-matching, men's → male-matching, 66,667 players), else the
majority of the roster page's Pronouns column (2,280), else a first-name
likelihood learned from the players the first two rules placed (7,886). The
prior compares `P(name | gender)`, not the male share of a name: the labelled
pool is 83% men, and a raw posterior threshold placed the remainder at 4.7 men
per woman. Corrected it lands at 1.09, which is the external check — mixed
rosters are gender-balanced by USAU's ratio rules. Held out on a balanced half
of the labelled pool it is 98.6% accurate at 63% coverage. The 3,124 identities
no rule places keep `gender=''` and appear only under "all genders";
`players.gender_source` records which rule fired.

## Front end

**https://sethlnx.github.io/usau-player-elo/** — `docs/index.html` plus a
deferred core and four on-demand tiers, no server and no network needed; see
*What loads when* below. It runs off a local checkout over `file://` exactly
as it does on Pages.

`analysis/site.py` emits it: searchable club rankings on three roster bases, a
searchable player table over all five divisions, a **Trends** tab drawing one line per
season for every player or club that has ever closed a season in the top 25,
a **Tournaments** browser over all 2,870 events, and a U.S. Open tracker that
re-runs a 40,000-sim Monte Carlo in the browser over whatever is left to play.
It reads only the published artifacts and never replays the model, so the page
cannot drift from `data/player_elo.csv` and `data/team_elo*.csv`.

Every tab filters by division, and what that means differs by tab — on
purpose. **Clubs** shows one division at a time (club men's / mixed /
women's), ranked within it and searchable by club name or the event it was
rated off, because those teams never play each other and a
merged 1..n would invite a comparison the games cannot settle. **Players**
follows the "2026 rosters only" toggle: with it on you get players in the
division NOW, with it off anyone who ever played it. Nobody is listed in two
CLUB divisions at once — men's, mixed and women's are alternatives, so the
club bits collapse to whichever division the player turned out in most that
season (ties to the most recent event) and someone on a mixed team shows only
under mixed. College is deliberately NOT collapsed against club: it runs in
the spring and club in the summer, so 1,789 players are genuinely in both in
2026 against just 217 who touched two club divisions, and a college senior who
also plays club belongs in both lists. That distinction is not
cosmetic — a career-wide mask left 43% of the club men's list stale (Nathan
Champoux last played men's in 2018 and has been on Hybrid since 2019, and
every graduated player still sat under college). Either way the rating is the
single number they carry everywhere, not a per-division rating. **Trends**
selects POINTS rather than subjects, so a season reads as the rating after
that season's last event in the division picked.

**Gender-matching** selects whole subjects everywhere, since nobody changes
group between events; it is disabled for clubs, which have no group. Neither
filtered gender view contains the 3,124 unplaced identities, so the two never
sum to the unfiltered count. The U.S. Open tracker remains club men's.

### Tournaments

USAU publishes a flat fixture list per event and nothing about its shape, so
the shape is **recovered** rather than read. The `stage` column is free text
typed by thousands of organisers and runs to 3,300 distinct values, including
`Chumpionship 9`, `GAME TO GO TO THE GAME TO GO` and `Round Name`. Two
different strategies, in `analysis/tournaments.py`:

**Pools are found structurally**, as sets of teams that have all played each
other — cliques in the co-play graph — because the labels carry no
information: 2,103 of the 2,680 events with pool play file every fixture under
one heading. Cliques consume EDGES rather than teams, so a placement round
robin reusing teams from the opening pools is still found, and a rematch
counts for the later pool instead of twice in the first pool's standings. The
clique chosen through a fixture is the one spanning the fewest CALENDAR DAYS,
size breaking the tie: structure alone cannot tell the U.S. Open's opening
pool of three from the 9-12 pool of four that reuses two of its teams — both
are cliques and the wrong one is bigger.

**Brackets are not.** Round rank comes from the organiser's own label
(`Pre-Quarters`, `Sweet 16`, `Gold Semi Finals`, `9th Place Quarters`), and
only the feeders are inferred, a slot reading back to the game that team won.
Deriving rounds from the results too — what this did first — turns a win-chain
through mislabelled pool play into a nine-round bracket that never existed;
showing no bracket is better than inventing one. Every rank-0 game is a root,
so a college invite running two flights off one schedule yields two trees
rather than one tree and a pile of orphans. Anything the label does not place
lands under **Placement & other games** with the organiser's own wording.
Across the corpus this puts 87.3% of the 90,505 completed games into a pool or
a bracket and loses none: every game is displayed somewhere.

**Labels also fence the structural recovery.** Most events name no pool at all
— 2,103 of the 2,680 with pool play file every fixture under one heading — so
when nothing matches "pool" the cliques are sought across the whole schedule.
That fallback used to run over the labelled games too, and on structure alone
a clique through the Final's edge is indistinguishable from a round robin, so
it sometimes ate one and the bracket lost its final. Centex 2023 lost
Colorado's universe-point title that way and the Northwest mixed Regional lost
BFG's. Excluding anything the label already places recovered 11 finals that
were never found, moved 355 games out of phantom pools into the brackets they
belonged to (84.6% -> 87.3% placed), changed no champion that was already
right, and lost none.

Cliques that tie on days and size then settle on their team names. That buys
nothing but reproducibility, and it is not optional: cliques come out of a
Bron-Kerbosch over sets of team-name strings, so an unbroken tie is resolved
by string hash order and the recovered shape changes between runs of the same
build. Five events used to flip their champion between builds.

A **series** is the printed name with everything that varies between instances
taken out — the year, the edition number (`Cooler Classic 30`), the division
wording, and whatever suffix that season used (`- ICC`, `(ICC)`). 2,870 events
collapse to 670 series, so opening the 2025 U.S. Open also shows the other 23
instances and who won each. Division wording goes because division is its own
facet: a Sectional's men's and women's halves are one tournament run twice.
"Open" is deliberately kept — it is load-bearing in "U.S. Open". Standings
break ties on head-to-head inside the tied group, then point differential;
unlike the U.S. Open tracker, which prices games that have not happened, every
game here has a score, so rating never enters.

Club identity is the normalized name, so "Rhino" and "Rhino Slam!" are one
club and a college program's D-I and D-III sides are one program. Mixed and
women's keys carry a suffix on top of that: 73 club names (5 active in 2026)
exist in more than one gender division, and men's Phoenix and women's Phoenix
are two teams. The men's group keeps the bare key, so every pre-existing
identity is byte-identical to before.

That selection is a union across seasons, so it is 67–164 lines depending on the
view rather than a fixed 25 — the point being that a club that owned 2019 and has
since folded sits on the chart beside this year's best. At that density no
stroke can identify a line on its own (8 hues x 8 dash patterns gives 64
combinations), so the interaction carries it: hovering a line or legend row
isolates it and drops the rest. **The legend is ranked on the most recent season
by default** — it opens as the current table, not as an all-time-peak list — and
hovering any other season in the chart re-ranks it on the one under the cursor,
showing that season's rating; leaving returns to the current season. A subject
that did not play the ranked season shows an em dash and sinks to the bottom
rather than sorting as zero, which would rank a club that skipped the year above
one that played badly.

The division filter selects **events, not teams**. 301 club identities play in
more than one division, so classifying a whole identity would misfile every one
of them — Colorado College (Wasabi) has both club and college events in 2024, and
its 2024 value is 1449 in the club view against 1512 in the college view. Within
a division a season reads as the rating after that season's last event *in that
division*, the population is only the subjects with an event there (968 club, 980
college, 316 D-III of 1,953), and the "above season median" baseline is
recomputed over that population.

### Field strength

The series tier above says what an event is FOR — Conference, Sectionals,
Regionals, Nationals. It says nothing about who was in it, and the two come
apart constantly: the Northeast men's Regional is a harder tournament than
most Sectionals will ever be, and Florida Warm Up is harder than the D-I
championship it feeds. So events carry a second, independent grade in
`analysis/field_strength.py`: **S / A / B / C / D**, from the attendees.

The model is Smash Bros' — the Tournament Tier System PGStats runs for the
Panda Global Rankings, which sorts events into S/A/B/C from two inputs: how
many entered, and how many highly ranked people entered. Three things carry
over. **Rank bands with steep decay** (PGRU pays 224 for a top-5 attendee
against 64 for a top-50 one) — what makes a tournament hard is its ceiling,
not its crowd. **Region multipliers**, so a smaller scene is not locked out of
the top tier for being small. And **the tier is a cutoff on a continuous
score**, which PGStats says out loud; both numbers are published here for the
same reason, because a high C is a low B.

Two things deliberately do not carry over.

**Attendance is not a second path to the top.** PGRU takes the HIGHER of an
attendance score and a ranked-attendee score, because a 1,200-entrant open
bracket in Smash genuinely is a supermajor. Ultimate fields run 5 to 45 teams
and the biggest are college regular-season invites, not championships — a
16-team Nationals is the hardest tournament of its year and among the smaller
draws. Size counts only through the attendees it brings: a club outside its
division-season's top fifth is worth zero, so a 40-team Sectional of unranked
teams scores zero rather than tiering up on bulk.

**The bands are percentiles, not fixed ranks.** PGRU's top-50 works because
Smash is one pool. Five divisions here run 67–500 clubs in a season, so a
fixed rank would make "top 50" the top tenth of college and the top third of
D-III. Moving the band from a rank to a percentile IS the region multiplier.

**Every rating is the one the club carried INTO the tournament** — its last
rated result strictly before the start date. Nothing an attendee did at the
event, or after it, can move the grade, so a tournament is never strong merely
because someone had a breakout there, and the number is exactly what was
knowable walking in. A club with no rated result yet is unranked and worth
nothing; it has not shown anything.

A club's standing is its rank on that point-in-time rating within its own
division and season. The pool is the clubs that turn out in that division that
season — the competitive field the event sits in — but every rating inside it
moves with the calendar, so the same club is a different rank in May than it
was in February and the standings are rebuilt per event. Ratings carry across
divisions and seasons (one number per club, the model's own convention), so
"current" means the club's last rated result anywhere, not its last one in
this division.

Each attendee scores by band, and the event's raw total is divided by what the
**16 best clubs in that pool as of that date** would have scored, 16 being a
USAU national championship field. So **100 means "as hard as this division's
Nationals ought to be"**, and it means the same in club women's as in college.
Above 100 is earned: Florida Warm Up 2026 draws 43 college teams and scores
114.3, beating the D-I championship it feeds.

| tier | score | what lands there |
|---|---|---|
| S | ≥ 90 | 41 events — every division's Nationals, every season, plus Florida Warm Up (2019, 2026) and Easterns (2018, 2023, 2024, 2026) |
| A | ≥ 60 | 64 — the elite regular season: Pro-Elite Challenge tops out at 86, Pro Championships at 80, the U.S. Open at 74 |
| B | ≥ 30 | 198 — the stronger Regionals (Northeast men's peaks at 44) and the better invites |
| C | ≥ 10 | 596 — Sectionals top out at 18 |
| D | < 10 | 1,944 — no ranked club attended |

Ties are routine: ratings are integers and the pool holds hundreds of clubs,
so equal ratings land on band edges. They break on the club key — arbitrary,
but stated, and the same on every rebuild.

Attendance is the set of clubs the model scored games for, so a team that
registered and never played is not counted. **27 events carry no grade at all
and show a dash rather than a D**, which is a different claim. Ten are novelty
4v4 and goalty brackets the model never rated. The other seventeen are events
where fewer than 32 clubs — twice the reference field — carried a rating on
the day, which happens in exactly two places: the first weekends of 2017,
where the corpus begins and nobody has played yet, and D-III's COVID-truncated
2020, where 19 rated clubs put a single team in the top 2% band. Outside
those, the thinnest pool any event faces is 48 clubs and the median is 244.

**The tracker takes played games from USAU, not from you.** Every fixture with
a final score arrives with it, renders the score where an unplayed game renders
the model's probability, and cannot be clicked away; the odds are conditioned
on it. What you can still enter is a call on a game not yet played, kept in
`localStorage` under the fixture's USAU game number.

Two things it stops guessing once the tournament starts. **Pools** are the sets
of teams that have all played each other, grown one fixture at a time — USAU
labels every opening fixture "Pool D" and publishes no pool letters at all.
Deriving them from the co-play graph, as this did until day one of 2026, merged
the field into two components of six the moment the winners' crossover was
seeded; the clique rule separates those two crossover games out instead, which
is also what keeps them out of the pool standings. **The bracket** is read off
USAU's published slots, which fill in as the tournament runs, replacing the old
"2nd plays 3rd cross-pool" assumption. Only the semifinal feed is still
assumed: quarters 1-2 into one semi, 3-4 into the other.

Clicking any name opens a drill-down: the rating curve, the full event history,
and — for a player — **which club he turned out for** at each event; for a club,
**rosters by season** behind a disclosure, tabbed newest-first. One table per
season, sorted strongest first, carrying each player's overall rank and Elo.
Past seasons show the union of every played event's listed squad — a club
re-registers each tournament — with an `Ev` column saying how many a player was
listed for. **The current season shows the best full-strength roster reported to
USAU instead**: the same squad `team_elo_best.csv` rates the club off, which may
be registered for an event not yet played. Truck Stop's 2026 tab is its 23-man
U.S. Open registration with twelve players at `Ev 0` — registered, not yet
played with. Clubs with no current club-division registration (college teams)
fall back to the union. Every name in the panel is itself a link, with a
`‹ Back` stack, and the open subject is reflected in the URL hash
(`#p/<player_id>`, `#c/<club key>`) so it is shareable and the browser Back
button works. A player below the 30-game floor has no rank or Elo to show and is
listed as plain text: he was on the roster, but there is nothing to open.

**Every event row opens onto the games behind it, with what each did to the
rating.** The curve is one point per tournament, which is the right grain for a
season and the wrong one for "why did Truck Stop drop 190 at Pro Elite
Challenge East" — so clicking an event expands it into that weekend's games:
stage, opponent (itself a link), W/L, score, and the club's rating change.
Only games the model scored are listed, so an expanded event is the whole
evidence for the Δ printed beside it; forfeits, cancellations and unseeded
bracket slots never appear.

**A club panel splits the row's Δ in two, because the games rarely explain it.**
A club's rating is the softmax mean of whoever took the field, so between two
events it moves both on results and on personnel. Truck Stop's U.S. Open row
reads `+248` off `+55 from results` and `+193 from a changed roster` — the
A-squad replacing the B-squad that went 2-4 in Colorado. Reporting only the
first number would look broken; reporting only the second would hide the
tournament.

The per-game number is the CLUB's move even in a player panel, and it is
labelled as such. The engine amplifies each game's delta by where a player sits in
their provisional window, so teammates do not move together: Tyler Monroe took
`+64` out of the U.S. Open weekend his club rated `+55` for. His own total is
the Δ on the row; recovering his per-game share would mean re-deriving the
engine in JavaScript, which is exactly what this page refuses to do.

**What loads when.** `fetch()` on a `file://` page is blocked by CORS; a
classic `<script>` from the same directory is not. That one asymmetry decides
the whole layout, because the page has to keep working off a thumb drive. So
every split below is a script tag, and what decides the tier is not size but
WHEN the thing is needed:

| tier | file | gzipped | when |
|---|---|---|---|
| page | `docs/index.html` | 1.32 MB | blocks first paint |
| core | `docs/history.js` | 0.48 MB | background, right after paint |
| tournament shapes | `docs/t/<season>.js` | 12–65 KB × 10 | an event is opened |
| trajectories | `docs/p/<pid % 32>.js` | 75–80 KB × 32 | a player panel is opened |
| rosters | `docs/r/<bucket>.js` | 59–82 KB × 32 | a club panel is opened |
| games | `docs/g/<season>.js` | 19–106 KB × 10 | an event row is expanded |

A cold visit that reads the rankings and leaves costs **1.8 MB gzipped**. It
used to cost 6.9 MB, because the whole 16 MB / 5.0 MB-gzipped trajectory
corpus — every rating, every roster, all 58,007 scored games — came down on
every visit whether or not anything was ever clicked. Nothing was wrong with
the corpus; what was wrong is that two features held it hostage, and both are
precomputed in `analysis/history_split.py` now:

- **Trends** walked all 39,325 trajectories to find a per-season median and a
  per-season top-25 cut. Both are statistics over the whole population, so no
  subset computes them, and that one function pinned the entire corpus. It can
  only ever be asked 24 questions (subject × division × gender-matching), so
  all 24 answers ship in the core at 40 KB gzipped.
- **The expandable-row test.** A panel's event table marks a row clickable
  only where the model scored games, which asked `games[evIdx]` per row and so
  wanted every season a subject ever played. `gameSides` answers it from the
  core: one sorted club-index list per event, 50 KB gzipped.

With those gone the panel needs exactly one bucket, and which bucket is
decidable without loading anything. Players key on `pid % 32`, which the page
reproduces directly. Clubs cannot — there is no string hash the emitter and
the page get to agree on for free — so a club's bucket rides on its
`rostByClub` entry, which the panel already looks up to know which seasons it
has rosters for. Rosters bucket by CLUB rather than by season so that opening
one club costs one fault for all ten of its season tabs.

Names inside a roster bucket are local to it: the page appends each bucket's
pool to the growing global one and rebases the indices as it merges, so a name
in two buckets is simply stored twice. Across 32 buckets that costs 22%, which
is cheaper than any scheme for sharing them. Roster members are still keyed on
`(name, player_id)` pairs, never on name alone, for the reason in the data
notes below: display names are not unique. Games stay grouped by event and
stored once each rather than once per side — the difference between 1.5 MB
and 3.

Bucket granularity is chosen against request overhead, not against total
bytes. Chunking costs 26% overall, because each file gzips in its own window;
what it buys is that nobody downloads a bucket they never open. Per-event
tournament files would have been 2,870 of them at ~170 bytes gzipped, where
the request costs more than the body and every rebuild churns the whole
directory. Ten seasons and 32 hash buckets put the worst single fault at
106 KB.

What deliberately stays inline is the index every view reads: `tourneys.events`
at 88 KB gzipped drives the tournament list, its filters, its search and its
champion column. That last one is why an event row carries its champion as a
GLOBAL team index — resolving it through the event's own field, as it did
first, made 400 rendered rows fault in 400 events' games to print 400 names,
and there was no on-demand split to be had while that was true.

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
  one body cannot do. Of 6,296 collisions only 580 conflict; the other 5,716
  auto-merge, since the raw rule keyed on (name, club) and so shattered a
  career over one messy season — each shard then re-debuting at its division
  base and re-burning a 6x provisional window, which inflated whichever shard
  sat on the strongest team. A name spanning a men's division and the women's
  division is the one case that always splits: 279 of them, two people each.
  Every collision, split or merged, lands in `data/ambiguities.csv` with its
  verdict; the 31,166 cross-division bridges log to
  `data/cross_division_links.csv`; review verdicts in
  `data/link_overrides.csv` (`block` = two people, `merge` = one person,
  `confirm` = bridge reviewed OK). A conflict proves two people, its absence
  only fails to disprove them, so same-named players in different regions who
  never coincide do merge wrongly — `block` is the correction. The same pass
  writes `players.gender` / `players.gender_source`; see Gender-matching above.
- `elo/` — the rating engine (`EloConfig` holds every knob). Debut players
  enter at their division's base rating (not teammate mean; the old
  context-init survives behind `EloConfig.context_init`), converging via an
  aggressive provisional multiplier. Offseason regression targets the
  division base of each player's last division.
  CAUTION: `team_rating` reaches unknown players through `_state`, which
  creates them at the GLOBAL base — `_materialize`, inside `play_game`, is what
  knows the division. Read a rating before that runs and every debutant in a
  non-club division enters 150-250 Elo too high, silently, for the whole
  replay. Use `pregame_ratings`, which materializes first; it exists because
  the per-game deltas in the site's drill-down needed a before-picture and the
  obvious `team_rating` call moved Truck Stop 123 Elo.
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
  college), the `descent.py` selection harness, CSV exports, and `bridge_audit.py` — ranks bridge
  links by false-merge suspicion (height contradictions across every division
  the bridge spans, how far merging moved the rating against a replay with
  every cross-division link severed, geography); `--write-overrides`
  auto-blocks hard height conflicts.
  `usau_baseline.py` reimplements USA Ultimate's own iterative rankings
  algorithm (v2.0) from our game data and scores it walk-forward on the
  same holdout — the plan's "comparison model 4".
  `usau_player.py` is that algorithm's rating form made player-level,
  stat-usage-weighted, and carried across seasons via a rolling date weight
  instead of a yearly reset — a batch fixed point solved with numpy rather
  than a sequential updater. Needs a prior pull toward the base rating to
  converge at all (see its module docstring); +10.1 accuracy points over the
  team-level original, still behind player Elo.
  `tournaments.py` recovers pools, brackets and tournament series from the
  fixture list (see **Tournaments** above) and hands `site.py` the payload the
  browser tab draws; it never touches ratings.
  `history_split.py` slices `data/history.json` into the resident core and the
  three lazy tiers `site.py` writes (see **What loads when**), and precomputes
  the two things that used to force the whole corpus into the browser: every
  Trends answer, and which clubs appear in each event's games.
  `field_strength.py` grades each event's field S/A/B/C/D from the standing of
  the clubs that played it (see **Field strength** above). It reads ratings but
  writes none, and `site.py` appends its verdict to the event rows rather than
  `tournaments.py` building it in — shape recovery stays rating-free.

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
- A fixture's `games.game_key` is NOT stable while a tournament runs. An
  unseeded slot has no `EventGameId`, so it is keyed on the page's own slot id
  ("bracket-game411456"); the moment USAU puts teams in it the row arrives
  under the real game id and the placeholder is orphaned. `games.slot` records
  the slot itself — the fixture's identity, which never moves — and
  `_drop_reseeded` deletes what a reseeding left behind. Without it a refetch
  mid-event doubles the bracket: the U.S. Open's four prequarterfinals became
  eight rows after day one, each played game shadowed by the teamless slot it
  replaced. The model never saw them (it filters NULL teams) but anything
  reading the schedule did.
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
- **A rating is only as identified as the player's influence on their team.**
  Softmax decides how much a player moves the team rating; the delta is
  applied equally. At the bottom those come apart — a player 2,300 Elo below
  his roster's best carries 0.4% of a 25-man team rating, so results say
  nothing about him while he keeps absorbing team-level noise, and with
  `offseason_regression` at 0 nothing pulls him back. Whatever the 6x
  provisional window did in his first 14 games was permanent: 340 players with
  100+ games sat below 900 Elo, 13 below zero, one of them on a 54-46 record
  recovering at +0.78 Elo/game. `low_info_anchor` (0.02) decays such a rating
  toward its division base in proportion to how little weight it carries,
  which cuts that to 36 and a floor of +657. Re-check it whenever tau moves:
  a flatter softmax pushes every share toward equal, which is what the anchor
  scales on, so tau 600 -> 900 made 0.01 go slack (54 stranded -> 120).
  Note what this says about the selection protocol: deleting all 340 from
  every roster moved TEST by <=0.0012, so logloss could not see them at all.
  It is the one published knob chosen against a pathology rather than a score.
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
- Selection scores the n-weighted VAL logloss across ALL FIVE divisions, not
  club men's alone — club men's is 19,682 of 90,284 games, so tuning global
  knobs on it would let 78% of the corpus be collateral. Per-division bases
  and scales are still scored on their own division's games.
  `python -m analysis.descent` is the harness: 21 axes, 3 passes, every
  surviving move dropped one at a time and kept only if reverting costs
  >0.0003 VAL. The last run converged in two passes and pruned twelve of
  thirteen improving moves as VAL noise.
- **The provisional window is the most valuable mechanism in the model.**
  Swept at the selected config, weighted VAL against the published point:
  multiplier 1/2/3/4/[6]/8/12 gives +.0297/+.0154/+.0071/+.0025/—/+.0036/
  +.0231, games 4/6/10/[14]/20/30/50 gives +.0031/+.0014/+.0001/—/+.0007/
  +.0027/+.0071, and hyperbolic beats exponential by .0039, linear by .0087,
  cliff by .0092. Turning it OFF costs 0.0297 — twenty times the entire
  descent's gain. It is also what gave the runaway its head start, which is
  why `roster_shrink` rather than removal is the answer: the most valuable
  mechanism here and the source of the worst pathology are the same one.
- TEST 2024-25 club men's: accuracy 0.7841, Brier 0.1464, logloss 0.4501 —
  against 0.7631 for team-level Elo with carryover and 0.7019 for a
  reimplementation of USAU's own v2.0 algorithm. Weighted across all five
  divisions, TEST logloss is 0.45389. The weak spot is championship events,
  where the model delivers ~0.67 accuracy against the 0.76 its own
  probabilities imply; more data at other tiers has not moved it.
- `home_advantage` now WINS on the numbers and is still not adopted: 15 scores
  VAL 0.45784 against 0.45930 at zero. It was worthless on the club-only
  corpus and is worth ~0.0015 on this one. The objection is not about the
  score — `games.home_id` is the team USAU lists first, which is the seed, so
  the knob imports USAU's judgement into a results-only model and makes every
  prediction depend on schedule listing order.
- `lo90`/`hi90` bound CURRENT SKILL, not future movement, and they finally DO
  converge with games: `rating_sigma` is `sqrt(470^2/n + 49^2)`, ±163 at 30
  games narrowing to ±92 at 300. Split-half reads 88.3 / 77.2 / 63.6 / 48.5 at
  mean n = 38 / 71 / 139 / 241. Every previous configuration in this project
  was FLAT — nothing pulled a rating toward anything, since hyperbolic never
  decays to 1 and `offseason_regression` is 0. `roster_shrink` supplies that
  anchor continuously, so more games now genuinely pin a player down.
  Re-fit whenever the update dynamics change — it is a property of the replay,
  not of the sport. It is also ONE number for a population where the
  uncertainty is plainly not uniform: bucketed by how much softmax weight a
  player carries, split-half sd/2 runs 161 for the lowest-influence tenth
  against 101 for the highest.
- **Within-roster credit is the model's hardest problem, and `roster_shrink`
  is the answer to it.** The game delta is applied to every rostered player
  EQUALLY, so nothing in the update distinguishes teammates: any spread that
  satisfies the team's weighted mean is equally consistent with every result.
  The spread came from provisional history and stat transfers instead, and it
  ran away. Before the fix: 219 of the top 1,000 ratings were anti-predictive
  (dropping the player from every roster did not hurt their own teams'
  predictions), the best player read 3444 against a best club of 2528, and
  corr(club rating, player rating) was 0.697.
  The fix shrinks every rostered player toward the team rating they just
  played under, which is FREE at the team level by construction — T is the
  softmax-weighted mean, so `sum_i w_i * lam * (T - r_i) = 0` and only the
  spread moves. Top rating 3444 -> 2645 against a best club of 2371, corr
  0.697 -> 0.857, and the #1 player became Adam Rees of Revolver: the best
  player on the best club. It also improved every division's TEST logloss and
  retired `low_info_anchor`, which had been paying logloss to rescue the
  opposite tail.
  The obvious alternative — pay the star more, `m_i` proportional to softmax
  weight — is WRONG, and measurably: renormalised so the team rating still
  moves by exactly the delta, VAL runs .45917 / .46010 / .46122 / .46429 /
  .46856 at alpha 0 / 0.25 / 0.5 / 1 / 1.5 while the top rating climbs 3444 ->
  4074. Paying the star more when the team wins accelerates the runaway.
- `analysis/identify.py` still measures the residue per player: drop them from
  every roster, replay, score their own teams' games. The page marks a band
  whose rating is not load-bearing with a "?". Two cheaper measures were tried
  and both FAIL: split-half is flat across softmax share, gap over roster
  median and teammate count, because both halves are reproducibly biased the
  same way; a static recompute off final ratings correlates +0.237 and gets
  the sign wrong on the worst cases. Hence one replay per player, top 1,000.
- Join `player_elo.csv` on `player_id`, never on `player`. Names that survive
  as separate identities are still split per club, so display names are not
  unique. Four names also contain commas (`Gregory Plaia, Jr`), so parse the
  file as CSV — splitting on commas corrupts those rows.
- Rosters are public per event-team, names only (no stable player IDs).
  Nationals rosters include per-player stats (points/assists/Ds/turns) —
  unused by the model so far.
