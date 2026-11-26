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
printf 'services:\n  caddy:\n    ports: !reset []\n    networks:\n      edge:\n        ipv4_address: !reset null\n  minio:\n    ports: !reset []\n  airflow:\n    ports: !reset []\n' > "$scratch/probes.yaml"
fixture+=(-f "$scratch/probes.yaml")

# Other checkouts may already use the default static edge subnet or database port.
"${fixture[@]}" config --quiet
if "${stack[@]}" config --services | grep -Ex 'grafana|prometheus|loki|alloy'; then
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

ingest_dashboard="$grafana/api/dashboards/uid/trip-ingest-overview"
wait_for curl_grafana "$ingest_dashboard" >/dev/null
empty_ingest_queue() {
  query "$1" |
    python3 -c 'import json,sys; assert [r["value"][1] for r in json.load(sys.stdin)["data"]["result"]] == ["0"]'
}
for panel in 1 2; do
  expression=$(curl_grafana "$ingest_dashboard" |
    python3 -c 'import json,sys; print(next(p["targets"][0]["expr"] for p in json.load(sys.stdin)["dashboard"]["panels"] if p["id"] == int(sys.argv[1])))' "$panel")
  wait_for empty_ingest_queue "$expression" 2>/dev/null || empty_ingest_queue "$expression"
done
printf '%s\n' 'Verified both ingest dashboard queries against an empty PostgreSQL queue.'

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

editor_expression=$(curl_grafana "$grafana/api/dashboards/uid/trip-service-health" |
  python3 -c 'import json,sys; print(next(p["targets"][0]["expr"] for p in json.load(sys.stdin)["dashboard"]["panels"] if p["id"] == 3))')
editor_matches() {
  query "$editor_expression" | python3 -c 'import json,sys; assert [r["value"][1] for r in json.load(sys.stdin)["data"]["result"]] == json.loads(sys.argv[1])' "$1"
}
editor_matches '[]'
"${fixture[@]}" start web
"${fixture[@]}" exec -T api python - > "$scratch/secrets/editor_probe.json" <<'PY'
import json
from secrets import token_urlsafe

from trip_api.cli import create_user
from trip_api.config import get_settings
from trip_api.db import Project, create_db_engine, create_session_factory
from trip_telemetry import RenderSpecV2

email = "synthetic-editor@example.invalid"
password = token_urlsafe(32)
user = create_user(email=email, password=password)
with create_session_factory(create_db_engine(get_settings().database_url))() as db:
    project = Project(owner_user_id=user.id, title="Synthetic workspace", settings_json=RenderSpecV2().model_dump(mode="json"))
    db.add(project)
    db.commit()
    assert not project.share_enabled
    print(json.dumps(dict(email=email, password=password, project_id=str(project.id), project_title=project.title)))
PY
chmod 0444 "$scratch/secrets/editor_probe.json"
infra/scripts/apply-observability.sh "$conf" "$scratch/local.yaml" "$scratch/probes.yaml"
wait_for editor_matches '["1"]' 2>/dev/null || editor_matches '["1"]'
python3 -c 'import json,sys; print(json.dumps(dict(json.load(sys.stdin), project_id="00000000-0000-0000-0000-000000000001")))' \
  < "$scratch/secrets/editor_probe.json" > "$scratch/invalid-project.json"
mv "$scratch/invalid-project.json" "$scratch/secrets/editor_probe.json"
chmod 0444 "$scratch/secrets/editor_probe.json"
infra/scripts/apply-observability.sh "$conf" "$scratch/local.yaml" "$scratch/probes.yaml"
wait_for editor_matches '["0"]' 2>/dev/null || editor_matches '["0"]'
"${fixture[@]}" exec -T api python - <<'PY'
from sqlalchemy import select

from trip_api.config import get_settings
from trip_api.db import Project, SessionRecord, create_db_engine, create_session_factory

with create_session_factory(create_db_engine(get_settings().database_url))() as db:
    project = db.scalars(select(Project)).one()
    assert project.revision == 1 and not project.share_enabled
    assert db.scalar(select(SessionRecord).where(SessionRecord.user_id == project.owner_user_id)) is None
PY
printf '%s\n' 'Verified a real Chromium sign-in, private editor startup, failed project lookup, and session cleanup.'
mv "$scratch/secrets/editor_probe.json" "$scratch/disabled-project.json"
infra/scripts/apply-observability.sh "$conf" "$scratch/local.yaml" "$scratch/probes.yaml"
wait_for editor_matches '[]' 2>/dev/null || editor_matches '[]'

# The application and monitoring data must survive disabling observability.
api_id=$("${fixture[@]}" ps -q api)
printf '\nOBSERVABILITY_ENABLED=false\n' >> "$conf"
infra/scripts/apply-observability.sh "$conf" "$scratch/local.yaml" "$scratch/probes.yaml"
test -z "$("${fixture[@]}" --profile observability ps --status running --services prometheus grafana loki alloy service-probes disk-metrics editor-probe)"
test "$api_id" = "$("${fixture[@]}" ps --status running -q api)"
docker volume inspect "${COMPOSE_PROJECT_NAME}_prometheus_data" "${COMPOSE_PROJECT_NAME}_grafana_data" \
  "${COMPOSE_PROJECT_NAME}_loki_data" >/dev/null
printf '%s\n' 'Disabling monitoring preserves the running API and monitoring volumes.'
