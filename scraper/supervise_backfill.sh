#!/usr/bin/env bash
# Supervises the mixed + women backfills to actual completion.
#
# backfill.sh caps itself at 42 exit rotations per invocation (deliberate: a
# broken outage shouldn't spin forever), but at the WAF's current temper a full
# 10-season division needs several such invocations. Every invocation resumes
# from the shared HTML cache, so re-running until "== ALL DONE" appears is
# cheap and idempotent. This wrapper does exactly that, sequentially per
# division so two runs never fight over the single Mullvad tunnel.
#
# Usage: scraper/supervise_backfill.sh [wait-pid]
#   wait-pid: optional pid of an already-running backfill wrapper to wait out
#             before taking over (avoids concurrent VPN rotation).

set -uo pipefail
cd "$(dirname "$0")/.."

WAIT_PID="${1:-}"
MAX_ATTEMPTS=20   # x42 rotations each; far beyond any plausible need

if [ -n "$WAIT_PID" ]; then
  echo "== supervisor: waiting for existing wrapper pid $WAIT_PID to finish"
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 60; done
  echo "== supervisor: pid $WAIT_PID gone, taking over"
fi

run_division() {
  local division="$1" db="$2" log="$3" attempt rc
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    # Already finished (this run or a previous one)?
    if [ -f "$log" ] && grep -q "^== ALL DONE" "$log"; then
      echo "== supervisor: $division already ALL DONE"
      return 0
    fi
    echo "== supervisor: $division attempt $attempt/$MAX_ATTEMPTS"
    scraper/backfill.sh "$division" "$db" 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026 2>&1 | tee -a "$log"
    rc=${PIPESTATUS[0]}
    case "$rc" in
      0) echo "== supervisor: $division complete"; return 0 ;;
      4) echo "== supervisor: $division hit rotation cap, re-running" ;;
      5) echo "== supervisor: $division rotation failed (VPN?), pausing 5m"; sleep 300 ;;
      *) echo "!! supervisor: $division exited $rc (real crash) — aborting"; return "$rc" ;;
    esac
  done
  echo "!! supervisor: $division exhausted $MAX_ATTEMPTS attempts"
  return 6
}

run_division club-mixed data/usau_mixed.db data/scrape_mixed.log || exit $?
run_division club-women data/usau_women.db data/scrape_women.log || exit $?
echo "== supervisor: BOTH DIVISIONS DONE"
