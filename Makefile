.PHONY: help setup bootstrap lint format format-check typecheck test test-pg dbt-parse dbt-build reap-dry-run check dev-api dev-web compose-config airflow-up airflow-down test-airflow lint-airflow remote-render-push remote-render-deploy

REMOTE_RENDER_TF_DIR ?= infra/terraform/standalone-remote-render
REMOTE_RENDER_AWS_REGION ?= eu-central-1
REMOTE_RENDER_IMAGE_TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)
REMOTE_RENDER_TF_ARGS ?=

help:
	@printf '%-22s %s\n' \
		help 'List the available targets' \
		setup 'Install Python and JavaScript dependencies' \
		bootstrap 'Create .env, install dependencies, start PostgreSQL, run migrations' \
		lint 'Run Ruff and ESLint' \
		format 'Format Python code with Ruff' \
		format-check 'Check Python formatting with Ruff' \
		typecheck 'Run mypy and tsc' \
		test 'Run pytest and Vitest' \
		test-pg 'Run the PostgreSQL-backed API and worker tests' \
		dbt-parse 'Parse the dbt project' \
		dbt-build 'Build dbt models and tests on synthetic Silver data' \
		reap-dry-run 'Show what the retention reaper would delete' \
		check 'Run the full local gate' \
		dev-api 'Start the FastAPI backend on :8000' \
		dev-web 'Start the Vite dev server' \
		compose-config 'Validate the Docker Compose configuration' \
		airflow-up 'Build and start Airflow' \
		airflow-down 'Stop Airflow' \
		test-airflow 'Run the Airflow and infra script tests' \
		lint-airflow 'Run Ruff and mypy on the Airflow project' \
		remote-render-push 'Build and push the AWS Lambda render image to ECR' \
		remote-render-deploy 'Bootstrap ECR, push Lambda image, and apply standalone AWS Terraform'

setup:
	uv sync --all-packages
	pnpm install

bootstrap:
	./scripts/bootstrap-local.sh

lint:
	uv run ruff check apps packages airflow infra/scripts
	pnpm lint:web

format:
	uv run ruff format apps packages airflow infra/scripts

format-check:
	uv run ruff format --check apps packages airflow infra/scripts

typecheck:
	uv run mypy apps/api/src apps/worker/src packages/telemetry/src airflow/src
	pnpm typecheck:web

test:
	uv run pytest --strict-markers -ra
	pnpm test:web

test-pg:
	uv run pytest -m postgres --strict-markers -ra apps/api/tests apps/worker/tests

dbt-parse:
	uv run dbt parse --project-dir dbt --profiles-dir dbt

