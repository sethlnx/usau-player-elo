# European Ultimate data feature TODO

## Goal

Create a provenance-preserving European Ultimate data pipeline with ingestion parity to `scraper/graphql.py` and an optional read-only GraphQL facade over normalized SQLite data.

The first production corpus lives in `data/euf.db`. Do not merge USAU and EUF ratings until a separate bridge audit establishes enough shared-player connectivity to put them on one scale.

## Research already completed

- [x] Confirmed that Ultimate Central exposes a public REST API and a live machine-readable endpoint catalog at `https://euf.ultimatecentral.com/api/help`.
- [x] Confirmed public resources for events, games, teams, final standings, and persons, with incomplete or restricted historical roster/game coverage.
- [x] Confirmed that EUCS Schedule exposes server-rendered historical schedules, scores, stages, pools, brackets, game IDs, iCalendar, and RSS-related feeds.
- [x] Confirmed that EUCS Ranking is an R Shiny application whose transient `/session/.../dataobj/...` URLs are not a stable API contract.
- [x] Confirmed that the EUF ranking algorithm has a public Python implementation at `Berenito/Ranking`.
- [x] Mapped the repository: `scraper/graphql.py` is the closest source-adapter precedent; `scraper/build_db.py` owns the normalized SQLite schema; the repository does not currently host a GraphQL server.
- [x] No implementation files were changed during research.

## Source authority and boundaries

Use this precedence for the initial implementation:

1. **EUCS Schedule HTML:** scored games, pool/bracket/stage labels, fields, and schedule structure.
2. **Ultimate Central REST:** event metadata, stable provider team IDs, divisions, and final standings.
3. **Ultimate Central public HTML:** fields absent from REST, especially publicly displayed historical rosters. Cache and crawl conservatively.
4. **EUCS Schedule iCalendar:** schedule cross-check only; do not expect historical scores.
5. **EUCS Ranking:** validation reference only. Never ingest transient R Shiny session endpoints.

Mandatory invariants:

- Preserve provider, tenant, source ID, source URL, observation time, and payload hash.
- Treat HTTP 401/403 as unavailable data, not an empty roster.
- Never synthesize hidden players or silently merge teams by display name.
- Keep unplayed bracket placeholders distinct from played games.
- Resolvers query normalized SQLite only; they never scrape upstream services.
- Report source disagreements instead of choosing an unexplained winner.

Initial acceptance criteria:

- A clean database can ingest one historical EUCF season with nonzero events, teams, divisions, and scored games.
- Every completed game has two mapped teams and two numeric scores.
- Repeating an unchanged ingestion is idempotent.
- Every normalized upstream entity has an auditable source mapping.
- The local GraphQL facade returns the same event/game counts as SQLite.
- An audit states exact source coverage and blocks publication on unresolved structural conflicts.

## Phase 1 — Normalize source identity

### Schema and migration

- [x] Extend `scraper/build_db.py:SCHEMA` with `source_entities` containing at least:
  - `source` — provider and tenant namespace, for example `ultimate-central:euf` or `eucs-schedule`.
  - `entity_type` — `event`, `team`, `game`, `person`, `standing`, or `division`.
  - `source_id` — text, never coerced to an integer.
  - `local_key` — the corresponding normalized row key encoded as text.
  - `source_url`.
  - `observed_at`.
  - `payload_hash`.
  - Primary key on `(source, entity_type, source_id)` and uniqueness preventing one source entity from mapping to multiple local entities.
- [x] Update `scraper/build_db.py:_ensure_columns` so existing databases migrate idempotently.
- [x] Add an `EUF_DB` configuration path defaulting to `data/euf.db`; do not write European provider keys into `data/usau.db`.
- [x] Keep the existing normalized `events`, `event_teams`, `games`, and `roster_entries` contracts so downstream analysis can be reused.
- [x] Namespace Ultimate Central IDs by tenant. The same integer from another TopScore tenant is not the same entity.
- [x] Preserve EUCS season codes and game IDs as strings; do not infer a year from the code alone.

Verification:

