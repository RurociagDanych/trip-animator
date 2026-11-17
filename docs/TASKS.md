# Trip Animator — Phased Implementation Plan

This milestone plan breaks **Trip Animator** into six incremental, testable phases from initial monorepo scaffold (`Phase 0`) through production hardening (`Phase 5`).

---

## Phase 0 — Monorepo, Infrastructure & Architecture Baseline
**Goal**: Establish a reproducible monorepo, hardened container/edge baseline, and documented architecture & security contracts.

### Deliverables
- Git repository initialized on `main` with `uv` workspace (`packages/telemetry`, `apps/api`, `apps/worker`) and `pnpm` workspace (`apps/web`).
- `apps/api`: FastAPI app skeleton, settings validation, SQLAlchemy 2.0 + Alembic baseline, `/health` probe.
- `packages/telemetry`: Medallion package skeleton (`defusedxml` GPX parser, Polars Silver GeoParquet builder, DuckDB Gold analytics interfaces).
- `dbt/`: `dbt-duckdb` project skeleton (`dbt_project.yml`, `profiles.yml`, Silver->Gold model & schema test placeholders).
- `apps/worker`: Playwright + FFmpeg render worker skeleton.
- `apps/web`: Vite + React 19 + TypeScript (strict) + Tailwind CSS v4 + TanStack Router/Query + Zustand + MapLibre/Deck.gl + uPlot skeleton with CSRF-aware API client.
- `infra/`: `compose.yaml` (Postgres 17, MinIO + init, migrate, hardened non-root read-only API/Worker/Web, Caddy 2 edge), `Caddyfile`, Dockerfiles.
- `docs/`: `ARCHITECTURE.md`, `THREAT_MODEL.md`, `TASKS.md`.

### Verification Gate
- `docker compose -f infra/compose.yaml --env-file .env.example config --quiet` exits `0`.
- `pnpm typecheck:web && pnpm lint:web && pnpm test:web && pnpm build:web` passes with 0 errors.
- `uv run ruff check . && uv run mypy packages apps && uv run pytest` passes baseline smoke tests.

---

## Phase 1 — Data Platform, Auth & Hardened GPX Ingestion
**Goal**: Complete relational OLTP schema, OWASP-compliant authentication, ObjectStore abstraction, and Bronze -> Silver -> Gold GPX ingestion pipeline.

### Deliverables
- Alembic migrations for `users`, `sessions`, `oauth_states`, `oauth_connections`, `projects`, `stages`, `render_jobs`, `audit_events`, and `rate_limits`.
- Auth endpoints (`/api/v1/auth/{login,dev-login,logout,me}`) with Argon2id, opaque SHA-256 sessions, constant-time CSRF (`X-CSRF-Token`), persistent PG rate limits, and prod boot guards.
- `ObjectStore` port with `LocalObjectStore` and `S3ObjectStore` (RustFS/MinIO/AWS compatible).
- Isolated subprocess GPX parser (`defusedxml`, 25 MiB cap, 15s hard timeout, depth/segment/point caps, antimeridian unwrapping via `unwrap_longitudes`).
- Polars Silver GeoParquet + multi-LOD (`full`, `medium`, `low`) writer and `dbt-duckdb` Gold KPI models + schema/quality tests (`not_null`, coordinate bounds, monotonic `cum_dist_m`).
- CLI `create-user` provisioning tool (`python -m trip_api.cli create-user`).

### Verification Gate
- Security test suite passes: XXE, billion-laughs, 15s parser timeout, malformed multipart `safeError`, prod boot guard failures, CSRF mismatch rejection, rate-limit `429`.
- `dbt build` passes all Gold models and data quality tests on multi-track sample fixtures (including antimeridian crossing).
- Integration test uploads GPX -> persists Bronze + Silver GeoParquet + Gold KPIs -> verifies stage metadata in PostgreSQL.

---

## Phase 2 — Strava OAuth2 & Multi-Day Stage Authoring API
**Goal**: Add secure Strava activity import and complete multi-day trip authoring + DuckDB OLAP query APIs.

### Deliverables
- Strava OAuth2 **PKCE (`S256`)** + `state` flow (`/api/v1/integrations/strava/{authorize,callback,status,disconnect,webhook}`), versioned `AES-256-GCM` token encryption, concurrency-safe refresh, SSRF-guarded `httpx` client.
- Strava activity browser & stream importer (`latlng`, `time`, `altitude`, `velocity_smooth`, `heartrate`, `cadence`, `watts`, `grade_smooth`) writing Bronze JSON -> Silver GeoParquet -> Gold KPIs.
- Project & stage CRUD API:
  - Optimistic locking (`revision`), stage reorder, rename, day-grouping, delete, non-destructive trim/pause compression.
  - Validation of `stage_colors` and `stage_transitions` (`none`, `bed`, `meal`, `coffee`, `tent`, `transport`) against existing project stage IDs, plus `showOvernightStops`.
- DuckDB analytical endpoints:
  - `GET /api/v1/projects/{id}/telemetry/chart` (LTTB downsampled elevation/speed/grade/HR/power series).
  - `GET /api/v1/projects/{id}/telemetry/geometry` (zoom-appropriate simplified GeoJSON/LOD track geometry).
  - `GET /api/v1/projects/{id}/kpis` (Gold trip & per-stage summary KPIs).

