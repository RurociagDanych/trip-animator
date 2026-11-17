.PHONY: setup bootstrap lint typecheck test dbt-parse dev-api dev-web compose-config airflow-up airflow-down test-airflow lint-airflow

setup:
	uv sync --all-packages
	pnpm install

bootstrap:
	./scripts/bootstrap-local.sh

lint:
	uv run ruff check apps packages airflow
	pnpm lint:web

typecheck:
	uv run mypy apps/api/src apps/worker/src packages/telemetry/src airflow/src
	pnpm typecheck:web

test:
	uv run pytest
	pnpm test:web

dbt-parse:
	uv run dbt parse --project-dir dbt --profiles-dir dbt

dev-api:
	uv run uvicorn trip_api.main:app --reload --host 0.0.0.0 --port 8000

dev-web:
	pnpm dev:web

compose-config:
	docker compose -f infra/compose.yaml --env-file .env.example config --quiet
	docker compose -f infra/compose.yaml --env-file .env.example --profile airflow config --quiet

airflow-up:
	docker compose -f infra/compose.yaml --env-file .env --profile airflow up -d --build

airflow-down:
	docker compose -f infra/compose.yaml --env-file .env --profile airflow down

test-airflow:
	uv sync --project airflow
	uv run --project airflow pytest airflow/tests -v

lint-airflow:
	uv run ruff check airflow
	uv run mypy airflow/src