```bash
.venv/bin/python -m scraper.euf --init-db --db data/euf_smoke.db
sqlite3 data/euf_smoke.db ".schema source_entities"
sqlite3 data/euf_smoke.db "PRAGMA foreign_key_check;"
```

Expected:

- `source_entities` exists with the declared uniqueness constraints.
- Running `--init-db` twice succeeds without schema drift.
- `PRAGMA foreign_key_check` returns no rows.

Risks and edge cases:

- Existing local keys are not uniformly typed; encode them canonically before storing `local_key`.
- An event may have records in both providers without a shared ID.
- Team names change with sponsors, season, language, and division.

## Phase 2 — Implement the Ultimate Central REST adapter

### Files and symbols

- [x] Create `scraper/ultimate_central.py`.
- [x] Implement `UltimateCentralClient` with an injected `requests.Session`, base URL, timeout, and request budget.
- [x] Implement:
  - `get_help()`
  - `list_events()`
  - `list_games()`
  - `list_teams()`
  - `final_standings()`
  - `list_public_persons()`
  - bounded pagination and transport helpers
- [x] Enforce `per_page <= 100`.
- [x] Retry transport failures and 5xx responses with bounded backoff. Do not retry permanent 4xx responses except an explicitly handled rate response.
- [x] Validate the response envelope: `status`, `count`, `result`, and `errors`.
- [x] Distinguish these observed states:
  - nonzero `count` with an empty `result`;
  - 401/403 restricted data;
  - zero records;
  - placeholder/unplayed game rows;
  - complete final standings with absent game history.
- [x] Preserve raw payload hashes and source URLs for auditability.

Probe:

```bash
.venv/bin/python -m scraper.euf --probe ultimate-central
```

Expected:

- `/api/help` returns status 200 and a positive endpoint count.
- `/api/events` returns a positive count.
- Probe output explicitly reports public roster availability instead of assuming it.
- No write endpoint is called.

Risks and edge cases:

- The public support article and live API catalog are authoritative enough for discovery, but do not guarantee completeness per event.
- Historical events may expose final standings but no scored games.
- Public person data may be intentionally hidden; preserve that boundary.
- Apply a conservative client-side request limit below the provider’s observed historical allowance.

## Phase 3 — Implement the EUCS Schedule adapter

### Files and symbols

- [x] Create `scraper/eucs_schedule.py`.
- [x] Implement:
  - `discover_seasons()`
  - `fetch_schedule()`
  - `parse_schedule()`
  - `fetch_ical()`
  - `EUCSFetchError`
  - `EUCSParseError`
- [x] Cache HTML before parsing and record the final URL and payload hash.
- [x] Parse:
  - season/event code;
  - division;
  - stage, pool, or bracket label;
  - date and local time;
  - field;
  - home and away teams;
  - home and away scores;
  - numeric/source game ID;
  - played, scheduled, placeholder, or forfeit state when the source supports it.
- [x] Keep score cells distinct from nearby spirit-score or standings fields.
- [x] Keep placeholder labels such as “Winner of …” unresolved until a real team is present.
- [x] Honor the source crawl policy. Use no concurrent HTML crawl and reuse cached responses.
- [x] Use iCalendar only to cross-check pairings, dates, times, and fields.

Probe:

```bash
.venv/bin/python -m scraper.euf --probe eucs --season eucf24
```

Expected:

- Positive played-game count.
- Mixed, Open, and Women divisions are detected for EUCF 2024.
- Completed games have numeric scores.
- Completed games have no unresolved placeholder teams.

Risks and edge cases:

- Event codes are not guaranteed to equal a four-digit year.
- Future fixtures contain unresolved bracket participants.
- Stage and field labels are organizer-entered free text.
- Time zones vary by event and must not be replaced with the workstation zone.
- HTML layout can change without an API version bump.

## Phase 4 — Orchestrate normalized EUF ingestion

### Files and symbols

- [x] Create `scraper/euf.py`.
- [x] Implement:
  - `main()`
  - `discover_events()`
  - `ingest_event()`
  - `upsert_event()`
  - `replace_event()`
  - `link_sources()`
  - `validate_event()`
