#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
env_file=${1:-$root/.env}
base=(docker compose --env-file "$env_file" -f "$root/infra/compose.yaml")
stack=("${base[@]}" -f "$root/infra/compose.observability.yaml")
services=(prometheus loki alloy grafana service-probes disk-metrics)
for override in "${@:2}"; do
  stack+=(-f "$override")
done


flag=$("${base[@]}" config --environment | awk '/^OBSERVABILITY_ENABLED=/ {sub(/^[^=]*=/, ""); print}')
case "$flag" in
  true)
    password_file=$(dirname -- "$env_file")/secrets/grafana_admin_password
    if [[ ! -r "$password_file" || ! -s "$password_file" ]] ||
       ! "${base[@]}" config --environment | awk '/^METRICS_TOKEN=./ { found = 1 } END { exit !found }'; then
      printf '%s\n' 'Set METRICS_TOKEN and create secrets/grafana_admin_password before enabling observability.' >&2
      exit 1
    fi
    GRAFANA_ADMIN_PASSWORD=$(<"$password_file")
    if [[ -z "$GRAFANA_ADMIN_PASSWORD" ]]; then
      printf '%s\n' 'The Grafana password file must not be blank.' >&2
      exit 1
    fi
    export GRAFANA_ADMIN_PASSWORD
    if [[ -z "${COMPOSE_PROJECT_NAME:-}" ]]; then
      COMPOSE_PROJECT_NAME=$("${base[@]}" config | awk '/^name:/ {print $2; exit}')
      export COMPOSE_PROJECT_NAME
    fi
    "${stack[@]}" up -d --no-deps "${services[@]}"
    editor_config=$(dirname -- "$env_file")/secrets/editor_probe.json
    if [[ -s "$editor_config" ]]; then
      EDITOR_PROBE_CONFIG_FILE=$(realpath -- "$editor_config")
      export EDITOR_PROBE_CONFIG_FILE
      "${stack[@]}" up -d --no-deps --force-recreate editor-probe
    else
      "${stack[@]}" stop editor-probe
    fi
    ;;
  false|'')
    "${stack[@]}" stop "${services[@]}" editor-probe
    ;;
  *)
    printf '%s\n' 'OBSERVABILITY_ENABLED must be true or false.' >&2
    exit 1
    ;;
esac
