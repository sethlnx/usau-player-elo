# USAU + International Player-Level Elo

### → **[Live rankings](https://sethlnx.github.io/usau-player-elo/)**
### → **[Glicko-2 sister rankings](https://sethlnx.github.io/usau-player-glicko/)**

Every player carries a personal Elo across seasons and, where the identity
bridge is unambiguous, across USAU, European, and official WFDF world
competition. A team's rating at an event is the softmax-weighted mean of its
event roster's player Elos
(better players weigh more, assuming more playing time). Game deltas are
applied to every rostered player equally. New seasons need no reset — a team's
opening rating falls out of whoever is on its roster.

Full plan: `USAU_by_player_elo.md`.

## Coach ratings

USAU's GraphQL team metadata supplies the published coaching staff. Two
walk-forward ratings use only games where both teams name at least one coach;
an unknown opponent is excluded rather than invented as a 1500-rated staff.
When several coaches are listed, the staff rating is their mean and every
coach receives the same staff update.

**Impact Elo** answers whether a coach's teams repeatedly beat the expectation
set by their players. A coach starts neutral at 1500. Before each game, the
model adds each staff's deviation from 1500 to that team's pre-game roster Elo,
forms the win probability on the division's published scale, and applies a
$K=24$ update from the residual. A 1500 impact rating changes nothing; a higher
rating is evidence of performance the roster rating did not already explain.

**Results-only Elo** is the control. It ignores both rosters and rankings,
forms the expectation from the two staff means on a conventional 400-point
scale, and applies the same $K=24$ update. `data/coach_metrics.json` compares
roster Elo, roster-plus-impact Elo, and results-only Elo on the identical
covered games, including a walk-forward 2024–25 slice.

The upstream `Coaches` field is free text, not person IDs. Explicit role
markers are authoritative. Unmarked values are accepted only when they parse
as one or more two-token names; ambiguous text is omitted instead of merging
several people into a false identity. `coach_key` is therefore a conservative
normalized name key, not a verified USAU person identifier.

## Refresh and publish

From a clean `main` checkout with the local `data/usau.db` and `data/euf.db`
corpora present, run:

```bash
.venv/bin/python refresh_and_publish.py
```

The command pulls `origin/main` with `--ff-only`, tops up newly finished or
incomplete USAU events in the current season, rebuilds bracket structure,
player identity, Elo outputs, and the static site, runs the focused publication
checks, commits only the generated data and `docs/` assets, then pushes
`main`. GitHub Pages deploys the pushed `docs/` site.

The first refresh after installing coach ratings also backfills coach metadata
for every finished historical event. This one-time pass ignores the season
filter so the published model cannot silently use current-season coaches only.

Refresh more than one season when an older result changed:

```bash
.venv/bin/python refresh_and_publish.py --season 2025 --season 2026
```

Preview the exact pipeline without reading data, changing files, or using the
network:

```bash
.venv/bin/python refresh_and_publish.py --dry-run
```

The command refuses a dirty worktree, a branch other than `main`, or missing
source databases. A failed refresh or check is not committed or pushed.

### Trigger the refresh from the site

Set up the self-hosted runner once:

1. In the repository, open **Settings → Actions → Runners → New self-hosted
   runner**, choose macOS, and follow GitHub's installation commands.
2. Add the custom label `rankings` while configuring the runner, then install
   and start it as a service. The runner must be online when a refresh starts.
3. Under **Settings → Secrets and variables → Actions → Variables**, create
   `RANKINGS_REPO_PATH` with the absolute path of this persistent checkout.
4. Keep that checkout on a clean `main` branch with `.venv/bin/python`,
   `data/usau.db`, and `data/euf.db` present.
5. Under **Settings → Actions → General**, allow workflows read and write
   access to repository contents.
6. Under **Settings → Pages → Build and deployment**, change **Source** to
   **GitHub Actions**.

On the rankings site, press **Refresh rankings**. GitHub requires a signed-in
collaborator with write access to press **Run workflow**; public visitors can
open the workflow page but cannot run it. The refresh job uses its short-lived
GitHub token for the push, then a separate least-privilege job deploys `docs/`
to Pages—the site never receives a repository credential.

## Pipeline

```
.venv/bin/python -m scraper.graphql 2014 … 2026 --division all   # the whole corpus, ~13 min
.venv/bin/python -m scraper.structure     # attach published bracket structure
.venv/bin/python -m identity.resolve      # names -> player IDs (+ data/ambiguities.csv)
.venv/bin/python -m scraper.euf_ranking data/raw/euf-ranking/rosters.json
.venv/bin/python -m scraper.wfdf all --workers 12  # all completed WFDF events on the official index
.venv/bin/python -m scraper.euf --audit       # must report publication_blocked: false
.venv/bin/python -m scraper.ultimate_canada --list --base-url https://cuc2026.ultimatecentral.com
.venv/bin/python -m scraper.ultimate_canada_database --all --year 2026
.venv/bin/python -m analysis.euf_overlap      # review name candidates before replay
.venv/bin/python -m analysis.backtest     # walk-forward eval; reports TEST 2024-25
.venv/bin/python -m analysis.rankings     # player/team/coach Elo, roles, metrics, history
.venv/bin/python -m analysis.player_metrics # offline UFA scorecard; add --refit-reference only to recalibrate
.venv/bin/python -m analysis.identify    # OPTIONAL, ~42 min: data/player_loo.csv
.venv/bin/python -m analysis.site         # docs/index.html + history.js + t,p,r,g/*.js — no server
```
The sister Glicko-2 publication reuses this complete corpus and static site:

```bash
.venv/bin/python -m analysis.glicko_rankings
RANKINGS_DATA_DIR=data/glicko \
RANKINGS_SITE_OUT=../usau-player-glicko/docs/index.html \
RATING_NAME=Glicko-2 .venv/bin/python -m analysis.site
```

