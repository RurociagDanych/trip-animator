# Trip Animator v2 — Repository Guide for AI Agents

## 1. Architecture Overview

- **`apps/web`**: React 19 + TypeScript + Vite + TanStack Router/Query + Zustand + MapLibre GL v5 + Deck.gl v9 + uPlot.
  - Core deterministic geo/timeline/layout engine lives in `apps/web/src/lib/geo/{timeline,camera,director,layout,photos,map-config}.ts` and is shared by `EditorPage`, `ShareViewPage`, and `RenderHarnessPage` (`window.__TRIP_RENDERER__`).
- **`apps/api`**: FastAPI + SQLAlchemy 2.0 + Alembic + PostgreSQL + S3/Local `ObjectStore`.
  - Handles auth/sessions (Argon2id + HMAC-SHA256 + CSRF), Strava OAuth2 PKCE + webhooks (`IngestJob`), project/stage/photo uploads, Gold DuckDB sync (`gold_sync.py`), and render job SSE (`LISTEN render_job_events`).
- **`apps/worker`**: Playwright Chromium + FFmpeg MP4 render worker (`FOR UPDATE SKIP LOCKED` lease queue + chunked rendering + anti-SSRF IP-pinned HTTP proxy).
- **`packages/telemetry`**: Polars + DuckDB + GeoParquet Medallion Bronze-to-Silver pipeline, DEM elevation backfill, climb/split/dwell analytics, and photo alignment.
- **`dbt/`**: DuckDB Gold marts (`mart_stage_summary`, `fct_climbs`, `fct_splits`, `fct_dwell_stops`, `mart_stage_geo_context`, `mart_spatial_coverage_h3`, `mart_trip_daily_summary`, `mart_user_career_summary`).
- **`airflow/`**: Optional scheduled DAGs for hourly dbt Gold builds, Strava incremental sync, Silver backfill, and platform maintenance.
- **`infra/`**: Docker Compose (`compose.yaml`, `Caddyfile`) for VPS/local deployment and Terraform (`infra/terraform/`) for optional AWS ECS.

## 2. Engineering & Code Standards

- **Strict English-Only Code**: All identifiers, filenames, comments, docstrings, test names, log messages, error strings, and commit messages must be in English.
- **Formatting & Linting**:
  - Python: `uv run ruff check apps packages airflow`, `uv run ruff format --check apps packages airflow`, `uv run mypy apps/api/src apps/worker/src packages/telemetry/src airflow/src`.
  - Web: `npm --prefix apps/web run typecheck && npm --prefix apps/web run lint && npm --prefix apps/web test && npm --prefix apps/web run build`.
- **Unified Local Gate**: Run `make check` before opening or updating a PR.

## 3. Critical Invariants

- **DB Locking & Async Boundaries**: Never hold Postgres `FOR UPDATE` row locks across network/S3 I/O or `asyncio.to_thread` boundaries. Perform upload reads, image/track parsing, and S3 writes first, then acquire `SELECT ... FROM projects ... FOR UPDATE` on the request session thread for quota/order checks and commit.
- **Worker Anti-SSRF**: Never call `route.continue_()` for external origins in Playwright; route all external requests through the IP-pinned `httpx.AsyncClient(follow_redirects=False)` proxy.
- **Frontend 60Hz Playback**: Keep `currentTimeRatio` and RAF playback loops isolated inside `EditorPlaybackDock` with atomic Zustand selectors; do not subscribe `StageSidebar` or `InspectorPanel` to 60Hz frame state.
- **dbt Incremental Marts**: Stage-grain Gold marts use `unique_key='stage_id'` with `delete+insert` and pre/post-hooks to prune deleted and zero-row-delta stages.
