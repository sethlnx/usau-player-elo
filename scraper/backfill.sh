#!/usr/bin/env bash
# Resilient USAU backfill: fast pace, automatic exit rotation on block.
#
# The WAF blocks per-IP on a REQUEST BUDGET that is largely independent of pace
# (data/scrape_2026-07-19.log: first failure at live request #1974). Pacing
# slowly therefore buys nothing — it spends the same budget over more hours. So
# run fast and rotate the exit when the budget runs out.
#
# Rotation uses Mullvad's per-server SOCKS5 proxies (USAU_PROXY -> fetch.py),
# NOT `mullvad reconnect`: the tunnel stays connected to one relay the whole
# time, so nothing else on the machine stalls, and every one of the ~200 US
# WireGuard servers is its own exit IP with its own WAF budget. The proxies
# (us-<city>-wg-socks5-NNN.relays.mullvad.net:1080) are only reachable while
# the tunnel is up.
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

# The tunnel must be up for the in-tunnel proxies to resolve at all.
if [ "$(mullvad status 2>/dev/null | head -1)" != "Connected" ]; then
  echo ">> tunnel down — connecting"
  mullvad connect >/dev/null 2>&1
  for _ in $(seq 1 30); do
    sleep 2
    [ "$(mullvad status 2>/dev/null | head -1)" = "Connected" ] && break
  done
fi

# Every US WireGuard server, one SOCKS proxy each. Shuffled so consecutive
# rotations land in different cities/subnets instead of walking one rack.
PROXY_HOSTS=()
while IFS= read -r host; do
  PROXY_HOSTS+=("${host/-wg-/-wg-socks5-}.relays.mullvad.net")
done < <(mullvad relay list | grep -oE 'us-[a-z]+-wg-[0-9]+' \
         | awk 'BEGIN{srand()} {print rand() "\t" $0}' | sort -n | cut -f2)
[ ${#PROXY_HOSTS[@]} -gt 0 ] || { echo "no US relays found — is mullvad installed?" >&2; exit 2; }
PROXY_IX=0

# Cap total rotations so a genuine outage (site down, VPN broken) cannot spin
# forever: two full passes over every US server is far more budget than any
# backfill needs.
MAX_ROTATIONS=$((${#PROXY_HOSTS[@]} * 2))
rotations=0

# Pace. Aggressive on purpose — see the budget note above.
export USAU_RATE_LIMIT="${USAU_RATE_LIMIT:-0.5}"
export USAU_JITTER="${USAU_JITTER:-0.4}"
# Short ladder: with rotation automated, confirming a block quickly and moving
# to a new exit beats sleeping through an escalating probe schedule.
export USAU_BLOCK_PROBES="${USAU_BLOCK_PROBES:-5,10}"
export USAU_DB="$DB"

myip() { curl -s --max-time 20 -x "socks5h://${1}:1080" https://am.i.mullvad.net/ip; }

# Point USAU_PROXY at the next server whose proxy actually answers.
rotate() {
  local host ip tries
  for tries in 1 2 3 4 5 6; do
    host="${PROXY_HOSTS[$((PROXY_IX % ${#PROXY_HOSTS[@]}))]}"
    PROXY_IX=$((PROXY_IX + 1))
    ip="$(myip "$host")"
    if [ -n "$ip" ]; then
      export USAU_PROXY="socks5h://${host}:1080"
      echo ">> exit is now $ip via ${host%%.relays*}"
      return 0
    fi
    echo ">> ${host%%.relays*} not answering, trying next server"
  done
  echo ">> ROTATION FAILED — no usable proxy after 6 attempts"
  return 1
}

echo "== backfill $DIVISION -> $DB, seasons ${SEASONS[*]}"
echo "== pace ${USAU_RATE_LIMIT}s + 0..${USAU_JITTER}s jitter, probes ${USAU_BLOCK_PROBES}"
echo "== ${#PROXY_HOSTS[@]} US exits available, rotation cap $MAX_ROTATIONS"
rotate || exit 5

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
    echo "== BLOCKED in season $season (rotation $rotations/$MAX_ROTATIONS) — rotating exit"
    rotate || exit 5
  done
done

echo "== ALL DONE: $DIVISION seasons ${SEASONS[*]}"