Its event-period model, native uncertainty bands, and matched prediction
metrics are documented in
[`usau-player-glicko`](https://github.com/sethlnx/usau-player-glicko).


**The GraphQL mirror is the default source.** One command pulls all sixteen
divisions for all thirteen seasons into `data/usau.db` in about thirteen
minutes. The HTML scrape it replaces needed a request per event page plus one
per team roster, spaced 1.5s apart, and took hours per division punctuated by
WAF blocks that each need a VPN switch. `--division all` writes every division
into ONE file, which is what retires the per-division split DBs and
`merge_divisions` entirely: `events` is keyed UNIQUE(url, division) so a
cross-listed tournament holds one row per division, and `event_id` is a local
autoincrement, so there is nothing left for the merge offsets to fix.

`scraper/build_db.py` (HTML) is kept and still works — it is the independent
source `--validate` checks the mirror against, and it recovers the handful of
games the mirror can score but attribute to only one side. Use it to audit,
not to build:

```
.venv/bin/python -m scraper.build_db --division club-men 2014 … 2026   # HTML, slow
.venv/bin/python -m scraper.graphql --validate data/usau_html_reference.db data/usau.db \
    --division club-men
```

`data/usau.db` is gitignored and rebuilt by the commands above. There is no
club 2020 — COVID cancelled the series — but college 2020 exists and is kept:
the series was cancelled in March, the January-March regular season was not.
The masters brackets carry 2020 rows too, and all 51 hold zero played games:
USAU opened registration before cancelling, so they are shells. They cost
nothing — the model only reads games with scores — and are left in rather than
special-cased, exactly as the 463 other empty events already were.

### Ingest women's professional league stats

Pull every dataset advertised by the PUL Stats Hub manifest:

```bash
.venv/bin/python -m womens_pro.scrape pul
```

The WUL Stats Dashboard has no stable download API. In its Player Data or Team
Data tab, select a view, click **Download**, then ingest the CSV with a stable
dataset slug:

```bash
.venv/bin/python -m womens_pro.scrape wul 2026 player-standard-game ~/Downloads/wul-players.csv
.venv/bin/python -m womens_pro.scrape wul 2026 team-standard-game ~/Downloads/wul-teams.csv
```

Repeat the WUL command for every exported view. Reusing a league, season, and
dataset replaces that export, so refreshed downloads cannot leave stale rows.
Both importers write `womens_pro_sources` and `womens_pro_records` in
`data/usau.db`. Common team, player, opponent, date, and season fields are
indexed; `payload_json` retains every source column, including metrics added
after this importer was written.

### Women's professional data coverage

PUL ingestion follows `api/v1/index.json` rather than a hard-coded year list.
It currently provides the all-season game results and team directory plus each
manifest season's schedule, standings, and team statistics. The first-party
manifest does not publish player rows.

WUL dashboard CSVs provide player and team standard and advanced views. The
importer is deliberately lossless and view-agnostic because the dashboard
publishes CSV exports through a stateful Shiny session rather than versioned
API endpoints.

`analysis.rankings` replays PUL's canonical `games` rows and WUL's
`team-standard-game` rows. WUL's `player-standard-game` rows provide each
game roster; mirrored team score rows are deduplicated and checked for
agreement. A unique WUL name reuses a USAU identity only when both sources
roster it in the same season. The PUL manifest has no player endpoint, so its
scores update explicit synthetic team-season identities without inventing
player records.

**An event scraped mid-tournament stays that way.** Nothing ever went back for
the rest, so the 2026 U.S. Open sat at 26 of 36 games for two days after it
finished and the 2026 Lehigh Valley Invite at 42 of 93. `scraper/refresh.py`
is the fix: it finds events that have ENDED but still hold an unplayed fixture,
asks the mirror whether it knows more, and replaces only those that are
genuinely behind — 306 club men's events looked stale on the cheap DB filter
and exactly 2 were. Run over the whole corpus it topped up 7 events and 125
played games; the college divisions were already complete.

It writes every division directly to the unified `data/usau.db`. The old
per-division files and `merge_divisions` belong only to the retired HTML
ingest; routing a refresh through them could overwrite newer unified rows.
Replacement is wholesale through
`scraper/graphql.py`, because for a game we hold no result for there is no
score to match on and the team pair alone cannot separate a pool meeting from a
bracket rematch.

**Sixteen divisions, 162,098 scored games, 2014-2026.** college men's
(46,028) and its D-III (7,623), college women's (31,041) and its D-III
(3,390), club mixed (30,966), club men's (25,905), club women's (11,083);
then the age-restricted brackets — masters men's (1,446), masters mixed
(1,305), grand masters men's (1,207), masters women's (829), great grand
masters men's (702), grand masters women's (291), grand masters mixed (203),
great grand masters women's (79). Great grand masters mixed exists on the
source but has never been contested: zero events in all thirteen seasons.

2014 is a floor imposed by the SOURCE, not the scraper. USAU's event and
roster system begins there; every earlier season the dropdown offers is empty
scaffolding, and the mirror independently starts in 2014 too.

All sixteen now come from the mirror in one pass into one file, so the
per-division split DBs (`usau_mixed.db`, `usau_women.db`,
`usau_college_women*.db`) and their merge offsets are retired. What made them
necessary was the HTML path: `events.url` is UNIQUE per division because a
cross-listed tournament is one url (260 club/mixed urls collide), and
`event_id` is a local autoincrement, so separate scrape FILES numbered
unrelated events identically. One file has no such problem.

D-I and D-III share the College-Men competition level and the same schedule
URL, so `--division college` takes every event whose name is not D-III and
`--division college-d3` takes exactly those that are. The two sets are
disjoint by name, which is what keeps them one row each.

### Masters, and what puts it on the same scale

The age brackets are a separate series: a masters roster's games only ever
price it against other masters teams, so rating one on the open scale by
assertion would be a category error. They are nonetheless on the same scale
here, for the same reason college and club are — **shared players**. 9,305
people appear in both a masters bracket and an open division, which is what
anchors the two pools to each other: masters mixed <-> club mixed (3,626),
masters men's <-> club men's (2,678), grand masters men's <-> club men's
(1,308), and even great grand masters men's <-> club men's (558).

The bridge is dense enough to work, and the evidence it worked is a gradient
nobody encoded. All three men's brackets were given an IDENTICAL prior (base
1500, scale 260 — see `PUBLISHED` in `analysis/rankings.py`), and the ratings
still separate by age in the right order and settle below open club's ceiling:

