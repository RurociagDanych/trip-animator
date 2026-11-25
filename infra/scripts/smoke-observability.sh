#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
umask 077
scratch=$(mktemp -d)
export COMPOSE_PROJECT_NAME="trip-obs-smoke-$$"
port=${SMOKE_GRAFANA_PORT:-33071}
conf=$scratch/env
stack=(docker compose --env-file "$conf" -f infra/compose.yaml -f infra/compose.observability.yaml)
fixture=("${stack[@]}" -f "$scratch/local.yaml")
cleanup() {
  if [[ $1 != 0 ]]; then
    "${fixture[@]}" --profile observability logs --tail=25 grafana prometheus loki alloy >&2 || true
  fi
  "${fixture[@]}" --profile observability down -v --remove-orphans
  rm -rf -- "$scratch"
}
trap 'cleanup "$?"' EXIT

mkdir "$scratch/secrets"
password=$(openssl rand -hex 24)
printf '%s' "$password" > "$scratch/secrets/grafana_admin_password"
printf 'OBSERVABILITY_ENABLED=true\nMETRICS_TOKEN=%s\nGRAFANA_LOCAL_PORT=%s\n' "$(openssl rand -hex 24)" "$port" > "$conf"
printf 'services:\n  postgres:\n    ports: !reset []\nnetworks:\n  edge:\n    ipam: !reset null\n' > "$scratch/local.yaml"

# Other checkouts may already use the default static edge subnet or database port.
"${fixture[@]}" config --quiet
if "${stack[@]}" config --services | grep -Ex 'grafana|prometheus|loki|alloy'; then
  printf '%s\n' 'Monitoring services must not be active in the default application model.' >&2
  exit 1
fi
"${fixture[@]}" up -d --wait postgres
"${fixture[@]}" run -T --no-deps --pull never --rm migrate
"${fixture[@]}" up -d --no-deps --build --wait api
infra/scripts/apply-observability.sh "$conf"

grafana="http://127.0.0.1:$port"
wait_for() {
  for _ in $(seq 1 45); do
    if "$@"; then return; fi
    sleep 2
  done
  return 1
}
curl_grafana() {
  curl -fsS --max-time 3 -u "admin:$password" "$@"
}
query() {
  curl_grafana -G "$grafana/api/datasources/proxy/uid/trip-prometheus/api/v1/query" --data-urlencode "query=$1"
}
queue_readable() {
  query 'trip_render_jobs_total{status="queued"} and on(job,instance) (trip_db_up == 1)' |
    python3 -c 'import sys,json; assert [row["value"][1] for row in json.load(sys.stdin)["data"]["result"]] == ["0"]'
}
if ! wait_for curl_grafana "$grafana/api/dashboards/uid/trip-api-overview" >/dev/null 2>&1; then
  curl_grafana "$grafana/api/dashboards/uid/trip-api-overview" >/dev/null
fi
test "$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' "$grafana/api/dashboards/uid/trip-api-overview")" = 401
wait_for queue_readable 2>/dev/null || queue_readable
printf '%s\n' 'Verified the authenticated dashboard and live API/PostgreSQL scrape.'

loki_readable() {
  curl_grafana -G "$grafana/api/datasources/proxy/uid/trip-loki/loki/api/v1/query_range" \
    --data-urlencode 'query={service=~"api|worker"}' |
    python3 -c 'import json, sys
expected = {
    "api": {"event": "http_server_error", "route": "/ready", "status": 503},
    "worker": {"event": "render_attempt_finished", "job_id": "00000000-0000-0000-0000-000000000001", "outcome": "failed"},
}
for stream in json.load(sys.stdin)["data"]["result"]:
    service = stream["stream"]["service"]
    if service in expected and any(json.loads(value[1]) == expected[service] for value in stream["values"]):
        del expected[service]
assert not expected, expected'
}
"${fixture[@]}" stop postgres
"${fixture[@]}" exec -T api python - <<'PY'
from time import sleep
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import UUID

from trip_telemetry.operational_events import record_render_outcome

for _ in range(10):
    try:
        with urlopen("http://localhost:8000/ready?fixture=not-in-logs", timeout=5):
            raise AssertionError("Readiness should fail while PostgreSQL is stopped")
    except HTTPError as exc:
        assert exc.code == 503
    record_render_outcome(UUID(int=1), "failed")
    sleep(1)
PY
wait_for loki_readable 2>/dev/null || loki_readable
"${fixture[@]}" up -d --wait postgres
printf '%s\n' 'Verified a real API readiness failure and a worker fixture event through Alloy, Loki and Grafana.'

api_id=$("${fixture[@]}" ps -q api)
printf '\nOBSERVABILITY_ENABLED=false\n' >> "$conf"
infra/scripts/apply-observability.sh "$conf"
test -z "$("${fixture[@]}" --profile observability ps --status running --services prometheus grafana loki alloy)"
test "$api_id" = "$("${fixture[@]}" ps --status running -q api)"
docker volume inspect "${COMPOSE_PROJECT_NAME}_prometheus_data" "${COMPOSE_PROJECT_NAME}_grafana_data" \
  "${COMPOSE_PROJECT_NAME}_loki_data" >/dev/null
printf '%s\n' 'Disabling monitoring preserves the running API and monitoring volumes.'
