# Operations

> Purpose: Explain how to set up, run, integrate and maintain a self-hosted Trip Animator deployment.
> Audience: operators
> Last reviewed: 2026-11-28

Sections:

- Status
- Local development setup
- Strava OAuth2 setup
- Docker Compose deployment
- Quotas, scaling, and VPS backups
- Marked Trails build in Airflow (under Apache Airflow 3 Orchestration)

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

### Marked Trails local build

The editor's hiking Marked Trails overlay reads a published trail store. To build one locally, run:

```bash
make marked-trails-local
```

The target builds the builder image (osmium, Planetiler and the `trip-marked-trails` CLI), downloads the Geofabrik extract of `MARKED_TRAILS_REGION` (default `europe/andorra`), verifies its md5, and publishes the PMTiles build and `current.json` into `.tmp/marked-trails/store`. The tools are capped at one CPU and 3 GB of memory. A second build on the same day gets a numeric suffix (`2026-11-27-2`); `current.json` keeps the previous build id. To build a local `.osm.pbf` instead of downloading, run the image with `build-publish --extract <path>`.

`make dev-web` serves the store at `/tiles/marked-trails/`, and `make dev-api` sets `MARKED_TRAILS_BASE_URL` to that path unless the environment sets it; `.env.example` leaves it empty. With `MARKED_TRAILS_BASE_URL` empty, the editor shows no Marked Trails toggle. `make test-marked-trails-image` runs the Docker test of the builder image on a committed fixture; `make test` skips it.

### Marked Trails on AWS

Production serves the trail store from a private S3 bucket behind CloudFront. Both come from `infra/terraform/standalone-remote-render`, the root that deploys the render Lambdas. The bucket must be in `us-east-1` and the root rejects another region, but the Makefile default is `eu-central-1`, so deploy with `make remote-render-deploy REMOTE_RENDER_AWS_REGION=us-east-1` (`terraform.tfvars.example` also sets `us-east-1`). The Marked Trails resources are created only when `marked_trails_app_origins` is not empty; without it the deploy is the render stack alone. A cloud build needs these steps, in order:

1. Set `marked_trails_app_origins` (default empty, which creates nothing) in that root's `terraform.tfvars` to the app origins that read the tiles in the browser, for example `["https://trips.example.com"]`. The CloudFront response headers policy answers only those origins, allows `GET` and `HEAD` with `Range`, and exposes `Content-Range`, `ETag` and `Accept-Ranges`. `marked_trails_prefix` (default `marked-trails`) is the key prefix of the store.
2. Review `terraform plan`, then `terraform apply`. Read the outputs: `marked_trails_bucket_name`, `marked_trails_base_url`, `marked_trails_cloudfront_origin` and `marked_trails_publisher_policy_arn`.
3. Attach the policy of `marked_trails_publisher_policy_arn` to the IAM user or role that Airflow uses; put that user's keys in `.env.marked-trails` as described in [Marked Trails build in Airflow](#marked-trails-build-in-airflow). It allows get, put, delete and abort-multipart on keys under the prefix and `ListBucket` only with the prefix condition. Existence checks of the CLI list the full key with `MaxKeys` 1 instead of sending a HEAD, so no unprefixed list permission is needed.
4. Add the CloudFront origin (`https://<id>.cloudfront.net`, from `marked_trails_cloudfront_origin`) to the render tile allowlist and to the edge CSP tile origins, keeping the origins that are already there:
   - Lambda renders: append it to `map_tile_allowlist` in the Terraform tfvars (comma-separated, for example `https://tiles.openfreemap.org,https://<id>.cloudfront.net`), then apply.
   - VPS renders: append it to `MAP_TILE_ALLOWLIST` in `.env`.
   - Edge CSP: append it, space-separated, to `CADDY_TILE_CSP_ORIGINS` in `.env` (read by `infra/Caddyfile` through `infra/compose.yaml`) and recreate the `caddy` service.
