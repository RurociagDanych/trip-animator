# trip-animator

[![CI](https://github.com/RurociagDanych/trip-animator/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/RurociagDanych/trip-animator/actions/workflows/ci.yml)

Trip Animator turns GPX/TCX tracks (or Strava activities, via an optional OAuth app you configure) into an animated multi-day trip film.

A browser editor lets you arrange stages, preview the synchronized map and elevation/speed telemetry, and export an MP4. Sharing and playback views are included.

![Local Trip Animator editor showing five consecutive cycling days on a loaded basemap, with elevation and speed telemetry](docs/demo/real-route-editor.png)

The screenshot is from the running local editor: five consecutive cycling days (August 1–5), nine GPX stages totaling 751.0 km, published with the route owner's permission. Source activity files and photos are not in this repository. For a demo without personal data, run `python docs/demo/generate_synthetic_gpx.py /path/to/demo.gpx` and import the result into a new trip.

Basemap: [OpenFreeMap](https://openfreemap.org), © [OpenMapTiles](https://openmaptiles.org) and © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).

## Status

Active, self-hosted. Last substantive change: 2026-11-26. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for what has and has not been validated.

## Architecture

```mermaid
flowchart TB
    Browser["Browser SPA (React 19, MapLibre, Deck.gl, uPlot)"]
    Caddy["Caddy 2 Edge (:80/:443 — TLS, CSP, HSTS, 25MB cap)"]
    Web["Static Web (nginx-unprivileged :8080)"]
    API["FastAPI API (:8000 — Argon2id, CSRF, PKCE, DuckDB)"]
    Worker["Render Worker (SKIP LOCKED, Playwright Chromium, FFmpeg)"]
    PG[("PostgreSQL 17 (OLTP + Render Lease Queue)")]
    Store[("ObjectStore (RustFS S3 / Local FS — Bronze, Silver, Outputs)")]

    Browser -->|"HTTPS / Cookie + X-CSRF-Token"| Caddy
    Caddy -->|"/*"| Web
    Caddy -->|"/api/*, /health, /ready"| API
    API --> PG
    API --> Store
    Worker -->|"FOR UPDATE SKIP LOCKED"| PG
    Worker -->|"Silver Parquet / MP4 + WebP"| Store
    Worker -->|"Load /render/:jobId"| Caddy
```

Data flows Bronze (raw GPX/Strava payloads) → Silver (Parquet via Polars) → Gold (dbt-duckdb marts). Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Key design decisions

- **Editor and render harness share one React/map presentation.** The worker drives the `/render/$jobId` route of the same SPA, so exported frames match the preview. Trade-off: rendering needs a headless Chromium and map-tile access, and render cost depends on the host.
- **Columnar ingestion pipeline (Polars/PyArrow → Parquet, DuckDB/dbt for analytics).** Telemetry is normalized once into a canonical point schema with multi-LOD simplification and queried with DuckDB, so charts and map geometry stay responsive on large tracks. Trade-off: more moving parts than storing points in PostgreSQL.
- **Separate async render worker on a PostgreSQL queue.** Jobs are leased with `FOR UPDATE SKIP LOCKED`, so long renders never block editing and no extra broker is needed. Trade-off: throughput is bounded by PostgreSQL polling and worker count.
- **Untrusted GPX is parsed in an isolated subprocess.** `defusedxml` with hard size, depth and time limits contains malformed or hostile files. Trade-off: per-upload process overhead.
- **Airflow is required for Strava Vault synchronization.** It runs alongside the default Compose stack; library browsing and GPX import remain available without it, while sync requests wait for recovery. Other scheduled DAGs are opt-in. Trade-off: an additional service to operate.
- **Fail-closed production config.** With `APP_ENV=production` the API refuses to start on dev auth, weak secrets or missing S3 credentials. Trade-off: more required configuration. See [SECURITY.md](SECURITY.md).

### Scale and limits

| Limit | Value | Source |
| :--- | :--- | :--- |
| GPX upload size | 25 MiB | `MAX_GPX_BYTES` in `packages/telemetry/src/trip_telemetry/gpx_parser.py`; Caddy `max_size 25MB` in `infra/Caddyfile` |
| Tracks / segments / points per GPX | 20 / 1,000 / 500,000 | `MAX_TRACKS`, `MAX_SEGMENTS`, `MAX_POINTS` in `gpx_parser.py` |
| GPX parse timeout | 15 s | `PARSE_TIMEOUT_SECONDS` in `gpx_parser.py` |
| Photo upload size | 15 MiB | `MAX_PHOTO_BYTES` in `apps/api/src/trip_api/routers/photos.py` |
| Render artifact retention | 7 days (default) | `RENDER_RETENTION_DAYS` in `infra/compose.yaml` |

### Repository layout

| Path | Contents |
| :--- | :--- |
| `apps/web` | React 19 + TypeScript editor and `/render/$jobId` frame harness |
| `apps/api` | FastAPI backend: auth, authoring, GPX/Strava ingestion, DuckDB queries |
| `apps/worker` | Playwright + FFmpeg render worker |
| `packages/telemetry` | GPX parser, Silver ETL, simplification, downsampling |
| `packages/object-store` | Shared Local and S3-compatible object store (`trip_object_store`) |
| `dbt/` | Silver → Gold SQL models and tests |
| `airflow/` | Strava Vault sync orchestration and optional scheduled jobs |
| `infra/` | Docker Compose, Caddy, nginx |
| `docs/` | Architecture, operations, threat model, deployment and specification documents (see [Docs index](#docs-index)) |

## Quick start

Prerequisites:

- Python 3.12+ with [`uv`](https://docs.astral.sh/uv/)
- Node.js 24+ with [`pnpm`](https://pnpm.io/) v11
- Docker Engine with the Docker Compose plugin (the bootstrap starts this repo's PostgreSQL service)
- `ffmpeg` and Playwright Chromium are optional; install them only if running the local render worker

```bash
make bootstrap                      # .env, deps, local PostgreSQL, migrations
uv run python -m trip_api.cli create-user --email rider@example.com \
  --password "<choose-a-password>" --display-name "Rider"
make dev-api                        # FastAPI on :8000
make dev-web                        # Vite on :5173
```

### Apache Airflow 3 orchestration (required for Strava sync)

Airflow is included in the default Compose stack and is required to execute Strava Vault synchronization. Catalog browsing and GPX import remain available when it is stopped; sync requests stay queued until it recovers. The isolated project (`airflow/pyproject.toml`) uses a dedicated PostgreSQL database (`${AIRFLOW_DB:-airflow}`), avoiding application migration collisions. The supervised single-host runtime exposes its UI only on loopback `127.0.0.1:${AIRFLOW_PORT:-8081}` (`read_only: true`, UID `10001`). API and execution JWT keys are persisted in the Airflow logs volume unless explicitly configured. The sync DAG starts unpaused; other schedules are opt-in:

- `trip_dbt_gold_pipeline` (`0 * * * *`): Discovers Silver GeoParquet objects, stages them locally, runs `dbt-duckdb`, atomically swaps `trip_analytics.duckdb` into `GOLD_OUTPUT_DIR`, and verifies mart parity.
- `trip_strava_incremental_sync` (`* * * * *`): Drains the durable Vault queue in bounded batches, resumes historical backfill and rate-limited jobs, and schedules incremental reconciliation every 30 minutes. Activities without GPS are retained; synchronization never creates projects or stages. Tokens remain out of XCom.
- `trip_platform_maintenance_and_audit` (`0 3 * * *`): Executes `reap_expired`, reconciles PostgreSQL `Stage`/`RenderJob` records against `ObjectStore`, and validates Silver GeoParquet schemas and metadata.

```bash
# Start just the orchestrator (also included in normal docker compose up)
make airflow-up

# Stop execution without removing services or volumes
make airflow-down

# Run required Airflow DAG integrity and task behavior tests
make test-airflow
make lint-airflow
```

### Export an MP4

In the editor’s **Export** tab, choose **Export MP4 locally (GPU)** to render the map on your device and download an MP4 without queuing a server job. This requires a secure context (HTTPS or localhost), accelerated WebGL, and WebCodecs support for H.264 at the chosen resolution and frame rate. GPU rendering does not guarantee hardware encoding: the browser may encode on the CPU.

Keep the editor’s Export panel and browser tab open until the download link appears. Local files are not added to server render history. You can still export on the server if local export is unavailable or fails.

For server-side hardware rendering, see [worker GPU setup](docs/OPERATIONS.md#worker-gpu-rendering).

## Configuration

Copy [`.env.example`](.env.example) to `.env` (`make bootstrap` does this when `.env` is absent) and set the values locally. Never commit `.env`. The full contract is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#5-configuration-contract-envexample).

| Area | Variables |
| :--- | :--- |
| Application runtime | `APP_ENV`, `LOG_LEVEL`, `DEV_AUTH_ENABLED`, `REGISTRATION_ENABLED`, `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`, `CADDY_TURNSTILE_CSP_ORIGIN` |
| Email verification and SMTP | `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_USE_TLS`, `EMAIL_VERIFICATION_TTL_HOURS` |
| Database (PostgreSQL 17) | `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL` |
| Security and encryption | `SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY`, `ALLOWED_ORIGINS` |
| Object storage (`local` or `s3`) | `OBJECT_STORE_BACKEND`, `LOCAL_OBJECT_STORE_DIR`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, `S3_REGION` |
| Strava OAuth2 | `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REDIRECT_URI` |
| Marked Trails overlay | `MARKED_TRAILS_BASE_URL` (empty turns the overlay off), publisher: `MARKED_TRAILS_STORE`, `MARKED_TRAILS_S3_BUCKET`, `MARKED_TRAILS_S3_PREFIX`, `MARKED_TRAILS_S3_REGION` |
| Google OIDC Sign-In | `GOOGLE_AUTH_CLIENT_ID`, `GOOGLE_AUTH_CLIENT_SECRET`, `GOOGLE_AUTH_REDIRECT_URI` |
| Map tiles and render worker | `MAP_TILE_ALLOWLIST`, `RENDER_BASE_URL`, `WORKER_CONCURRENCY`, `WORKER_GPU_ENABLED`, `WORKER_CPU_RENDERER`, `RENDER_BACKEND`, `RENDER_AWS_*`, `RENDER_CALLBACK_*`, `RENDER_REMOTE_*` |
| Apache Airflow 3 | `AIRFLOW_PORT`, `AIRFLOW_DB`, `AIRFLOW_FERNET_KEY`, `AIRFLOW_API_SECRET_KEY`, `AIRFLOW_JWT_SECRET`, `AIRFLOW_SIMPLE_AUTH_USERS`, `GOLD_OUTPUT_DIR` |
| Docker Compose and reverse proxy | `DOMAIN`, `COMPOSE_ALLOWED_ORIGINS`, `CADDY_HTTP_PUBLISH`, `CADDY_HTTPS_PUBLISH` |

Production settings (`APP_ENV=production`, `METRICS_TOKEN`, secret overrides) are described in [docs/OPERATIONS.md](docs/OPERATIONS.md#docker-compose-deployment).

## Testing

```bash
# Workspace targets
make help           # list all Makefile targets
make lint           # ruff check apps packages + eslint
make typecheck      # mypy --strict + tsc --noEmit
make test           # pytest (telemetry, object-store, api, worker, marked-trails; no docker marker) + vitest
make test-marked-trails-image # Docker test: build and run the Marked Trails builder image
make dbt-parse      # uv run dbt parse --project-dir dbt --profiles-dir dbt
make compose-config # docker compose -f infra/compose.yaml --env-file .env.example config --quiet

# Individual package checks
uv run pytest apps/api/tests -v
uv run pytest apps/worker/tests -v
uv run pytest packages/telemetry/tests -v
uv run pytest packages/object-store/tests -v
uv run pytest -m "not docker" packages/marked-trails/tests -v
uv run ruff check apps/api
uv run mypy apps/api
pnpm --filter @trip-animator/web build
```

Run `make check` for the full local gate (lint, format check, typecheck, tests, Airflow tests, dbt build, Compose config).

### Local worker benchmark

All three modes rendered the same project sample: 4 consecutive route stages and 11 photos, with a fit-track camera and cinematic photo stops. Each run used the same worker build, a fresh copy of the same warm tile cache, and a local Docker container limited to 2 vCPUs and pinned to the same two logical CPUs. Only GPU mode had access to the hardware GPU. This was a local benchmark, not a measurement on the deployed VPS.

| WebGL renderer | Wall time (one completed run per mode) |
| :--- | ---: |
| SwiftShader (CPU) | 304.82 s |
| Mesa Lavapipe (default CPU renderer) | 85.54 s |
| Intel UHD 620 (hardware Vulkan) | 20.67 s |

The WebGL2 backend was verified for each mode. Every export decoded successfully as a 21-second H.264 clip (315 frames, 720 × 900, 15 fps) and recorded 16 tile-cache hits with no misses. All 11 photos were visible in sampled frames from each clip. The benchmark supplied photo bytes from a local fixture, so it did not exercise production photo storage. Wall time includes rendering and FFmpeg encoding but excludes Chromium startup. GPU mode accelerates WebGL, not encoding. These are single-run results for this sample, not averages or estimates for the full project. Set `WORKER_CPU_RENDERER=swiftshader` to use the previous CPU renderer.

## Deployment

A VPS deployment exists.

- VPS (current deployment): the manual GitHub Actions workflow [`.github/workflows/deploy-vps.yml`](.github/workflows/deploy-vps.yml) (`workflow_dispatch`) connects to the VPS over SSH, checks out the requested Git ref, builds the Docker images, starts the Compose stack from `infra/compose.yaml` and verifies the health endpoint. See [docs/DEPLOY_VPS.md](docs/DEPLOY_VPS.md).
- Docker Compose stack: `infra/compose.yaml`, see [docs/OPERATIONS.md](docs/OPERATIONS.md#docker-compose-deployment).

## Docs index

- [SECURITY.md](SECURITY.md): vulnerability reporting, publication safety and implemented security controls.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): containers, telemetry pipeline, render pipeline and configuration contract.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): local setup, Strava OAuth setup, Compose deployment, observability and Airflow.
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): trust boundaries, threats and control matrix.
- [docs/DEPLOY_VPS.md](docs/DEPLOY_VPS.md): manual VPS deployment through GitHub Actions.
- [docs/decisions/0001-own-marked-trail-tiles.md](docs/decisions/0001-own-marked-trail-tiles.md): decision to build Marked Trail tiles and publish them to S3.
- [docs/decisions/0002-shared-object-store.md](docs/decisions/0002-shared-object-store.md): one shared object store package for the API and the worker.
- [docs/specs/ACTIVITY_LIBRARY_SPEC.md](docs/specs/ACTIVITY_LIBRARY_SPEC.md): Activity Library specification.
- [docs/specs/CINEMATIC_PHOTOS_SPEC.md](docs/specs/CINEMATIC_PHOTOS_SPEC.md): cinematic photo-stop specification.
- [docs/specs/MARKED_TRAILS_SPEC.md](docs/specs/MARKED_TRAILS_SPEC.md): Marked Trail overlay, tile build and publication specification.
- [docs/specs/OBSERVABILITY_SPEC.md](docs/specs/OBSERVABILITY_SPEC.md): self-hosted observability specification.
- [docs/specs/PUBLIC_LAUNCH_AND_CREDITS_SPEC.md](docs/specs/PUBLIC_LAUNCH_AND_CREDITS_SPEC.md): public launch, Google OIDC, Render Credits and Credit Voucher specification.
- [docs/specs/REMOTE_RENDER_BACKEND_SPEC.md](docs/specs/REMOTE_RENDER_BACKEND_SPEC.md): pluggable Render Backend and AWS Step Functions + Lambda specification.
- [GLOSSARY.md](GLOSSARY.md): Activity Library, trip playback, render execution and credits terminology.
- [docs/demo/generate_synthetic_gpx.py](docs/demo/generate_synthetic_gpx.py): generates a synthetic GPX demo track.

## Known gaps

- Not yet established (see [docs/OPERATIONS.md](docs/OPERATIONS.md#status)): real Strava OAuth behavior, manual FIT import, full-length 1080p export and terrain/3D rendering.
- PostgreSQL-backed tests skip when the test database (`TEST_POSTGRES_URL`) is unreachable, so a plain local `make test` does not exercise them.
- VPS deployment is manual (`workflow_dispatch`); there is no automatic deployment on merge.
- The AWS Terraform is only format-checked and validated in CI; it has never been applied.

## License

MIT, see [LICENSE](LICENSE).
