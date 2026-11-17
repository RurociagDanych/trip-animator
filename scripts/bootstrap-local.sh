#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

for command in uv pnpm docker; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  fi
done

if [[ ! -f .env ]]; then
  cp .env.example .env
  printf 'Created .env from .env.example (local development defaults).\n'
fi

# uv loads .env while preserving explicitly exported process variables. Validate
# the effective target before starting services or applying any migrations.
(
  cd apps/api
  uv run --project . --env-file "$ROOT_DIR/.env" python -c '
import os
import sys
from urllib.parse import urlsplit

environment = os.environ.get("APP_ENV", "").strip().lower()
if environment not in {"", "local", "development", "dev", "test"}:
    print("Refusing local bootstrap: APP_ENV must be local/development.", file=sys.stderr)
    raise SystemExit(1)

database_url = os.environ.get("DATABASE_URL", "")
try:
    parsed = urlsplit(database_url)
    hostname = (parsed.hostname or "").lower()
    # URL options and libpq routing variables can override the visible host.
    if parsed.query or parsed.fragment:
        raise ValueError
    if any(os.environ.get(name) for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")):
        raise ValueError
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError
except ValueError:
    print("Refusing local bootstrap: use a loopback PostgreSQL URL without query options or libpq routing overrides.", file=sys.stderr)
    raise SystemExit(1)
'
)

docker compose version >/dev/null

uv sync --all-packages --frozen
pnpm install --frozen-lockfile

docker compose --env-file "$ROOT_DIR/.env" -f infra/compose.yaml up -d postgres

printf 'Waiting for PostgreSQL health check'
for _ in $(seq 1 60); do
  if docker compose --env-file "$ROOT_DIR/.env" -f infra/compose.yaml exec -T postgres pg_isready >/dev/null 2>&1; then
    printf ' ready\n'
    break
  fi
  printf '.'
  sleep 2
done

if ! docker compose --env-file "$ROOT_DIR/.env" -f infra/compose.yaml exec -T postgres pg_isready >/dev/null 2>&1; then
  printf '\nPostgreSQL did not become ready within 120 seconds.\n' >&2
  exit 1
fi

(
  cd apps/api
  uv run --project . --env-file "$ROOT_DIR/.env" alembic upgrade head
)
printf 'Local dependencies installed, PostgreSQL is healthy, and migrations are current.\n'
