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
printf 'services:\n  caddy:\n    ports: !reset []\n    networks:\n      edge:\n        ipv4_address: !reset null\n  minio:\n    ports: !reset []\n  airflow:\n    ports: !reset []\n' > "$scratch/probes.yaml"
fixture+=(-f "$scratch/probes.yaml")

# Other checkouts may already use the default static edge subnet or database port.
"${fixture[@]}" config --quiet
if "${stack[@]}" config --services | grep -Ex 'grafana|prometheus'; then
  printf '%s\n' 'Monitoring services must not be active in the default application model.' >&2
  exit 1
fi
"${fixture[@]}" up -d --wait postgres
"${fixture[@]}" run -T --no-deps --pull never --rm migrate
"${fixture[@]}" up -d --no-deps --build --wait api
"${fixture[@]}" up -d --no-deps --wait web caddy minio
"${fixture[@]}" run -T --no-deps --rm airflow-init
"${fixture[@]}" up -d --no-deps --wait --wait-timeout 180 airflow
infra/scripts/apply-observability.sh "$conf" "$scratch/local.yaml" "$scratch/probes.yaml"

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
queue_age_query=$(curl_grafana "$grafana/api/dashboards/uid/trip-api-overview" |
  python3 -c 'import sys,json; panels=json.load(sys.stdin)["dashboard"]["panels"]; print(next(p for p in panels if p["id"] == 4)["targets"][0]["expr"])')
query "$queue_age_query" |
  python3 -c 'import sys,json; assert [row["value"][1] for row in json.load(sys.stdin)["data"]["result"]] == ["0"]'
printf '%s\n' 'Verified the dashboard queue age query against an empty PostgreSQL queue.'
printf '%s\n' 'Verified the authenticated dashboard and live API/PostgreSQL scrape.'

probe_expression=$(curl_grafana "$grafana/api/dashboards/uid/trip-service-health" |
  python3 -c 'import sys,json; print(json.load(sys.stdin)["dashboard"]["panels"][0]["targets"][0]["expr"])')
probes_match() {
  query "$probe_expression" | python3 -c 'import json, sys; rows = json.load(sys.stdin)["data"]["result"]; assert {r["metric"]["service"]: r["value"][1] for r in rows} == json.loads(sys.argv[1])' "$1"
}
wait_for probes_match '{"caddy":"1","web":"1","rustfs":"1","airflow":"1"}' 2>/dev/null
"${fixture[@]}" stop web
wait_for probes_match '{"caddy":"0","web":"0","rustfs":"1","airflow":"1"}' 2>/dev/null
"${fixture[@]}" --profile observability stop service-probes
wait_for probes_match '{}' 2>/dev/null
"${fixture[@]}" --profile observability start service-probes
printf '%s\n' 'Verified healthy, failed, and unknown internal service probes in Grafana.'

disk_expression=$(curl_grafana "$grafana/api/dashboards/uid/trip-service-health" |
  python3 -c 'import json,sys; print(next(p for p in json.load(sys.stdin)["dashboard"]["panels"] if p["id"] == 2)["targets"][0]["expr"])')
disk_matches() {
  query "$disk_expression" | python3 -c 'import json,sys; rows = json.load(sys.stdin)["data"]["result"]; assert (len(rows) == 1 and float(rows[0]["value"][1]) > 0) if sys.argv[1] == "present" else not rows' "$1"
}
wait_for disk_matches present 2>/dev/null
"${fixture[@]}" --profile observability stop disk-metrics
wait_for disk_matches absent 2>/dev/null
"${fixture[@]}" --profile observability start disk-metrics
printf '%s\n' 'Verified real Docker volume disk headroom and unknown state after exporter shutdown.'

# Keep the running application container and both named volumes when switching off.
api_id=$("${fixture[@]}" ps -q api)
printf '\nOBSERVABILITY_ENABLED=false\n' >> "$conf"
infra/scripts/apply-observability.sh "$conf" "$scratch/local.yaml" "$scratch/probes.yaml"
test -z "$("${fixture[@]}" --profile observability ps --status running --services prometheus grafana service-probes disk-metrics)"
test "$api_id" = "$("${fixture[@]}" ps --status running -q api)"
docker volume inspect "${COMPOSE_PROJECT_NAME}_prometheus_data" "${COMPOSE_PROJECT_NAME}_grafana_data" >/dev/null
printf '%s\n' 'Disabling monitoring preserves the running API and monitoring volumes.'