- [x] Support:
  - `--db`
  - `--init-db`
  - `--probe`
  - `--event`
  - `--backfill`
  - `--audit`
  - explicit season arguments
- [x] Follow the clean-replace invariant from `scraper/graphql.py`: refresh one provider-owned event transactionally so stale games and rosters cannot survive a successful replacement.
- [x] Roll back the whole event when parsing, mapping, or validation fails.
- [x] Link cross-provider entities by stable source mapping first.
- [x] Allow normalized name/date/division matching only to produce auditable candidates; require an explicit accepted mapping before authoritative merge.
- [x] Preserve hidden/missing roster states and never synthesize names.
- [x] Store score conflicts from two sources as audit findings.

Smoke scenario:

```bash
rm -f data/euf_smoke.db
.venv/bin/python -m scraper.euf 2024 --event eucf24 --db data/euf_smoke.db
sqlite3 data/euf_smoke.db "SELECT count(*) FROM events;"
sqlite3 data/euf_smoke.db "SELECT count(*) FROM event_teams;"
sqlite3 data/euf_smoke.db "SELECT count(*) FROM games WHERE home_score IS NOT NULL AND away_score IS NOT NULL;"
.venv/bin/python -m scraper.euf 2024 --event eucf24 --db data/euf_smoke.db
.venv/bin/python -m scraper.euf --audit --db data/euf_smoke.db
```

Expected:

- Event, team, and scored-game counts are all positive.
- Every played game has two mapped teams and two numeric scores.
- A second unchanged run leaves row counts and payload hashes unchanged.
- No source ID maps to multiple local entities.
- Audit identifies all incomplete roster coverage instead of silently reporting zero.

Risks and edge cases:

- Same-name teams may belong to different countries or clubs.
- One club may field division-specific teams with similar names.
- A game can be corrected after the first scrape; event replacement must update it without duplicating it.
- Ultimate Central and EUCS Schedule may disagree on score, date, or team spelling.

## Phase 5 — Add the read-only GraphQL facade

This phase is gated on the ingestion smoke corpus passing. The current repository is static, so do not add a server stack before the normalized data path works.

### Files and symbols

- [x] Inventory current runtime imports, then add the smallest explicit dependency manifest required for the existing Python code plus a maintained GraphQL ASGI implementation.
- [x] Create `api/__init__.py`.
- [x] Create `api/euf_schema.py` with:
  - `Event`
  - `Team`
  - `Game`
  - `Standing`
  - `SourceRef`
  - paginated connection/page types
  - `Query.events`
  - `Query.event`
  - `Query.teams`
  - `Query.team`
  - `Query.games`
- [x] Create `api/euf_graphql.py` exposing `schema` and ASGI `app`.
- [x] Read from SQLite using bounded queries; prevent unbounded nested event/game expansion.
- [x] Support filters for source, event code, season, division, team, and played state where represented.
- [x] Return `null` for unavailable roster/person values. Do not turn missing source coverage into invented empty data.
- [x] Ensure resolvers perform no upstream HTTP requests.

Smoke scenario:

```bash
.venv/bin/uvicorn api.euf_graphql:app --host 127.0.0.1 --port 8001
```

POST these queries to `http://127.0.0.1:8001/graphql`:

```graphql
{ __schema { queryType { name } } }
```

```graphql
{
  events(eventCode: "eucf24", first: 1) {
    nodes {
      id
      name
      divisions
      sources { source sourceId sourceUrl }
      games(first: 200) {
        nodes { id homeScore awayScore played }
      }
    }
  }
}
```

Expected:

- Introspection returns query type `Query`.
- The event and played-game counts match direct SQLite queries.
- An unknown ID returns `null`, not a 500 response.
- Invalid pagination returns a GraphQL validation/domain error.
- Server logs show no upstream requests while executing queries.

Risks and edge cases:

- GraphQL nested selections can produce N+1 queries; batch or join source references and games.
- SQLite is safe for the intended read workload only if writes remain batch-oriented and transactions are short.
- A hosted service needs separate operational decisions for process supervision, database refresh, and public rate limits.

