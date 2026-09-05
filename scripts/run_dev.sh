#!/usr/bin/env bash
# Starts the whole system for local demo/dev use: the API, every background
# job (§14 — one process per job, matching how the architecture says they
# deploy), and the operator console.
#
# Requires: reclaim_dev migrated (scripts/apply_migrations.py), the
# recovery_app / recovery_verifier roles created, and `npm run build` already
# run once in web/ (the console is served as a static build, not a dev
# server, so its behaviour here matches production).
#
# Usage:
#   scripts/run_dev.sh          # start everything, logs under /tmp/reclaim-*.log
#   scripts/run_dev.sh stop     # stop everything this script started

set -euo pipefail
cd "$(dirname "$0")/.."

# Nothing in the application loads .env itself — the processes below only see
# what's actually exported into their environment.
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

PIDFILE=/tmp/reclaim-dev.pids
LOGDIR=/tmp

JOBS=(
  action-deadline-expiry breaker-monitor case-worker diagnosis executor
  policy reconciler review-expiry sweeper ttl-expiry verifier
)

if [[ "${1:-}" == "stop" ]]; then
  if [[ -f "$PIDFILE" ]]; then
    xargs kill < "$PIDFILE" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "stopped."
  else
    echo "nothing tracked in $PIDFILE"
  fi
  exit 0
fi

: > "$PIDFILE"

echo "starting API on :8000"
python3 -m uvicorn reclaim.api.main:app --port 8000 --log-level warning \
  > "$LOGDIR/reclaim-api.log" 2>&1 &
echo $! >> "$PIDFILE"

for job in "${JOBS[@]}"; do
  echo "starting job: $job"
  python3 -m reclaim.jobs --job "$job" > "$LOGDIR/reclaim-job-$job.log" 2>&1 &
  echo $! >> "$PIDFILE"
done

echo "starting console on :4000"
# Run directly rather than in a `(cd web && ...) &` subshell: `$!` would
# capture the subshell's PID, not node's, and stop would leave node itself
# still bound to the port.
(cd web && exec node server/index.js) > "$LOGDIR/reclaim-web.log" 2>&1 &
echo $! >> "$PIDFILE"

sleep 2
echo ""
echo "console:  http://localhost:4000"
echo "api:      http://localhost:8000/api/health"
echo "logs:     $LOGDIR/reclaim-*.log"
echo "stop with: scripts/run_dev.sh stop"
