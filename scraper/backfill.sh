#!/usr/bin/env bash
# Resilient USAU backfill: fast pace, automatic Mullvad exit rotation on block.
#
# The WAF blocks per-IP on a REQUEST BUDGET that is largely independent of pace
# (data/scrape_2026-07-19.log: first failure at live request #1974). Pacing
# slowly therefore buys nothing — it spends the same budget over more hours. So
# run fast and rotate the exit when the budget runs out, which is what makes a
# ~18k-request backfill a few hours instead of ten.
#
# build_db exits 3 on SiteBlocked with everything committed, and resumes from
# the shared HTML cache, so a rotate-and-retry loses only the event in flight.
#
# Usage: scraper/backfill.sh <division> <db-path> <season>...
#   scraper/backfill.sh club-mixed data/usau_mixed.db 2017 2018 2019

set -uo pipefail
cd "$(dirname "$0")/.."

DIVISION="${1:?usage: backfill.sh <division> <db> <season>...}"
DB="${2:?usage: backfill.sh <division> <db> <season>...}"
shift 2
SEASONS=("$@")
[ ${#SEASONS[@]} -gt 0 ] || { echo "no seasons given" >&2; exit 2; }

# Every US Mullvad WireGuard city. One city == one fresh budget; the list is
# walked in order and wraps, so a long run reuses a city only after 20 others.
CITIES=(qas atl bos chi dal den det hou mkc lax txc mia nyc phx rag slc sfo sjc sea uyk was)
CITY_IX=0
# Cap total rotations so a genuine outage (site down, VPN broken) cannot spin
# forever burning exits: 21 cities x 2 passes is far more budget than the job needs.
MAX_ROTATIONS=42
rotations=0

# Pace. Aggressive on purpose — see the budget note above.
export USAU_RATE_LIMIT="${USAU_RATE_LIMIT:-0.5}"
export USAU_JITTER="${USAU_JITTER:-0.4}"
# Short ladder: with rotation automated, confirming a block quickly and moving
# to a new exit beats sleeping through an escalating probe schedule.
export USAU_BLOCK_PROBES="${USAU_BLOCK_PROBES:-5,10}"
export USAU_DB="$DB"

myip() { curl -s --max-time 20 https://am.i.mullvad.net/ip; }

rotate() {
  local before after city tries
  before="$(myip)"
  for tries in 1 2 3 4 5 6; do
    city="${CITIES[$((CITY_IX % ${#CITIES[@]}))]}"
    CITY_IX=$((CITY_IX + 1))
    echo ">> rotating exit -> us/$city (was ${before:-unknown})"
    mullvad relay set location us "$city" >/dev/null 2>&1
    mullvad reconnect >/dev/null 2>&1
    # Wait for the tunnel to come back before trusting any request.
    for _ in $(seq 1 30); do
      sleep 2
      [ "$(mullvad status 2>/dev/null | head -1)" = "Connected" ] && break
    done
    sleep 3
    after="$(myip)"
    if [ -n "$after" ] && [ "$after" != "$before" ]; then
      echo ">> exit is now $after"
      return 0
    fi
    echo ">> exit did not change (got '${after:-none}'), trying next city"
  done
  echo ">> ROTATION FAILED — no usable exit after 6 attempts"
  return 1
}

echo "== backfill $DIVISION -> $DB, seasons ${SEASONS[*]}"
echo "== pace ${USAU_RATE_LIMIT}s + 0..${USAU_JITTER}s jitter, probes ${USAU_BLOCK_PROBES}"
echo "== starting exit $(myip)"

for season in "${SEASONS[@]}"; do
  while :; do
    .venv/bin/python -m scraper.build_db --division "$DIVISION" "$season"
    rc=$?
    if [ $rc -eq 0 ]; then
      echo "== season $season done"
      break
    fi
    if [ $rc -ne 3 ]; then
      # Not a block: a real crash. Stop rather than loop on a broken build.
      echo "!! season $season exited $rc (not a block) — stopping"
      exit $rc
    fi
    rotations=$((rotations + 1))
    if [ $rotations -gt $MAX_ROTATIONS ]; then
      echo "!! hit MAX_ROTATIONS ($MAX_ROTATIONS) — stopping; re-run to resume"
      exit 4
    fi
    echo "== BLOCKED in season $season (rotation $rotations/$MAX_ROTATIONS) — resuming after switch"
    rotate || exit 5
  done
done

echo "== ALL DONE: $DIVISION seasons ${SEASONS[*]}"