5. Set `MARKED_TRAILS_BASE_URL` in `.env` to `marked_trails_base_url` (it already ends in the prefix and serves `current.json`) and recreate the API.
6. Publish with the CLI, selecting the S3 store by environment or flags. Credentials come from the default AWS chain (`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, or a role), never from flags:

```bash
export MARKED_TRAILS_STORE=s3
export MARKED_TRAILS_S3_BUCKET=<marked_trails_bucket_name>
export MARKED_TRAILS_S3_PREFIX=marked-trails
export MARKED_TRAILS_S3_REGION=us-east-1
trip-marked-trails build-publish --region europe/andorra --work-dir /work
```

`MARKED_TRAILS_S3_PREFIX` must equal `marked_trails_prefix`. Build files are uploaded as `application/vnd.pmtiles` with `public, max-age=31536000, immutable`; `current.json` is uploaded as `application/json` with `no-cache, no-store, must-revalidate`. The bucket has no expiration rule: only the publisher deletes builds. Bucket versioning is off, so a deleted or overwritten object cannot be restored from the bucket. CloudFront serves `current.json` with the managed CachingDisabled policy and the builds with CachingOptimized. A key that does not exist answers 403 through CloudFront, not 404, because the bucket policy grants CloudFront only `s3:GetObject` and no `s3:ListBucket`.

### Marked Trails publication policy and rollback

`build-publish` follows one order: check that `current.json` is valid (its `current_build_id` is a build id, `pmtiles_key` is the key of that build, and the object exists), delete every build except the current one, build the new PMTiles file, validate the local file, upload it under `builds/<build id>/`, then overwrite `current.json`. An invalid `current.json` stops the run before anything is deleted. Build ids are never reused: a later build on the same date gets the next numeric suffix above every id in the store and in the pointer. The store never holds more than two builds, the current one and the previous one kept for rollback. Validation requires a non-empty file with at least one Marked Trail relation, at most `--size-budget-bytes` (environment `MARKED_TRAILS_SIZE_BUDGET_BYTES`, default 1073741824, 1 GiB; empty means the default, zero or below is rejected) and, when a current build exists, its size and `marked_trail_count` to be within 20% of the current build. The first build is checked only against the budget. After the upload the stored size must equal the local size. A failed build, failed validation or failed upload leaves `current.json` unchanged and the command exits with status 1.

The previous build is deleted when a new build starts, so after a failed run `previous_build_id` in `current.json` can name a build that no longer exists. Rollback is only possible while the previous build is in the store, which is right after a successful run.

To roll back, check that `builds/<previous_build_id>/marked-trails.pmtiles` exists in the store, then edit `current.json`:

1. Set `current_build_id` to the old `previous_build_id`, and set `pmtiles_key` to `builds/<that build id>/marked-trails.pmtiles` (it must match `current_build_id`, or the next run refuses to prune), `size_bytes`, `sha256`, `osm_data_date` and `marked_trail_count` to that build's values.
2. Set `previous_build_id` to the build being rolled back from, or to `null`. Never change or remove `last_build_id`: it is the highest build id ever allocated, and the next build id is allocated above it. Do not set `previous_build_id` to `null` on a pointer that has no `last_build_id` (written by an older version); first set `last_build_id` to the highest build id that existed.
3. Write the file back with `Cache-Control: no-cache`. For an S3 store use `aws s3 cp current.json s3://<marked_trails_bucket_name>/<marked_trails_prefix>/current.json --content-type application/json --cache-control "no-cache, no-store, must-revalidate"`; the content type and cache control are replaced by the upload, so pass both. The next `build-publish` run then deletes every build except the new current one.

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

Grafana uses the `admin` account on first launch. Open `http://127.0.0.1:3001` locally, or use `ssh -N -L 3001:127.0.0.1:3001 user@vps` before opening the same URL. Prometheus has no published port. The **Trip Animator — API and renders** dashboard shows scrape status, database health, render queue depth, and the age of the oldest queued render measured from job creation. The age is zero when the database confirms the queue is empty. Unavailable queue data is not rendered as zero. Prometheus keeps up to 15 days, with a 1 GB retention target for stored blocks; the WAL and head can use additional space. No history backup or alerts are configured.

The **Trip Animator — internal service health** dashboard probes Caddy and the web service for the React application shell, RustFS for readiness, and Airflow for scheduler, DAG processor, and metadata database health. A failed probe is `0`; an unavailable exporter or stale sample is unknown (no data). These probes do not verify public DNS/TLS, editor startup, or DAG run outcomes. The probe exporter has no published port.

The same dashboard shows free bytes on the filesystem backing default local Docker volumes. It reads a dedicated empty volume, without access to application data, the Docker socket, or host mounts. This is a VPS disk headroom signal, not a storage cap; it does not measure volumes placed on separate filesystems.

The **Trip Animator — ingest queue** dashboard shows the number of persisted jobs in `queued` status and the age of the oldest one since creation. Running and completed jobs are excluded. A confirmed empty queue reports zero; stale or unavailable measurements are not shown as zero.

The **Trip Animator — API HTTP** dashboard shows 4xx/5xx responses per second and p95 time to response headers by route template, over a five-minute window. Requests without a matched route are grouped as `unmatched`; scrapes of `/metrics` are excluded. Client-side failures and errors after response headers (such as interrupted streams) are not measured. The dashboard needs multiple scrapes and does not replace missing data with zero.

The optional **Synthetic editor startup** panel uses a dedicated verified non-admin account and a private project owned by that account. Provision both once and leave project sharing off. Save a JSON object with `email`, `password`, `project_id`, and `project_title` in `secrets/editor_probe.json` beside `.env`. Keep the host `secrets/` directory private (`0700`); give the file read access to container GID `10001` (for example, group `10001` and mode `0440` with standard Docker UID/GID mapping). Compose bind-mounts the file, preserving its host permissions. Apply monitoring again after creating, changing, or removing the file. Without it the probe stops and its panel shows no data. The probe uses the worker image for Chromium, without starting a render worker or mounting application volumes. In production, set `EDITOR_PROBE_ORIGIN` to the application's bare HTTPS origin. The probe connects to Caddy over the Compose edge and still verifies its certificate. The local HTTP default is `http://caddy`. The probe signs in and out normally, which creates the usual auth audit events. It does not edit the project or retain screenshots or traces.

Set `OBSERVABILITY_ENABLED=false` and rerun the same script to stop only monitoring and retain its data. Do not use `--remove-orphans` with the base file alone when monitoring is enabled: include `-f infra/compose.observability.yaml` so Compose recognizes its containers.

In Grafana Explore, select **Trip Loki**. Alloy collects stdout/stderr logs from application containers (`api`, `worker`, `ingest`, `web`, `caddy`, `airflow`, `postgres`, `minio`) over a read-only Docker socket (`source="container"`), scoped to the Compose project and excluding monitoring containers to avoid feedback loops. It also receives reviewed operational UDP events on ports `1514` (`api`), `1515` (`worker`), and `1516` (`ingest`) (`source="operational"`), including startup and five-minute heartbeats so labels stay populated even on quiet deployments. Alloy promotes only low-cardinality labels (`service`, `source`, `stream`, `level`, and `event`) and keeps `job_id`, `route`, `status`, and `outcome` inside the JSON body. Use `{service="api"} | json` to inspect API server errors by route template, `{service="worker"} | json | job_id="…"` to trace a render attempt, `{service="ingest"}` for Strava ingest runs, or `{source="container",level="error"}` for container errors. The internal UDP transport is best effort: missing events do not imply success. Loki retains logs for seven days and rate-limits ingestion (`1 MB/s`, `4 MB` burst), which is not a hard disk quota.

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

In Docker Compose, the `ingest` service runs periodic retention reaping every hour (`--reap-interval-s 3600`). You can also trigger an immediate pass manually:

```bash
docker compose -f infra/compose.yaml exec -T api python -m trip_api.cli reap
```

## Quotas, scaling, and VPS backups

- **Per-user quotas** (configured in `.env`): `MAX_PROJECTS_PER_USER=25`, `MAX_STAGES_PER_PROJECT=30`, `MAX_ACTIVE_RENDERS_PER_USER=1` (alongside `MAX_PHOTOS_PER_PROJECT=100`).
- **Server Render caps**: `SERVER_RENDER_MAX_DURATION_S=120.0`, `SERVER_RENDER_MAX_FPS_LOCAL=30`, and `SERVER_RENDER_MAX_FPS_REMOTE=60`. Local browser export remains uncapped by these server limits.
- **AWS Lambda concurrency ceiling**: `RENDER_REMOTE_MAX_INFLIGHT=3` throttles the remote outbox dispatcher across API workers. Pair this with `reserved_concurrent_executions = 24` (`3 x 8`) in `infra/terraform/standalone-remote-render/terraform.tfvars`.
- **Multi-worker API & SSE scaling**: `UVICORN_WORKERS=2` (default in `infra/compose.yaml`) runs multiple FastAPI workers safely behind Caddy. Open render SSE streams share one in-process PostgreSQL `LISTEN render_job_events` connection per API worker.
- **PostgreSQL backups**: Schedule `scripts/backup-vps.sh` via host cron or systemd timer to capture `pg_dump --format=custom` archives, prune local files older than `BACKUP_RETENTION_DAYS` (default `14`), and optionally sync to S3/R2/B2 via `BACKUP_S3_URI` (and optional `BACKUP_S3_ENDPOINT_URL`):

```bash
# Dry-run check
./scripts/backup-vps.sh --dry-run

# Run backup immediately
BACKUP_DIR=/var/backups/trip-animator BACKUP_RETENTION_DAYS=14 ./scripts/backup-vps.sh
```

Verify backups periodically by restoring into a scratch PostgreSQL instance with `pg_restore --clean --if-exists`.

### Apache Airflow 3 Orchestration

Airflow is included in the default Compose stack and required for Strava Vault synchronization. Library browsing and GPX import do not depend on Airflow; sync requests remain queued while it is unavailable. `airflow/pyproject.toml` is a standalone project, and `airflow-init` provisions a dedicated PostgreSQL database (`${AIRFLOW_DB:-airflow}`) to avoid `alembic_version` collisions. The UI is exposed on loopback `127.0.0.1:${AIRFLOW_PORT:-8081}` (`read_only: true`, UID `10001`).

- `trip_dbt_gold_pipeline` (`0 * * * *`): Discovers Silver GeoParquet objects, stages them locally, runs `dbt-duckdb`, atomically swaps `trip_analytics.duckdb` into `GOLD_OUTPUT_DIR`, and verifies mart parity.
- `trip_strava_incremental_sync` (`* * * * *`): Drains the durable Vault queue in bounded batches, resumes historical backfill and rate-limited jobs, and schedules incremental reconciliation. Activities without GPS are retained; synchronization does not create projects or stages, and tokens are not placed in XCom.
- `trip_platform_maintenance_and_audit` (`0 3 * * *`): Executes `reap_expired`, reconciles PostgreSQL `Stage`/`RenderJob` records against `ObjectStore`, and validates Silver GeoParquet schemas and metadata.
- `trip_marked_trails_build` (no schedule): Builds and publishes the Marked Trails PMTiles; see [Marked Trails build in Airflow](#marked-trails-build-in-airflow).

```bash
# Start the stack, including required Strava sync orchestration
make airflow-up

# Run Airflow DAG integrity + pure-Python task tests
make test-airflow
make lint-airflow
```

#### Marked Trails build in Airflow

`trip_marked_trails_build` refreshes the Marked Trails data. It never starts on its own (no schedule, `catchup=False`), allows one active run (`max_active_runs=1`) and has one task, `build_and_publish`, in the Airflow pool `marked_trails_build` with one slot. `airflow-init` creates the pool on every start with `airflow pools set`, which updates an existing pool, so no manual step is needed. The task runs the `trip-marked-trails build-publish` command in process; Airflow has no Docker socket. The Airflow image installs `osmium-tool` and `openjdk-25-jre-headless` from Debian and compiles the Planetiler profile in its own build stage from the same digest-pinned Planetiler image as the builder image (`infra/docker/Dockerfile.marked-trails-builder`), so `docker compose up -d --build` needs no other image; a test keeps the two digests equal. The Debian osmium (1.18) can differ from the builder image's (1.19) by a minor version. The image build runs `osmium --version`, `java -version` and `prlimit --version`, so a broken toolchain fails the build.

Prerequisites on the host:

- Set `MARKED_TRAILS_STORE=s3`, `MARKED_TRAILS_S3_BUCKET`, `MARKED_TRAILS_S3_PREFIX` and `MARKED_TRAILS_S3_REGION` in `.env`, and copy `marked-trails.env.example` to `.env.marked-trails` with the keys of the publisher policy from [Marked Trails on AWS](#marked-trails-on-aws): `MARKED_TRAILS_AWS_ACCESS_KEY_ID`, `MARKED_TRAILS_AWS_SECRET_ACCESS_KEY` and, for temporary credentials, `MARKED_TRAILS_AWS_SESSION_TOKEN`. Only the `airflow` service loads that file (every service loads `.env`, so the keys must not go there), and the CLI gives them only to the trail store client, never to other AWS clients as `AWS_ACCESS_KEY_ID`. Without the file the default AWS chain applies. Never commit the file; it is ignored by git. The Airflow container mounts no local store, so production uses `s3`.
- Keep at least 12 GB free for the Docker volume `marked_trails_work`, mounted at `/work/marked-trails`. Airflow's root filesystem is read-only and `/tmp` is a 512 MB tmpfs, so the build writes only to that volume. The largest extract is France at about 5.1 GB (Germany is 4.9 GB, Poland 2.1 GB); with the filtered files, the Planetiler working files and the PMTiles output (0.8 to 1.6 GB before the zoom cuts) the peak is about 10 GB. The task checks the free space of the volume first and fails under 12 GiB before it downloads anything. The 35.6 GB Europe extract is never downloaded: Geofabrik lists the countries of Europe and each is downloaded, filtered and deleted before the next.

To run it, unpause the DAG once (new DAGs start paused), then trigger it from the Airflow UI with the parameter `region`, or from the host:

```bash
docker compose -f infra/compose.yaml --env-file .env exec airflow airflow dags unpause trip_marked_trails_build
docker compose -f infra/compose.yaml --env-file .env exec airflow airflow dags trigger trip_marked_trails_build --conf '{"region": "europe"}'
```

`region` defaults to `europe`. Use `europe/andorra` (a region without children) for a quick check of the setup in a few minutes; it is built directly and published like any other build. The parameter accepts only lowercase Geofabrik paths such as `europe` or `europe/germany`.

Expect 2 to 4 hours for Europe; this is an estimate from the Poland and Germany measurement, not a measured Europe run (see [the spec](specs/MARKED_TRAILS_SPEC.md#further-notes)). Which extracts are built: every child of `europe` in the Geofabrik index except those named in the `special_regions` property of `europe` in that index (`alps`, `britain-and-ireland`, `dach`, `great-britain` at the time of writing), which overlap their neighbours; the list is read from the index on each run. Russia, including Kaliningrad, is not built: Geofabrik files it under `russia`, not under `europe`, and the owner decided against adding it. The build covers exactly the children of `europe` minus those special regions, about 49 extracts.

The extracts are downloaded hours apart and Geofabrik refreshes them daily, so two children can hold different versions of the same relation. The `osmosis_replication_sequence_number` is a counter per region and is not compared. The filtered files are merged with `osmium merge --with-history` and `osmium time-filter` then keeps only the newest version of each object, so each relation appears once with its current members. The `osmosis_replication_timestamp` of every child is logged with the spread between the oldest and the newest; `osm_data_date` in `current.json` is the oldest one, and a spread of more than 2 days only logs a warning and never fails the run. An object deleted between two snapshots can survive from the older extract; this is accepted. The task has an eight hour timeout and no retries, so a failure never silently starts a second multi-hour run. A failed country, a failed download (md5 mismatch) or a failed validation fails the run and leaves `current.json` unchanged; see [publication policy](#marked-trails-publication-policy-and-rollback). For a parent region the scratch files of a failed run are deleted by the run itself, and by the next run if the container was killed.

While it runs the host is busier. Each tool process (osmium, Planetiler) is started under `nice -n 10`, pinned to one CPU with `taskset` and capped at 3 GB of data memory with `prlimit --data`; Planetiler's heap is limited to 2 GB and its direct memory to 512 MB. The `airflow` service has no `--cpus` or `--memory` limit of its own and the unprivileged task cannot create cgroups, so the caps are per process limits; RLIMIT_AS is not used because it would also count the JVM's reserved address space. Each tool also runs under `setpriv --pdeathsig KILL`, so the kernel kills it when the Airflow task process dies and no multi-hour tool is left running. `AIRFLOW__CORE__PARALLELISM` is 3 (it was 2): the build holds one task slot for hours, and with two slots the minutely Strava sync would take the other one, so the hourly dbt run and the daily maintenance DAG would wait for the whole build. The editor and the Strava sync still share the other CPU and the 8 GB: expect slower API responses and later Strava syncs during a build, and a downloaded country at 4 to 33 MB/s keeps the network busy. Trigger it when the editor is quiet. If a tool exceeds the memory cap it fails with an allocation error and the run fails. The 3 GiB cap rests on these measurements of data memory (`VmData`, what `prlimit --data` counts) under the cap, with osmium 1.19: `tags-filter` on the 553 MB Switzerland extract peaks at 2105 MiB (2151 MiB without the queue limits), and Planetiler was measured at 2.3 GB. `osmium merge` grows with the number of inputs, 48 to 90 MiB each: 56 inputs of 45 MB ran out of memory at 3071 MiB. So the build merges at most 8 files at a time into intermediate `.osh.pbf` files, merges those the same way, and runs `time-filter` once; merged inputs are deleted at once. Measured with 56 inputs of 45 MB (2.5 GB in total) under the same caps, a batch of 8 peaks at 387 MiB, the final merge of 7 at 346 MiB and `time-filter` at 152 MiB. osmium runs with `OSMIUM_MAX_INPUT_QUEUE_SIZE=2` and `OSMIUM_MAX_OSMDATA_QUEUE_SIZE=2`, because its read-ahead queues are the per-input growth. A full Europe build has not been run, so the largest country extract (France, Germany) under `tags-filter` is the least measured step. Logs of each tool are in the task log; the last line reports the build id, size and Marked Trail count.

See [SECURITY.md](../SECURITY.md) before exposing a deployment to a network.
