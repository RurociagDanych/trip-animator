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
    "${fixture[@]}" --profile observability logs --tail=25 grafana prometheus >&2 || true
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
if "${stack[@]}" config --services | grep -Ex 'grafana|prometheus'; then
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

http_dashboard="$grafana/api/dashboards/uid/trip-api-http"
wait_for curl_grafana "$http_dashboard" >/dev/null
http_expression() {
  curl_grafana "$http_dashboard" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["dashboard"]["panels"][int(sys.argv[1])]["targets"][0]["expr"])' "$1"
}
error_expression=$(http_expression 0)
latency_expression=$(http_expression 1)
http_panels_readable() {
  # Generate traffic across scrapes so both rate expressions have observations.
  "${fixture[@]}" exec -T api python -c 'import http.client; c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=3); c.request("GET", "/_smoke_unmatched"); assert c.getresponse().status == 404' || return 1
  query "$error_expression" | python3 -c \
    'import json,sys; assert any(r["metric"] == {"route": "unmatched", "status_class": "4xx"} and float(r["value"][1]) > 0 for r in json.load(sys.stdin)["data"]["result"])' || return 1
  query "$latency_expression" | python3 -c \
    'import json,math,sys; assert any(r["metric"] == {"route": "unmatched"} and math.isfinite(float(r["value"][1])) and float(r["value"][1]) > 0 for r in json.load(sys.stdin)["data"]["result"])'
}
wait_for http_panels_readable 2>/dev/null || http_panels_readable
printf '%s\n' 'Verified live HTTP error rate and latency in Grafana from requests to the API.'

# Keep the running application container and both named volumes when switching off.
api_id=$("${fixture[@]}" ps -q api)
printf '\nOBSERVABILITY_ENABLED=false\n' >> "$conf"
infra/scripts/apply-observability.sh "$conf"
test -z "$("${fixture[@]}" --profile observability ps --status running --services prometheus grafana)"
test "$api_id" = "$("${fixture[@]}" ps --status running -q api)"
docker volume inspect "${COMPOSE_PROJECT_NAME}_prometheus_data" "${COMPOSE_PROJECT_NAME}_grafana_data" >/dev/null
printf '%s\n' 'Disabling monitoring preserves the running API and monitoring volumes.'