dbt-build:
	@set -eu; \
	dbt_ci_dir="$(CURDIR)/.tmp/dbt_ci"; \
	rm -rf "$$dbt_ci_dir"; \
	trap 'rm -rf "$$dbt_ci_dir"' EXIT; \
	mkdir -p "$$dbt_ci_dir/silver" "$$dbt_ci_dir/gold"; \
	uv run python -c '\
	import sys; \
	from datetime import UTC, datetime, timedelta; \
	from pathlib import Path; \
	from trip_telemetry import ParsedTrack, TrackPoint, build_silver_dataframe, write_silver_parquet; \
	from trip_telemetry.ref_data import resolve_ref_data_dir; \
	base_dir = Path(sys.argv[1]); \
	silver_dir = base_dir / "silver"; \
	resolve_ref_data_dir(base_dir / "ref", ensure_exists=True); \
	make_pts = lambda base_dt, lat0, lon0, dwell: ( \
	    [TrackPoint(lat=lat0 + i * 0.0012, lon=lon0 + i * 0.0004, elev_m=1000.0 + i * 10.0, ts_utc=base_dt + timedelta(seconds=i * 60), hr_bpm=140 + (i % 15), cadence=85, watts=210 + (i % 30), segment_idx=0 if i < 12 else 1) for i in range(20)] \
	    + ([TrackPoint(lat=lat0 + 19 * 0.0012, lon=lon0 + 19 * 0.0004, elev_m=1190.0, ts_utc=base_dt + timedelta(seconds=19 * 60 + j * 60), moving=False, segment_idx=1) for j in range(1, 4)] if dwell else []) \
	); \
	t1 = ParsedTrack(name="CI Stage 1", points=make_pts(datetime(2024, 7, 1, 8, 0, 0, tzinfo=UTC), 0.120, 0.340, True), point_count=23, segment_count=2, has_timestamps=True, has_elevation=True); \
	t2 = ParsedTrack(name="CI Stage 2", points=make_pts(datetime(2024, 7, 2, 8, 0, 0, tzinfo=UTC), 0.220, 0.440, False), point_count=20, segment_count=2, has_timestamps=True, has_elevation=True); \
	df1, _ = build_silver_dataframe(t1, trip_id="trip-ci", stage_id="stage-ci-1"); \
	df2, _ = build_silver_dataframe(t2, trip_id="trip-ci", stage_id="stage-ci-2"); \
	write_silver_parquet(df1, silver_dir / "stage-ci-1.parquet"); \
	write_silver_parquet(df2, silver_dir / "stage-ci-2.parquet")' "$$dbt_ci_dir"; \
	uv run dbt parse --project-dir dbt --profiles-dir dbt; \
	DBT_DUCKDB_PATH="$$dbt_ci_dir/gold/trip_analytics.duckdb" uv run dbt build \
		--project-dir dbt \
		--profiles-dir dbt \
		--target-path "$$dbt_ci_dir/target" \
		--log-path "$$dbt_ci_dir/logs" \
		--vars "{\"silver_parquet_glob\": \"$$dbt_ci_dir/silver/*.parquet\", \"ref_data_dir\": \"$$dbt_ci_dir/ref\"}"

reap-dry-run:
	uv run python -m trip_api.cli reap --dry-run

check: lint format-check typecheck test test-airflow dbt-build compose-config

dev-api:
	uv run uvicorn trip_api.main:app --reload --host 0.0.0.0 --port 8000

dev-web:
	pnpm dev:web

compose-config:
	docker compose -f infra/compose.yaml --env-file .env.example config --quiet

airflow-up:
	docker compose -f infra/compose.yaml --env-file .env up -d --build airflow

airflow-down:
	docker compose -f infra/compose.yaml --env-file .env stop airflow

test-airflow:
	uv sync --project airflow --frozen
	uv run --project airflow pytest --strict-markers -ra airflow/tests infra/scripts/tests

lint-airflow:
	uv run ruff check airflow
	uv run mypy airflow/src

remote-render-push:
	@set -eu; \
	ecr_url="$$(terraform -chdir="$(REMOTE_RENDER_TF_DIR)" output -raw ecr_repository_url)"; \
	aws ecr get-login-password --region "$(REMOTE_RENDER_AWS_REGION)" \
		| docker login --username AWS --password-stdin "$$ecr_url"; \
	docker build --platform linux/amd64 \
		-f infra/docker/Dockerfile.worker-aws-lambda \
		-t "$$ecr_url:$(REMOTE_RENDER_IMAGE_TAG)" \
		.; \
	docker push "$$ecr_url:$(REMOTE_RENDER_IMAGE_TAG)"

remote-render-deploy:
	terraform -chdir="$(REMOTE_RENDER_TF_DIR)" init
	terraform -chdir="$(REMOTE_RENDER_TF_DIR)" apply \
		-target=aws_ecr_repository.worker \
		-var="aws_region=$(REMOTE_RENDER_AWS_REGION)" \
		-auto-approve
	$(MAKE) remote-render-push \
		REMOTE_RENDER_TF_DIR="$(REMOTE_RENDER_TF_DIR)" \
		REMOTE_RENDER_AWS_REGION="$(REMOTE_RENDER_AWS_REGION)" \
		REMOTE_RENDER_IMAGE_TAG="$(REMOTE_RENDER_IMAGE_TAG)"
	terraform -chdir="$(REMOTE_RENDER_TF_DIR)" apply \
		-var="aws_region=$(REMOTE_RENDER_AWS_REGION)" \
		-var="image_tag=$(REMOTE_RENDER_IMAGE_TAG)" \
		$(REMOTE_RENDER_TF_ARGS)