## Phase 6 — Verify with post-smoke contract tests

Add tests only after the feature and its smoke path work. Tests must defend observable behavior and must not replace the end-to-end smoke scenario.

### Test files

- [x] Create `tests/test_ultimate_central.py`.
- [x] Create `tests/test_eucs_schedule.py`.
- [x] Create `tests/test_euf_ingest.py`.
- [x] Create `tests/test_euf_graphql.py`.
- [x] Add sanitized, minimal fixtures under `tests/fixtures/euf/` only after capturing the working behavior.

Required contracts:

- Pagination stops correctly and enforces `per_page <= 100`.
- 401/403 roster responses differ from an empty successful response.
- Nonzero `count` with empty `result` is surfaced.
- Completed, scheduled, placeholder, corrected, and forfeited games are distinct.
- Spirit or standings values are not parsed as game scores.
- Event replacement is atomic and idempotent.
- One source entity cannot map to two normalized entities.
- Score conflicts remain visible in audit output.
- GraphQL pagination is bounded and unknown IDs resolve to `null`.
- Fixtures contain no private person fields, emails, or authentication tokens.

Commands:

```bash
.venv/bin/python -m unittest tests.test_ultimate_central
.venv/bin/python -m unittest tests.test_eucs_schedule
.venv/bin/python -m unittest tests.test_euf_ingest
.venv/bin/python -m unittest tests.test_euf_graphql
.venv/bin/python -m unittest discover -s tests
```

Expected: every module and the full discovery run pass.

Never alter a fixture or weaken an assertion merely to accommodate a parser regression. Fix the parser unless a separately verified upstream contract changed; then refresh only the affected sanitized fixture and record the changed contract in the test name or assertion.

## Phase 7 — Backfill and audit the corpus

- [x] Backfill Ultimate Central events only across the date/event ranges its public API or HTML actually covers.
- [x] Backfill EUCS Schedule only for discovered, verified season codes.
- [x] Do not imply continuous European coverage from gaps between the two systems.
- [x] Add audit counts for:
  - events by provider and year;
  - divisions;
  - teams;
  - scheduled and scored games;
  - public roster entries;
  - restricted/hidden roster states;
  - unresolved cross-provider mappings;
  - duplicate source mappings;
  - score/date/team disagreements;
  - played games missing either team or score.
- [x] Block publication when duplicate source mappings, half-attributed played games, or unexplained score conflicts remain.

Commands:

```bash
.venv/bin/python -m scraper.euf --backfill --db data/euf.db
.venv/bin/python -m scraper.euf --audit --db data/euf.db
```

Expected:

- The audit reports exact first/last dates and event counts for each source.
- Every gap is reported as unavailable, restricted, unresolved, or failed—never silently omitted.
- Blocking invariants return a nonzero process exit until resolved.

## Phase 8 — Integrate only after verification

- [x] Update `README.md` after the smoke scenario, contract tests, and corpus audit pass.
- [x] Document the exact ingest, refresh, audit, and GraphQL launch commands.
- [x] State coverage boundaries and roster/privacy limitations next to the commands.
- [x] Keep EUF and USAU data/rating outputs separate.
- [x] If cross-continent ratings are later requested, first add a bridge audit that measures shared stable players, connected components, seasons, and divisions; do not calibrate from team-name similarity.
- [x] Re-run the implementation smoke scenario and full test discovery after the README command examples are finalized; the documented commands must be the commands that passed.

## Definition of done

The feature is complete only when all of the following are true:

- `data/euf.db` can be rebuilt from an empty file using documented commands.
- A representative historical EUCF event has validated metadata, divisions, teams, scored games, standings, and provenance.
- Re-ingestion is atomic and idempotent.
- Missing/restricted roster data is explicit.
- Source disagreements and unresolved entity mappings appear in audit output.
- The read-only GraphQL facade returns results consistent with SQLite and performs no live scraping.
- Focused contract tests and full test discovery pass unchanged.
- The README reports exact observed coverage rather than inferred completeness.

# Add career plus minus