| division | teams | median | max |
|---|---:|---:|---:|
| masters men's | 40 | 1650 | 2135 |
| grand masters men's | 27 | 1613 | 2100 |
| great grand masters men's | 27 | 1532 | 2069 |
| club men's | 218 | 1606 | 2359 |

Masters medians sit at or slightly above club's because the brackets are
shallower — self-selected, experienced squads, with fewer weak teams dragging
the middle down — while the CEILING stays below club's, which is the check
that matters. Read a masters rating the way the gender note below asks you to
read a women's one: a position in a pool linked to the open pool by shared
players, not a prediction of a game USAU never schedules.

These bridges go through `analysis/bridge_audit.py` like every other:
47 of the 10,113 masters bridges show a hard height contradiction (0.46%,
against 0.32% across the rest), and they land in `data/bridge_audit.csv` for
review rather than being merged silently.

### The GraphQL mirror

`scraper/graphql.py` pulls the same events, games, teams, rosters, and
published coach metadata from the third-party mirror at
`usau-rankings.fly.dev`, which serves USAU's own data
without the WAF. It is a different shape of cost: one request per event
returns teams, coach metadata, rosters, and games together, so a
division-decade lands in about 30 seconds against the HTML path's hours of
1.5s-spaced requests and VPN-switch stalls.

```
USAU_GQL_DB=data/usau_gql_d3w.db .venv/bin/python -m scraper.graphql \
    2014 … 2026 --division college-women-d3
.venv/bin/python -m scraper.graphql --validate \
    data/usau_college_women_d3.db data/usau_gql_d3w.db --division college-women-d3
```

It writes the SAME schema but NOT interchangeable keys: the mirror's team id is
season-scoped where `event_teams.event_team_id` is per event, and its game id
is a content hash rather than USAU's number. So team keys are synthesized in a
`gq:` namespace and an event is sourced from exactly one provider — the ingest
replaces an event's rows wholesale rather than overlaying them. Hence the
separate `$USAU_GQL_DB` file.

