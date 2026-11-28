#!/bin/sh
# The Airflow launcher passes a deadline that is longer than its own wait timeout. It only fires
# when the launcher died without stopping the task, so a lost launcher cannot leave a paid
# Fargate task running. timeout exits 124 at the deadline and 137 when it had to send KILL.
set -eu

if [ -z "${MARKED_TRAILS_DEADLINE_SECONDS:-}" ]; then
  exec trip-marked-trails "$@"
fi

exec timeout --signal=TERM --kill-after=60s "${MARKED_TRAILS_DEADLINE_SECONDS}s" trip-marked-trails "$@"
