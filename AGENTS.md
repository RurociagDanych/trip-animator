# AGENTS.md

## Purpose

Repository guide for AI coding agents working on Trip Animator: a GPX/TCX and Strava trip animation platform with a React editor, a FastAPI backend, a Playwright + FFmpeg render worker and a Polars/DuckDB/dbt telemetry pipeline.

## Layout

- `.github/`: CI workflow (`ci.yml`) and manual VPS deployment workflow (`deploy-vps.yml`).
- `airflow/`: standalone Airflow 3 project with DAGs for hourly dbt Gold builds, Strava incremental sync, Silver backfill and platform maintenance.
- `apps/`: application packages.
  - `apps/web`: React 19 + TypeScript + Vite + TanStack Router/Query + Zustand + MapLibre GL v5 + Deck.gl v9 + uPlot. The deterministic geo/timeline/layout engine lives in `apps/web/src/lib/geo/{timeline,camera,director,layout,photos,map-config}.ts` and is shared by `EditorPage`, `ShareViewPage` and `RenderHarnessPage` (`window.__TRIP_RENDERER__`).
  - `apps/api`: FastAPI + SQLAlchemy 2.0 + Alembic + PostgreSQL + S3/Local `ObjectStore`. Handles auth/sessions (Argon2id + HMAC-SHA256 + CSRF), Strava OAuth2 PKCE + webhooks (`IngestJob`), project/stage/photo uploads, Gold DuckDB sync (`gold_sync.py`) and render job SSE (`LISTEN render_job_events`).
  - `apps/worker`: Playwright Chromium + FFmpeg MP4 render worker (`FOR UPDATE SKIP LOCKED` lease queue, chunked rendering, anti-SSRF IP-pinned HTTP proxy).
- `dbt/`: DuckDB Gold marts (`mart_stage_summary`, `fct_climbs`, `fct_splits`, `fct_dwell_stops`, `mart_stage_geo_context`, `mart_spatial_coverage_h3`, `mart_trip_daily_summary`, `mart_user_career_summary`) and data tests.
- `docs/`: architecture, operations, threat model, deployment docs, specifications (`docs/specs/`) and demo assets (`docs/demo/`).
- `infra/`: Docker Compose (`compose.yaml`), Caddy, nginx, Dockerfiles and optional AWS Terraform (`infra/terraform/`).
- `packages/`: `packages/telemetry`, the Polars + DuckDB + GeoParquet Bronze-to-Silver pipeline, DEM elevation backfill, climb/split/dwell analytics and photo alignment.
- `scripts/`: local bootstrap and publication secret gates with their tests.

## Commands

- Install: `make setup` (`uv sync --all-packages` and `pnpm install`), or `make bootstrap` for `.env`, dependencies, local PostgreSQL and migrations.
- Lint: `make lint`, `make format-check`, `make typecheck`, `make lint-airflow`.
- Test: `make test` (pytest + vitest), `make test-pg` (PostgreSQL tests), `make test-airflow`, `make dbt-build`.
- Run: `make dev-api` (FastAPI on :8000) and `make dev-web` (Vite on :5173); full stack with `docker compose -f infra/compose.yaml --env-file .env up -d --build`.
- Deploy dry run: `make compose-config`; `terraform fmt -check -recursive infra/terraform`, then `terraform init -backend=false && terraform validate` in `infra/terraform/environments/dev` and `prod`.
- Help: `make help` lists every Makefile target.
- Full local gate: `make check` before opening or updating a PR.

## Conventions

- Language: English only for identifiers, filenames, test names, log messages, error strings, commit messages and docs.
- Public modules, classes and functions have docstrings. Comments explain why, not what. Tool directives such as # noqa are allowed.
- Formatters and linters: Ruff (`uv run ruff check` / `uv run ruff format --check` on `apps packages airflow infra/scripts`), mypy (`apps/api/src apps/worker/src packages/telemetry/src airflow/src`), ESLint and `tsc --noEmit` for `apps/web`.
- Commit style: Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`, optional scope such as `feat(web):`).
- PR branches: use `feat/`, `fix/`, `docs/`, or `chore/` followed by a short lowercase kebab-case description (for example, `fix/preview-aspect-matches-resolution`). Branch from current `origin/main` and open the PR against `main` unless the task specifies another base.
- Test locations: `apps/api/tests`, `apps/worker/tests`, `packages/telemetry/tests`, `airflow/tests`, `scripts/tests`, `infra/scripts/tests`, `dbt/tests`, and Vitest tests in `apps/web/src/test`.

### Code invariants

- DB locking and async boundaries: never hold Postgres `FOR UPDATE` row locks across network/S3 I/O or `asyncio.to_thread` boundaries. Perform upload reads, image/track parsing and S3 writes first, then acquire `SELECT ... FROM projects ... FOR UPDATE` on the request session thread for quota/order checks and commit.
- Worker anti-SSRF: never call `route.continue_()` for external origins in Playwright; route all external requests through the IP-pinned `httpx.AsyncClient(follow_redirects=False)` proxy.
- Frontend 60 Hz playback: keep `currentTimeRatio` and RAF playback loops isolated inside `EditorPlaybackDock` with atomic Zustand selectors; do not subscribe `StageSidebar` or `InspectorPanel` to 60 Hz frame state.
- dbt incremental marts: stage-grain Gold marts use `unique_key='stage_id'` with `delete+insert` and pre/post-hooks to prune deleted and zero-row-delta stages.

## Guardrails

- Secrets: never commit `.env`, credentials, Terraform state or plans, object-store data or raw activity files; only `.env.example` with placeholders. Follow [SECURITY.md](SECURITY.md) and run `scripts/check-publication-secrets.sh` (Gitleaks with `.gitleaks.toml`).
- Git: no force-push and no history rewrite on shared branches.
- Deletes: do not delete files, data or directories without owner approval.
- Generated paths: `.venv/`, `node_modules/`, `apps/web/dist/`, `dbt/target/`, `.tmp/` and other ignored build or cache output stay untracked; do not edit or commit them.
- Lock files: `uv.lock` (workspace), `airflow/uv.lock` and `pnpm-lock.yaml` are authoritative; update them only through `uv` and `pnpm`.
