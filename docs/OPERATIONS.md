# Operations

> Purpose: Explain how to set up, run, integrate and maintain a self-hosted Trip Animator deployment.
> Audience: operators
> Last reviewed: 2026-11-24

Sections:

- Status
- Local development setup
- Strava OAuth2 setup
- Docker Compose deployment

Deployment, integration and maintenance notes for self-hosting Trip Animator.

## Status

Active, self-hosted. You operate the application, database, object storage, backups, TLS and any external integrations.

Deployment: the VPS is deployed with the manual GitHub Actions workflow described in [DEPLOY_VPS.md](DEPLOY_VPS.md): it connects over SSH, checks out the requested Git ref, builds the Docker images, starts the Compose stack from `infra/compose.yaml` and verifies the health endpoint.

Exercised in local QA: GPX/TCX ingestion, share/playback, the mobile header, and a five-second 1280×720 flat-mode MP4 render.

Not yet established: real Strava OAuth behavior, manual FIT import, full-length 1080p export, terrain/3D rendering. Render cost and success depend on the host and map-tile availability.

## Local development setup

### Prerequisites
- Python 3.12+ with [`uv`](https://docs.astral.sh/uv/)
- Node.js 24+ with [`pnpm`](https://pnpm.io/) v11
- Docker Engine with the Docker Compose plugin (the bootstrap starts this repo's PostgreSQL service)
- `ffmpeg` and Playwright Chromium are optional; install them only if running the local render worker

### Bootstrap

From the repository root, run:

```bash
make bootstrap
```

This runs `scripts/bootstrap-local.sh`: it creates `.env` from `.env.example` only when `.env` is absent, installs the locked Python and JavaScript dependencies, starts the Compose PostgreSQL service, waits for its health check, and applies Alembic migrations. It does not start the API, web app, object store, or render worker. Review `.env` locally before starting services; do not share it.

Bootstrap is local-only: it refuses `APP_ENV=production`, non-loopback database hosts, URL query/fragment options, and libpq host/service overrides. Existing process variables take precedence over `.env`. Set `POSTGRES_PORT` and the port in `DATABASE_URL` together when the default local PostgreSQL port is occupied.

Create a local user account (or use dev-login when `DEV_AUTH_ENABLED=true` in `APP_ENV=development`):

```bash
uv run python -m trip_api.cli create-user \
  --email rider@example.com \
  --password "correct-horse-battery-staple" \
  --display-name "Alpine Rider"
```

### Start services

Run each in a separate terminal:

```bash
# Terminal 1: FastAPI backend (:8000)
make dev-api

# Terminal 2: Vite React frontend (:5173, proxies /api to :8000)
make dev-web

# Terminal 3 (optional, for MP4 rendering): Render worker
uv run python -c "from trip_worker.runner import run_worker_loop; run_worker_loop()"
```

## Strava OAuth2 setup

1. In your [Strava API Application settings](https://www.strava.com/settings/api), set **Authorization Callback Domain** to:
   - `localhost` for local development or local Docker Compose, or
   - your production domain (e.g., `trips.example.com`).
2. Configure `.env`:
   - `STRAVA_CLIENT_ID=<your-client-id>`
   - `STRAVA_CLIENT_SECRET=<your-client-secret>`
   - `STRAVA_REDIRECT_URI`:
     - Local Vite dev: `http://localhost:5173/strava/callback`
     - Docker Compose (Caddy HTTP): `http://localhost/strava/callback`
     - Production HTTPS: `https://<your-domain>/strava/callback`
   - Optional webhook verification token: `STRAVA_VERIFY_TOKEN=<random-secret>`

> Note: The SPA route `/strava/callback` receives the Strava redirect and invokes `GET /api/v1/strava/callback` using your authenticated session cookie.

## Docker Compose deployment

`infra/compose.yaml` orchestrates PostgreSQL, RustFS (under the service name `minio`), initialization and migrations, the API, ingestion, the render worker, the web app, Caddy, and Airflow.

```bash
# Validate Compose configuration
make compose-config

# Build and launch the full stack
docker compose -f infra/compose.yaml --env-file .env up -d --build
```

For production (`APP_ENV=production`), ensure `.env` overrides all secrets (`POSTGRES_PASSWORD`, `SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `METRICS_TOKEN`), sets `DEV_AUTH_ENABLED=false`, and sets `DOMAIN` + `ALLOWED_ORIGINS` for Caddy automatic TLS.

### Worker GPU rendering

The worker defaults to software WebGL2 using Mesa Lavapipe (`WORKER_CPU_RENDERER=lavapipe`). The worker image includes the driver. For a native Linux worker, install `mesa-vulkan-drivers` on Debian or Ubuntu, or the equivalent Mesa Vulkan package providing a Lavapipe ICD under `/usr/share/vulkan/icd.d`. To use SwiftShader instead, set `WORKER_CPU_RENDERER=swiftshader` in `.env` and restart the native worker or recreate the Compose worker. Look for `Worker CPU renderer:` in the logs.

`WORKER_GPU_ENABLED=true` takes precedence over `WORKER_CPU_RENDERER` and enables hardware WebGL2 through Chromium ANGLE/Vulkan on a compatible Linux host. These settings do not change local browser exports or FFmpeg encoding (`libx264` on the CPU). If Chromium cannot use the requested renderer, the worker reports an error instead of silently switching backends.

For a native worker, set `WORKER_GPU_ENABLED=true` in the repository root `.env` and run the worker as a user with access to the host's Vulkan render device and drivers. For Docker Compose, use a Linux host with a Mesa Vulkan-capable DRM render node, such as a compatible Intel or AMD GPU. Set these values in `.env`:

- `WORKER_GPU_ENABLED=true`
- `WORKER_GPU_DEVICE=/dev/dri/renderD128` (choose the appropriate host render node if different)
- `WORKER_GPU_RENDER_GID` to the numeric group ID printed by `stat -c '%g' /dev/dri/renderD128` (use the same device path as above)

From the repository root, start the stack with the GPU override. If the stack is already running, add `--no-deps worker` to the `up` command to rebuild and recreate only the worker.

```bash
docker compose --env-file .env -f infra/compose.yaml -f infra/compose.worker-gpu.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml -f infra/compose.worker-gpu.yaml up -d --build
docker compose --env-file .env -f infra/compose.yaml -f infra/compose.worker-gpu.yaml logs worker
```

Submit a server render and look for `Worker GPU renderer:` in the logs. The override grants only the worker access to the selected render node. The standard VPS workflow does not apply it; keep the flag disabled there unless the deployment is configured to pass through a compatible GPU. To return to CPU rendering, set `WORKER_GPU_ENABLED=false`, omit the override and recreate the worker.

### Observability and retention reaper

The optional monitoring stack starts **off**. To enable it locally, start the application first, set `METRICS_TOKEN` in `.env` (recreate the API if you changed the token), and create an owner-readable-only `secrets/grafana_admin_password` file containing a unique password. Set `OBSERVABILITY_ENABLED=true` in `.env` and run `infra/scripts/apply-observability.sh`. The VPS workflow applies this setting after the application health check; monitoring failures do not fail the application deployment. Keep it off on the VPS until the storage budget and safeguards are verified.

Grafana uses the `admin` account on first launch. Open `http://127.0.0.1:3001` locally, or use `ssh -N -L 3001:127.0.0.1:3001 user@vps` before opening the same URL. Prometheus has no published port. The **Trip Animator — API and renders** dashboard shows scrape status, database health, and render queue depth. Unavailable queue data is not rendered as zero. Prometheus keeps up to 15 days, with a 1 GB retention target for stored blocks; the WAL and head can use additional space. No history backup or alerts are configured.

The **Trip Animator — API HTTP** dashboard shows 4xx/5xx responses per second and p95 time to response headers by route template, over a five-minute window. Requests without a matched route are grouped as `unmatched`; scrapes of `/metrics` are excluded. Client-side failures and errors after response headers (such as interrupted streams) are not measured. The dashboard needs multiple scrapes and does not replace missing data with zero.

Set `OBSERVABILITY_ENABLED=false` and rerun the same script to stop only monitoring and retain its data. Do not use `--remove-orphans` with the base file alone when monitoring is enabled: include `-f infra/compose.observability.yaml` so Compose recognizes its containers. This slice does not collect logs yet.

- **Liveness**: `GET /health` → `200 {"status":"ok","service":"trip-api","env":"..."}`
- **Readiness**: `GET /ready` → `200` when `SELECT 1` succeeds against PostgreSQL, `503` otherwise
- **Metrics**: `GET /metrics` (internal `api:8000/metrics`, blocked at Caddy edge, Bearer-gated by `METRICS_TOKEN`, zero PII)
- **Retention Reaper**: Prunes expired `sessions`, expired `oauth_states`, stale `rate_limits`, and terminal `render_jobs` + ObjectStore `.mp4`/`.webp` artifacts past `RENDER_RETENTION_DAYS` (default `7`):

```bash
# Dry-run preview
uv run python -m trip_api.cli reap --dry-run --json

# Execute prune
uv run python -m trip_api.cli reap
```

In Docker Compose, run the reaper on a host cron or systemd timer:

```bash
docker compose -f infra/compose.yaml exec -T api python -m trip_api.cli reap
```

### Apache Airflow 3 Orchestration

Airflow is included in the default Compose stack and required for Strava Vault synchronization. Library browsing and GPX import do not depend on Airflow; sync requests remain queued while it is unavailable. `airflow/pyproject.toml` is a standalone project, and `airflow-init` provisions a dedicated PostgreSQL database (`${AIRFLOW_DB:-airflow}`) to avoid `alembic_version` collisions. The UI is exposed on loopback `127.0.0.1:${AIRFLOW_PORT:-8081}` (`read_only: true`, UID `10001`).

- `trip_dbt_gold_pipeline` (`0 * * * *`): Discovers Silver GeoParquet objects, stages them locally, runs `dbt-duckdb`, atomically swaps `trip_analytics.duckdb` into `GOLD_OUTPUT_DIR`, and verifies mart parity.
- `trip_strava_incremental_sync` (`* * * * *`): Drains the durable Vault queue in bounded batches, resumes historical backfill and rate-limited jobs, and schedules incremental reconciliation. Activities without GPS are retained; synchronization does not create projects or stages, and tokens are not placed in XCom.
- `trip_platform_maintenance_and_audit` (`0 3 * * *`): Executes `reap_expired`, reconciles PostgreSQL `Stage`/`RenderJob` records against `ObjectStore`, and validates Silver GeoParquet schemas and metadata.

```bash
# Start the stack, including required Strava sync orchestration
make airflow-up

# Run Airflow DAG integrity + pure-Python task tests
make test-airflow
make lint-airflow
```

See [SECURITY.md](../SECURITY.md) before exposing a deployment to a network.