### Verification Gate
- Unit & integration tests with mocked Strava endpoints verify PKCE `S256`, `state` mismatch rejection, AEAD roundtrip/key rotation, token refresh, deauth cleanup, and SSRF IP blocking.
- LTTB chart query benchmark returns <= 2,000 points for a 100k-point multi-day trip in < 50ms.

---

## Phase 3 — Modern 3-Pane Editor & 1:1 WYSIWYG Browser Preview
**Goal**: Deliver a modern, responsive React 19 UI with full v1 feature parity, bug fixes, and rich multi-day authoring.

### Deliverables
- **Routes**: `/login`, `/` (Project Dashboard with KPI cards, search/pagination, Strava connection badge), `/projects/$projectId` (3-pane Studio Editor), `/render/$jobId` (deterministic render harness).
- **Stage & Source Authoring**: Drag-and-drop multi-GPX uploader with per-file progress, Strava activity picker modal, stage reorder/rename/delete/day-group controls, per-stage color & transition icon (`bed`, `meal`, `coffee`, `tent`, `transport`) + `showOvernightStops` toggle.
- **Map & Animation Engine**: MapLibre GL JS v5 (OpenFreeMap styles `liberty`, `positron`, `bright`, `dark`, optional 3D terrain/pitch) + Deck.gl progressive `full`/`traversed`/`trail` layers + fixed dynamic `follow-marker` camera + crisp vector/HTML HUD (`statisticsPanel` with all 8 reorderable metrics, IANA timezone, metric/imperial, opacity/typography controls).
- **Interactive Telemetry Scrubber**: Synchronized `uPlot` elevation/speed/grade chart below the map; hovering/scrubbing updates the map marker and HUD live; play/pause/speed controls.
- **Persistence UX**: Explicit dirty indicator + debounced autosave + save-before-render guard using `revision`.

### Verification Gate
- `pnpm typecheck:web && pnpm lint:web && pnpm test:web && pnpm build:web` passes cleanly.
- Component & integration tests (Vitest + Testing Library) cover login/CSRF, stage reorder, autosave revision conflict handling, and synced scrubber/map state.

---

## Phase 4 — Deterministic Render Worker & Video Export UX
**Goal**: Achieve 1:1 WYSIWYG server-side MP4 & preview rendering with persistent job history and inline playback.

### Deliverables
- `apps/worker` `FOR UPDATE SKIP LOCKED` claim loop (120s lease, periodic heartbeat, max 3 attempts, backoff retry).
- Deterministic `/render/$jobId` harness with virtual frame clock (`seekToFrame(i)`), `fit-track` static tile background caching, and proportional 5s/15fps watermarked preview trail calculation.
- Playwright `page.route()` URL allowlist + private-IP blocker; fixed-argv `ffmpeg` (`libx264`, `yuv420p`, `movflags +faststart`, `-an`) + `ffprobe` validation + WebP thumbnail generation.
- API render endpoints with `Idempotency-Key` payload verification, SSE progress stream (`/api/v1/renders/{id}/events`), cancel, download, and thumbnail serving.
- Editor Export Drawer with persistent render job history, inline `<video>` player, and MP4 download.

### Verification Gate
- End-to-end render test generates a valid H.264/YUV420p MP4 + WebP thumbnail verified via `ffprobe` for both `fit-track` and `follow-marker` presets (`1280x720`, `1080x1920`, and 5s watermarked preview).
- Playwright SSRF test confirms requests to `http://169.254.169.254`, `http://api:8000` (outside `/render` origin), and `file://` are aborted.

---

## Phase 5 — Operational Hardening, Observability & Release Gates
**Goal**: Harden production deployment, automate data retention, add observability, and enforce CI release gates.

### Deliverables
- Retention reaper job (`python -m trip_api.cli reap` & Airflow `trip_platform_maintenance_and_audit` DAG) pruning expired `sessions`, expired `render_jobs` output objects (MP4s/thumbnails), orphaned Bronze/Silver/LOD blobs, and expired `oauth_states` / `rate_limits`.
- Prometheus `/metrics` & structured JSON logging (with coordinate/secret redaction).
- Optional pinned/self-hostable OpenFreeMap/PMTiles tile configuration for air-gapped or rate-limit-free rendering.
- Production hardening verification in `infra/compose.yaml` (`read_only: true`, `cap_drop: ALL`, non-root users, Caddy TLS).
- CI workflow running lint, strict typecheck, unit/security/dbt/Airflow DAG tests, Playwright/Vitest suite, and Docker Compose config validation.

### Verification Gate
- Full `docker compose -f infra/compose.yaml up --build` stack passes end-to-end smoke test (CLI `create-user` / login -> GPX upload -> Gold KPI check -> 5s render -> inline playback).
- 100k-point multi-day GPX benchmark meets ingestion (< 3s), LTTB query (< 50ms), and `fit-track` render throughput targets.
- Zero critical/high unwaived CVEs across Python, Node, and container images.