Validated against the HTML scrape on all six divisions it covers: across 3,162
shared events only 27 differ on played games, and most of those are the mirror
being FRESHER on the live 2026 club season. The mirror's one loss mode is a
played game it can score but attribute to only one team — 110 games, 0.12% of
the shared corpus, dropped rather than half-attributed. 44 of those sit at one
event (2017 Mid-Atlantic Men's Regionals) whose division roll it files with 2
teams instead of 16.

Against that it fixes pool labels the HTML parse gets wrong (30 pool games
spread over Pools A-E, not lumped into two), it reaches back to 2014, and it
recovers `Denver-Round-Robin-2019` — the event that resets the connection on
every request from every exit, so the HTML path can never fetch it, and which
the mirror serves complete (10 games, 92 roster lines). Roster names differ on
0.2-1.2% of lines because the mirror serves a player's current USAU name where
the HTML froze it at scrape time.

Two bugs the full run exposed, both fixed and both worth not reintroducing: a
division must be matched on LEVEL AND DIVISION, since one event files the same
division name at several levels (the U.S. Open carries Mixed/Club beside
Mixed/Youth Club U-20) and matching the name alone will rate youth or masters
squads on the open scale; and team rows must be seeded from the DIVISION's
membership rather than the event-level team connection, which is sometimes
empty even when the division roll is full — that dropped all 34 played games
at the 2017 Carolina D-I CC.

**Retuned for this corpus.** `descent.py` was re-run over the expanded corpus
— FIT 2014-2021, VAL 2022-23, TEST 2024-25, scoring the n-weighted VAL across
all fifteen contested divisions rather than the five the constant used to
name. Seven moves survived the prune:

| knob | was | now |
|---|---:|---:|
| `roster_shrink` | 0.015 | 0.025 |
| `involvement_credit` | False | True |
| `stat_transfer_beta` | 8.0 | 12.0 |
| `stat_transfer_clamp` | 60 (implicit) | 90 |
| `division_scale` club women's | 200 | 160 |
| `division_bases` college women's | 1350 | 1250 |
| `division_bases` college women's D-III | 1350 | 1200 |

Weighted VAL 0.44808 -> 0.44637, weighted TEST 0.44706 -> 0.44578, and all
seven established divisions improve on TEST. `stat_transfer_clamp` had never
been in `PUBLISHED` at all, so it was silently taking EloConfig's 60; it is
spelled out now.

Two things the sweep settled rather than improved. **Club men's TEST is
0.45910 against the 0.45193 published for the old corpus, and retuning moved
it 0.00005** — so that gap was never a stale hyperparameter. It is a different
eval: the mirror carries 231 club-men events the HTML scrape never had, so the
2024-25 games being scored are not the same games. Compare the weighted
figures, not that pair.

**The masters priors stay by analogy, deliberately.** `descent.py` carries
scale and base axes for the four brackets with enough VAL games to search
(masters men's 235, masters mixed 292, grand masters men's 214, great grand
masters men's 169). Every one found a large gain in isolation — great grand
masters men's base +0.01104, grand masters men's +0.00907 — and every one
pruned back out at 0.00001-0.00011 once the global moves were in. A gain that
evaporates when the rest of the model catches up was measuring noise at that
n, not a division constant. The other four brackets are not in the grid at
all; two of them have zero FIT games, having first been contested in 2022.

Masters TEST does degrade slightly under the retune (masters men's 0.39336 ->
0.39596, grand masters women's 0.29481 -> 0.30704) because the global knobs
are n-weighted and masters is 4.5% of VAL. That is the intended trade and it
is worth naming rather than burying.

**Division-specific update speed was tested rather than assumed.** A grouped
K-factor descent gives college, masters, school, beach, and UFA their own
candidate multiplier while leaving club at the 1.0 gauge. Thin brackets share
a competition-family axis: at the time of this sweep, beach had only 377 VAL
games across its contested brackets and UFA had 316. The replay and objective
included all 901 UFA games then available; without that merge, a UFA K-factor
would have been a dead knob.

The proposed lower weights did not survive. Beach's global VAL optimum remains
1.0. UFA improves monotonically toward 1.5, not below 1.0, but that move
prunes as noise. College, masters, and school also remain 1.0. YCC alone keeps
a 1.25 multiplier: weighted VAL 0.45944 -> 0.45925 and untouched TEST
0.46446 -> 0.46425. This is update speed, not an assertion that a YCC game is
more important than a club game.

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
searchable player table over all seven divisions, a current UFA player-stats
view toggled from that same table, a **Trends** tab drawing one stepped line
per player or club with one point at every tournament/rating event, and a
**Tournaments** browser over all 3,863 events. Club rankings can switch
from the ranked list to a 25-club head-to-head results matrix; green cells are
winning records, red cells are losing records, and stronger color marks a more
decisive win-loss split. The **UFA** division includes the available AUDL/UFA
API history from the league's 2012 inaugural season onward. Its season table
reports each team's Elo after its latest game in that season; the team detail
chart has one point per UFA game, and each point expands to the score and Elo
change behind it. Season rosters and current linked player ratings remain
available in the same detail panel.
The site reads the published rating and history artifacts plus the UFA roster
database and identity links; it never replays the model, so the page cannot
drift from the generated ratings.

Every tab now spans the same seven divisions, and every tab defaults to all of
them. **Clubs** used to show one division at a time, ranked within it, on the
grounds that a merged 1..n invites a comparison the games cannot settle. It
now offers "All divisions" like the others: the seven share one rating scale,
bridged through mixed, so one list is arithmetically sound even where it is
not a prediction — club men's and college teams never play, so #4 above #5
across that line settles nothing. The number shown is the club's position in
whatever is selected, with its rank inside its own division on the tooltip,
exactly as the player table works. College's three roster bases carry less
information than a club division's, because a college squad is registered for
the season more often than for the event: in 2025, 57% of D-III clubs and 18%
of college ones filed an identical roster at every event they entered, against
1-4% across the club divisions. Less discriminating, not meaningless — the
other 43% and 82% do vary. **Players**
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
selects event points rather than whole subjects: narrowing the division keeps
only rating events in that division. Every tournament remains a distinct point
even when its date or display name matches another division; professional
fixtures are intentionally one-game rating events.

**Gender-matching** selects whole subjects everywhere, since nobody changes
group between events; it is disabled for clubs, which have no group. Neither
filtered gender view contains the 3,124 unplaced identities, so the two never
sum to the unfiltered count.

### Tournaments

USAU publishes a flat fixture list per event and nothing about its shape, so
the shape is **recovered** rather than read. The `stage` column is free text
typed by thousands of organisers and runs to 4,139 distinct values, including
`Chumpionship 9`, `GAME TO GO TO THE GAME TO GO` and `Round Name`. Two
different strategies, in `analysis/tournaments.py`:

**Pools are found structurally**, as sets of teams that have all played each
other — cliques in the co-play graph — because the labels carry no
information: of the 2,546 events where two or more pools were recovered, 1,447
file them under a single pool heading, so the label cannot tell one from
another. Cliques consume EDGES rather than teams, so a placement round
robin reusing teams from the opening pools is still found, and a rematch
counts for the later pool instead of twice in the first pool's standings. The
clique chosen through a fixture is the one spanning the fewest CALENDAR DAYS,
size breaking the tie: structure alone cannot tell the U.S. Open's opening
pool of three from the 9-12 pool of four that reuses two of its teams — both
are cliques and the wrong one is bigger.

**Brackets are READ where they are published, and recovered where they are
not.** The GraphQL mirror serves each bracket's own name, its `placeStart` and a
`nextGameId` chain, which is the one thing no label can carry: organisers name
every bracket's decider "Finals", so Texas 2 Finger 2024 holds six games
reading exactly that and the label path crowned Clutch, who won the NINTH-place
bracket, over Alamode, who won the event. `scraper/structure.py` attaches that
structure to games already in the DB — joined on the unordered team pair and
score, since `games.slot` postdates most of the corpus — and
`analysis/tournaments.py` prefers it. 3,041 of the 3,863 events with completed
games (79%) now have it.

Two things are still inferred, because the mirror gets them wrong. A published
heading can hold several independent knockouts under ONE `placeStart`: the 2026
Lehigh Valley Invite files 24 men's games as "Championship" at place 1, and
they are four separate trees — the title bracket, a 9-16 "Ninals", a 17-24
"Seventeenals", and a four-team flight. They are split by following
`nextGameId` to its root, and each tree's real position is read off its own root
label. And a heading with NO wiring at all is ignored rather than trusted:
splitting one of those by tree makes every game its own final, which at Flat
Tail 2017 turned one bracket into six championships and lost Oregon, who won
the game the schedule calls "Champ".

Where nothing is published, round rank still comes from the organiser's label
(`Pre-Quarters`, `Sweet 16`, `Gold Semi Finals`, `9th Place Quarters`) and only
the feeders are inferred, a slot reading back to the game that team won.
Deriving rounds from the results too — what this did first — turns a win-chain
through mislabelled pool play into a nine-round bracket that never existed;
showing no bracket is better than inventing one. Every rank-0 game is a root,
so a college invite running two flights off one schedule yields two trees
rather than one tree and a pile of orphans, and the page numbers them
`flight 1`/`flight 2` rather than repeating a heading. Anything no bracket
claims lands under **Placement & other games** with the organiser's own
wording. Across the corpus this puts 88.6% of the 115,173 completed games into
a pool or a bracket and loses none: every game is displayed somewhere.

Reading the published shape moved 1,513 games out of the loose pile, gave 85
events a champion they did not have, and corrected 84 that were wrong —
including Truck Stop at the 2022 Mid-Atlantic Regional and Revolver at the
2022 Southwest, both of which the labels had misfiled. Two events lost a
champion and both deserved to: Eastern's Qualifier 2017 was crowning Kansas,
who won its 17th-place bracket.

**A fixture in no pool and no bracket keeps its published name.** USAU's
`stage` for one of those is routinely the pool heading it was played under: the
2026 U.S. Open's two quarterfinal-seeding games both read "Pool D", which files
a seeding round among the pool games and hides it in the catch-all table. The
mirror calls them "Seeding Crossovers", and that name is stored as `stage_pub`
and preferred wherever it exists — 6,361 games, most often `Crossover` (415),
`Round Robin` (306), `Pool Play Completion` (250) and `Consolation` (178).

**Labels also fence the structural recovery.** Most events name no pool that
distinguishes anything — 1,447 of the 2,546 multi-pool events, above — so
when nothing matches "pool" the cliques are sought across the whole schedule.
That fallback used to run over the labelled games too, and on structure alone
a clique through the Final's edge is indistinguishable from a round robin, so
it sometimes ate one and the bracket lost its final. Centex 2023 lost
Colorado's universe-point title that way and the Northwest mixed Regional lost
BFG's. Excluding anything the label already places recovered 11 finals that
were never found, moved 355 games out of phantom pools into the brackets they
belonged to (84.6% -> 87.3% placed, measured on the five-division corpus that
change was made against), changed no champion that was already right, and lost
none.

Cliques that tie on days and size then settle on their team names. That buys
nothing but reproducibility, and it is not optional: cliques come out of a
Bron-Kerbosch over sets of team-name strings, so an unbroken tie is resolved
by string hash order and the recovered shape changes between runs of the same
build. Five events used to flip their champion between builds.

A **series** is the printed name with everything that varies between instances
taken out — the year, the edition number (`Cooler Classic 30`), the division
wording, the tour brand (`USAU`, `TCT`), and whatever suffix that season used
(`- ICC`, `(ICC)`). 3,863 events collapse to 771 series, so opening the 2025
U.S. Open also shows the other 23 instances and who won each. Division wording
goes because division is its own facet: a Sectional's men's and women's halves
are one tournament run twice. "Open" is deliberately kept — it is load-bearing
in "U.S. Open".

`TCT` goes for the same reason `USAU` does, and it earns its place: USAU dropped
the prefix in 2025, which split the Pro-Elite Challenge East and West into
`tct pro elite challenge west` for 2023-24 and `pro elite challenge west` for
2025-26, so a 2026 page showed nothing before 2025. Stripping it merges exactly
six series and over-merges none — the other five are the Select Flight Invite,
its East, the Elite-Select Challenge and the Pro Championships. The single
pre-2023 Pro-Elite Challenge stays its own series, correctly: one tournament
that later became two regional ones is not either of them.

A series spanning divisions gets a **picker** in the tournament view, opening on
the division being looked at, because three divisions on one weekend are three
separate histories. Standings break ties on head-to-head inside the tied group,
then point differential; every game here has a score, so rating never enters.

Club identity is the normalized name, so "Rhino" and "Rhino Slam!" are one
club and a college program's D-I and D-III sides are one program. Mixed and
women's keys carry a suffix on top of that: 337 club names (281 active in 2026)
exist in more than one gender division, and men's Phoenix and women's Phoenix
are two teams. The men's group keeps the bare key, so every pre-existing
identity is byte-identical to before.

Trend eligibility remains a union across season-end top-25 lists rather than a
fixed 25. That keeps a club that owned 2019 and has since folded beside the
current leaders without allowing a small tournament to qualify its entire
field. Eligibility is the only season-grain part: each eligible line retains
every tournament/rating-event update. The chart uses an ordinal source-event
axis, so two divisions or source events on the same date stay distinct. A
stepped segment remains flat between events and changes at the event point;
professional fixtures are intentionally one-game events.

At that density no stroke can identify a line on its own, so the interaction
carries it. Hovering a line or legend row isolates it; hovering a point names
its date, source event, division, and post-event rating. The legend opens in
latest-event rating order for the selected scope. "Above event median" compares
each point with the median post-event rating of every rated participant in that
same source event.

The division filter selects **events, not teams**. Club identities can play in
more than one division, so classifying a whole identity would misfile every one
of them. Narrowing the picker removes event points outside that division and
recomputes both the participant medians and season-end eligibility over the
remaining population.

### Field strength

The series tier above says what an event is FOR — Conference, Sectionals,
Regionals, Nationals. It says nothing about who was in it, and the two come
apart constantly: the Northeast men's Regional is a harder tournament than
most Sectionals will ever be, and Florida Warm Up is harder than the D-I
championship it feeds. So events carry a second, independent grade in
`analysis/field_strength.py`: **S / A / B / C / D**, from the attendees.

**The score is the average Elo in the room.** Each club is counted at the
rating it carried INTO the event — its last rated result strictly before the
start date — so nothing an attendee did there, or after it, can move the
grade, and the number is exactly what was knowable walking in. A club with no
rated result yet has shown nothing and is left out of the average rather than
guessed at. Ratings carry across divisions and seasons, one number per club,
so "current" means its last result anywhere.

This replaced a port of Smash Bros' Tournament Tier System, which scored an
event by how many of the division's top-ranked clubs attended, in steeply
decaying rank bands after PGRU's 224-for-a-top-5, 64-for-a-top-50 shape. It
read well at the top and lied everywhere else, because a band scheme has to
end somewhere: everything outside the division's top fifth scored **zero**, so
a club ranked 43rd counted exactly as much as one ranked 211th. Select Flight
Invite West 2026 drew Dark Star (20th), SOUF (32nd) and Wavestorms (40th) and
then nine clubs between 43rd and 111th, and came out **D** — a tier whose own
label read "no ranked clubs present". Corpus-wide, 1,088 of 1,959 D events had
ranked clubs in them. A mean has no cliff in it, every attendee moves the
number by what they are worth, and the number is a rating, which is a thing
people already know how to read.

What a mean does have is a blind spot for SIZE: two elite clubs playing a
showcase average higher than a sixteen-team Nationals, and a raw mean duly
made a 2-team exhibition championship-grade. So the average is taken against a
**prior of half a Nationals field** of merely typical clubs. A 16-team event
moves by a few Elo; a 2-team one is dragged most of the way back to ordinary,
because two results are not evidence of a tournament. At `PRIOR = 6` that
removes every small-field S while leaving every national championship in S: 73
of the 81 events the series tier files as Nationals-or-major grade S, and the
eight that do not are U.S. Opens — an invitational, graded on the field that
actually turned up — plus one event whose name merely contains "Nationals".
It is the only correction applied, and it is one line.

**The bars are per division, pinned at both ends to that division's own
record.** Ratings are one scale across all seven, so the averages are directly
comparable — but the divisions are not equally deep, and a bar that is right
for club men's is wrong for D-III. S is the weakest national championship the
division has ever held, C is its median event, and A and B split the gap
evenly.

| division | S | A | B | C |
|---|---|---|---|---|
| club men's | ≥ 1808 | ≥ 1729 | ≥ 1649 | ≥ 1570 |
| college men's | ≥ 1664 | ≥ 1562 | ≥ 1460 | ≥ 1358 |
| college men's D-III | ≥ 1527 | ≥ 1468 | ≥ 1408 | ≥ 1349 |
| club mixed | ≥ 1821 | ≥ 1742 | ≥ 1662 | ≥ 1583 |
| club women's | ≥ 1869 | ≥ 1792 | ≥ 1715 | ≥ 1638 |
| college women's | ≥ 1712 | ≥ 1614 | ≥ 1517 | ≥ 1419 |
| college women's D-III | ≥ 1528 | ≥ 1486 | ≥ 1444 | ≥ 1402 |

So an S says "the average team here was as strong as the average team at this
division's Nationals" and a D says "a below-average field for this division" —
the same claims everywhere, at different numbers. Every national championship
is an S by construction and stays one: the anchor is recomputed from the data,
so a thinner championship in some future season becomes the new floor rather
than dropping out of the tier.

| tier | n | what lands there |
|---|---|---|
| S | 117 | every national championship, plus the elite regular season — U.S. Opens, Pro Championships, Pro-Elite Challenge East, Florida Warm Up, Easterns |
| A | 143 | the rest of the Triple Crown Tour and the better invites |
| B | 421 | strong Regionals and the deeper Select Flight fields |
| C | 1,208 | an ordinary field for the division |
| D | 1,866 | below the division's median event |

For club men's, the ranges: Pro-Elite Challenge 1644-1918, U.S. Open
1673-1978, Select Flight 1602-1743, Northeast Regionals 1479-1791, Sectionals
1475-1668. 48 events carry no grade at all and show a dash rather than a D:
not one club in the field had a rating yet, which is the first weekends of the
corpus and the novelty 4v4 and goalty brackets the model never rated.

There are no ranks left to tie: the mean does not care about ordering, so the
per-event standings and their tie-break rule are gone with the bands.

Attendance is the set of clubs the model scored games for, so a team that
registered and never played is not counted.

**The tracker takes played games from USAU, not from you.** Every fixture with
a final score arrives with it, renders the score where an unplayed game renders
the model's probability, and cannot be clicked away; the odds are conditioned
on it. What you can still enter is a call on a game not yet played, kept in
`localStorage` under the fixture's USAU game number.

The field's ratings prefer the **upcoming** roster table, which rates a club
off what it registered for this event rather than off whatever it last played
— worth about 180 Elo for a club that fielded a B-squad last time out. But
that table empties the moment the event finishes, and a club with nothing
registered leaves it: rebuilding the day after the 2026 U.S. Open rated all
twelve entrants `None`, which quietly reduced the pool lettering to arbitrary
and the simulation to noise. It now falls back to the completed roster and
then the best one, so the preference is unchanged and the tracker no longer
depends on WHEN the page was built.

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
and — for a player — **which club he turned out for** at each event. A club's
event history also carries **what it did there**: its win-loss record for the
weekend, a mark where it won the thing, and the field-strength grade of the
tournament. Both ride in the resident core — the record folded into each
trajectory point as `[elo, rosterSize, wins, losses]`, the grade and champion
in an `eventMeta` map keyed by event — because counting wins from the games
tier would fault a season of games for every row drawn, which is the entire
thing the split exists to avoid. Self-fixtures are excluded from the record:
USAU sometimes files a club's A and B squads under one name, 76 of them at a
single event, and a club cannot beat itself. A club also gets
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

- **Trends** walked all 39,325 trajectories to find population medians and the
  season-end top-25 eligibility cut. Both are statistics over the whole
  population, so no lazy subset can compute them. The resident answers now
  retain every tournament/rating-event change for eligible lines, plus an
  ordinal event axis and one participant median per event.
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
tournament files would have been 3,810 of them at ~170 bytes gzipped, where
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
  (`build_db.py`), and the WAF-free GraphQL mirror ingest plus its
  HTML-comparison harness (`graphql.py` — see Alternate source above), and the
  published-bracket backfill plus its champion audit (`structure.py`), and the
  top-up for events that ended mid-scrape (`refresh.py`).
- `ufa/` — UFA (watchufa.com) integration: cached JSON API client (`api.py`),
  annual schedule and season scraper into `ufa_*` tables (`scrape.py` — teams,
  players, per-season stats, every scheduled game status, and per-game player
  lines for completed games), and name→identity linker with city/jersey/year
  corroboration (`link.py` → `data/ufa_links.csv` +
  `data/ufa_link_audit.csv` review queue).
- `womens_pro/` — lossless WUL/PUL statistics ingestion. `api.py` caches the
  public PUL JSON files; `scrape.py pul` follows every manifest endpoint, while
  `scrape.py wul` imports dashboard CSV exports. Both sources share indexed
  common fields and retain every source-specific metric as JSON.
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
`confirm` = bridge reviewed OK). A `merge` row may include a fourth `scope`
field containing `|`-separated divisions, or a fifth `clubs` field containing
`|`-separated canonical clubs, when only those records should be joined.
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
  Player box scores feed two mechanisms only after the source event is final:
  `involvement_credit` splits each game delta by a shrunk usage index
  (roster-mean preserved), and `stat_transfer_beta` transfers rating zero-sum
  between teammates by E± relative to that team-event mean. Complete USAU
  G/A/B/T events use the frozen yardage-trained proxy in
  `data/box_score_reference.json`; partial events inform involvement only and
  never manufacture a missing block or turnover as zero. UFA uses observed
  per-game yards and that game's scoring efficiency,
  `0.00663*yards + 0.223*(goals+assists) + GameSE*(blocks-turnovers)`, after
  the game prediction and result. Its rating input is scaled by `0.2` because
  UFA supplies roughly five game-level updates for each USAU event-level
  update. Published settings in `analysis/rankings.py` are
  `stat_transfer_beta=12` and `stat_transfer_clamp=90`.
  Final UFA game results also enter the authoritative replay as a third
  division through
  `load_ufa_games`; each game uses the linked players who recorded a point
  played in its per-game stats. Every scheduled API row remains in `ufa_games`;
  a Final 0–0 or forfeit-code score is retained there but is not treated as a
  played Elo result.
- `analysis/` — backtest (headline eval on club; slices for early-club and
  college), the `descent.py` selection harness, CSV exports, and `bridge_audit.py` — ranks bridge
  links by false-merge suspicion (height contradictions across every division
  the bridge spans, how far merging moved the rating against a replay with
  every cross-division link severed, geography); `--write-overrides`
  auto-blocks hard height conflicts.
  `player_roles.py` classifies each player-season as handler, hybrid, cutter,
  or unknown from roster position labels, UFA volume statistics, then USAU
  goals/assists/turns. Low-confidence evidence remains unknown; prior-season
  evidence decays, and `data/player_role_overrides.csv` supplies reviewed
  corrections. Roles are descriptive and do not affect Elo.
  `player_metrics.py` writes the UFA-linked scorecard to
  `data/player_metrics.csv`. Version `ufa-eb-v4` uses a stable game-style
  1–99 presentation: the UFA reference midpoint is 60 OVR, an average
  role-adjusted attribute is 70, and each model standard deviation adds ten
  points. THR combines huck rate and accuracy, yards per throw, and assists
  plus hockey assists per throw. POS separately combines completion rate,
  throwaway and stall avoidance, and catch security. Current-season evidence
  has full weight; each earlier season retains half the weight of the following
  season. Sparse current samples use that recency-weighted player history, then
  shrink toward the role average; historical evidence is capped at four times
  the fitted group-prior exposure. THR/POS/OFF/DEF use the frozen 2022–2025
  reference in `data/player_metric_reference.json`. Attribute reliability,
  history seasons, weighted prior throws, raw counts, source date, and missing
  values remain explicit. These presentation attributes do not feed Elo
  directly; the source per-game E± described above does.
  UFA publishes the scorecard's rich stats only as cumulative season rows, so
  `stats_through` uses that season's latest
  dated Final (September 1 only when game dates are absent); an `--as-of`
  cutoff before it excludes the whole row rather than leaking later games.
  The Players table's **Player stats** checkbox switches between the rating
  columns and the current OVR/THR/POS/OFF/DEF plus G/A/B/T scorecard;
  historical seasons disable the switch because no historical scorecard is
  published.
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
- Second source: `usau-rankings.fly.dev/api/graphql`, an unauthenticated
  third-party mirror of the same USAU data, no WAF and no rate limiting
  observed at 32-way concurrency. Introspection is enabled, so the schema is
  self-documenting. It carries no ToS, no robots.txt, no versioning and no
  named operator, so it is treated as a fast path rather than the source of
  record — `scraper/graphql.py --validate` is what keeps that judgement
  evidence-based, and the HTML scraper stays the fallback.
- The mirror also answers questions the HTML never did: `franchiseId` gives a
  stable cross-season program key (though it fragments across a gap in
  seasons, 24% of college-women names), and per-team `currentRating` /
  `ratingHistory` resolve for College. Its `rankings` and `ratingMeta` list
  queries are Club-only — empty for College across every season and variant.
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
- Selection scores tier-weighted VAL logloss across all 47 modeled divisions.
  Club owns 50% of the objective, college 30%, school/youth 15%, and all
  other divisions (including beach, masters, and UFA) 5%; each tier's share
  is independent of its row count. Per-division bases and scales are still
  scored on their own division's games. `python -m analysis.descent` is the
  40-axis harness; `--division-k-only` runs the six competition-family update
  speeds alone. Three passes are followed by cumulative 0.0003 VAL pruning.
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
- Join `player_elo.csv` on `player_id`, never on `player`. USAU names that
  survive as separate identities are still split per club; European-only
  identities use deterministic negative IDs. Display names are not unique.
  Four USAU names also contain commas (`Gregory Plaia, Jr`), so parse the file
  as CSV — splitting on commas corrupts those rows.
- Rosters are public per event-team, names only (no stable player IDs).
  Nationals rosters include per-player stats (points/assists/Ds/turns) —
  unused by the model so far.


## European and international data source reference

European and world-championship data is independently ingested into
`data/euf.db`. `analysis.rankings` joins its games and rosters to the USAU
replay only through the conservative identity contracts described below.

| Surface | Contract used here | Coverage and boundary |
|---|---|---|
| [Ultimate Central API](https://help.ultimatecentral.com/support/solutions/articles/4000064797-api-access) | Public, paginated REST endpoints discovered from `/api/help`: events, teams, games, final standings, and public persons | Historical event metadata, scored games, teams, and final standings. Roster responses remain `incomplete` or `restricted` unless public rows are actually returned. |
| [EUCS Schedule](https://eucs-schedule.ultimatefederation.eu/) | Server-rendered season schedule plus iCalendar cross-check | Schedule rows, scores, pools/stages, divisions, dates, times, fields, and placeholder/forfeit state. There is no roster contract. HTML is cached with URL, observation time, and SHA-256. |
| [EUCS Ranking](https://ranking.ultimatefederation.eu/) | R/Shiny `dataobj/team_master_roster` responses captured through a browser session | Season/team roster names for 2024–2026. The `session/.../dataobj/...` URLs are transient and are retained only as observation evidence, not presented as a stable API. The source exposes names, not player IDs. |
| [WFDF Results](https://results.wfdf.sport/) | Official server-rendered results pages plus the BULA Live JSON API: event index, teams, standings, schedules/scores, and (where requested) public rosters | WFDF-only: all 17 completed tournaments available in the official 2022–2025 corpus plus the three WUCC 2026 divisions, covering 72 event/division records, 1,109 source-scoped teams, and 5,203 scored games. Legacy games without published gameplay links receive deterministic row-based IDs; live API games use WFDF game IDs. WFDF player IDs are scoped to one results installation, not globally stable. |

The observed `data/euf.db` audit covers Ultimate Central EUCF events from
2015, 2016, 2019, 2021, and 2022; all discoverable completed EUCS schedules
from 2023–2025; all 17 completed WFDF tournaments exposed for 2022–2025;
and WUCC 2026. The remaining 2026 WFDF and EUCS events are excluded while
current or upcoming. Years and event classes not listed above are coverage
gaps, not zero-game seasons.

WFDF-only, the tournaments contribute 72 event/division records, 1,109
source-scoped teams, and 5,203 games with complete scores. The championships
previously loaded with public rosters retain 9,602 roster memberships. The
remaining newly added tournaments publish teams and games but no roster rows
were requested during the historical load; their roster state is explicitly
`unavailable`, not inferred empty. Normalized divisions include club
men's/women's/mixed plus masters, grand masters, and great-grandmasters
divisions. Legacy games with no gameplay link use a stable source-row
identifier.


The captured EUCS Ranking snapshot adds 384 season/team rosters, 11,274 roster
memberships, and 6,804 distinct display names across 2024–2026. Eleven
successful responses contained zero rows. These are season-level ranking
rosters, not event rosters. Ultimate Central roster coverage remains nullable:
its person collections report a larger source count than the public result
set, so all 15 Ultimate Central event/divisions are `incomplete`; the 32
schedule-only event/divisions are `unavailable`. No private identities are
inferred.

Every imported event, team, game, standing, public person, and ranking roster
observation retains its provider namespace, provider key, request URL,
observation time, and payload hash in `source_entities` or
`source_observations`. Provider IDs remain text: EUCS season and synthetic slot
IDs are not coerced to integers. Team-name similarity creates review
candidates only; it never merges team identities.

The rating replay uses 908 scored 2024–2025 EUCS games whose teams map to a
captured ranking roster and 5,194 eligible WFDF games. European-only roster
names receive deterministic negative IDs within JavaScript's safe-integer range.
A European name reuses a positive USAU player ID only when its accent-,
punctuation-, and spacing-insensitive key maps to one non-ambiguous USAU
identity, appears in both corpora during the same calendar season, and does
not occur on two European teams in the same season/division. This produces
208 EU/USA bridges, recorded with their match method in
`data/euf_bridge_audit.csv`.

WFDF player IDs are event-installation scoped, so they are retained as source
provenance but are not treated as global identities. The replay instead uses
the same conservative same-season name rule against USAU and EUF rosters,
rejects names duplicated across two teams at one championship, and assigns a
deterministic international ID otherwise. The current build makes 4,334
WFDF-to-USAU/EUF name bridges; all 11,544 roster-name decisions are written
to `data/wfdf_bridge_audit.csv`.
Reviewed cross-name assignments live in `data/wfdf_player_aliases.csv`. Each
row maps one WFDF source name to one unique, non-ambiguous USAU display name;
an event-level duplicate still blocks the assignment.

The European division bases and scales are explicit, untuned priors copied
from their analogous USAU club divisions. WFDF club divisions use those same
club dimensions; masters divisions use the existing masters dimensions.
Same-player bridges propagate observed ratings between corpora, but do not
prove that the continent-wide scale is calibrated. Six EUF scored games touch
an unavailable roster and use the engine's existing ghost-team behavior.
Current European team tables use the captured 2026 season roster in the
completed and best views; European teams are omitted from the next-event view
because EUCS does not expose event-specific upcoming rosters.

`python -m scraper.euf --audit` blocks publication for duplicate source
mappings, invalid played games, blocking source disagreements, or provenance
gaps. `python -m analysis.euf_overlap` reproduces the identity-candidate counts
without treating any name match as authoritative.

## How to build and query the European and international corpus

Install the declared runtime:

```sh
uv pip install --python .venv/bin/python -r requirements.txt
```

Probe the two live contracts without writing the database:

```sh
.venv/bin/python -m scraper.euf --probe ultimate-central --request-budget 10
.venv/bin/python -m scraper.euf --probe eucs --season eucf24
```

Create the isolated database, ingest one schedule or one Ultimate Central
event, load a captured ranking-roster snapshot, and ingest the official WFDF
championship corpus:

```sh
.venv/bin/python -m scraper.euf --init-db
.venv/bin/python -m scraper.euf 2024 --event eucf24
.venv/bin/python -m scraper.euf --event uc:116389 --request-budget 40
.venv/bin/python -m scraper.euf_ranking data/raw/euf-ranking/rosters.json
.venv/bin/python -m scraper.wfdf all --workers 12
.venv/bin/python -m scraper.euf --audit
.venv/bin/python -m analysis.euf_overlap
```

For a games-and-tournaments-only historical load, add `--skip-rosters` to the
WFDF command; roster availability remains explicitly `unavailable` for those
replacements.

Rerunning an event transactionally replaces that event; it does not append
duplicates. A failed replacement rolls back. Add `--refresh` to an EUCS or
WFDF command to bypass its content-addressed HTML cache.

Serve the read-only GraphQL facade:

```sh
EUF_DB=data/euf.db .venv/bin/uvicorn api.euf_graphql:app \
  --host 127.0.0.1 --port 8766
```

Query `http://127.0.0.1:8766/graphql`:

```sh
curl -sS http://127.0.0.1:8766/graphql \
  -H 'content-type: application/json' \
  --data '{"query":"{ events(eventCode:\"eucf24\", first:3) { totalCount nodes { id divisions rosterState games(first:200, played:true) { totalCount } sources { source sourceId } } } }"}'
```

Top-level `events`, `teams`, and `games` accept source, event-code, season,
division, team, and relevant played-state filters. Connections expose
`nodes`, `totalCount`, and offset page information. `first` must be 1–200.
Teams imported from `eucs-ranking` expose their observed roster names through
`players`; a successful zero-row roster returns `[]`. `eventRosterEntries`
returns each public event roster with event code, season, division, team,
number, goals (`points`), assists, Ds, and turns. For example,
`teams(source:"wfdf-results", eventCode:"wucc-2022:club-men",
name:"Gentle")` exposes the official WUCC roster and player-card statistics.
Unknown IDs and roster fields with no observed roster source return `null`.
The schema has no mutation root and opens SQLite with `mode=ro` plus
`PRAGMA query_only=ON`. The service never calls an upstream endpoint from a
resolver.

The overlap command reports candidate counts under both exact case/spacing and
diacritic-folded keys. The replay uses the stricter operational contract above
and records its 208 accepted people, source-name variants, and match method in
`euf_bridge_audit.csv`. Because EUCS Ranking publishes no stable player ID, the
comparable stable-ID match count remains zero by construction. The audit file
is the review boundary for every cross-source identity used by Elo.
