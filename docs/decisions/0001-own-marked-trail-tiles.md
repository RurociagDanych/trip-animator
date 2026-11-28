# 0001 Own Marked Trail tiles on S3

> Purpose: Record why Marked Trail tiles are built by Trip Animator and published to S3 instead of using a hosted tile provider.
> Audience: contributors, reviewers
> Last reviewed: 2026-11-28

Sections:

- Decision
- Considered options
- Consequences

## Decision

Waymarked hiking, cycling and MTB routes from OpenStreetMap are built into one PMTiles file covering Europe. The editor shows it as a per-project overlay on any map style, stored next to `map_style`. MP4 export fetches every tile of a film in headless Chromium. That bulk use conflicts with the fair-use and no-bulk policies of free tile servers, and per-tile quotas of keyed providers grow with each export. An own vector file stays sharp at 4K and makes renders independent of third-party availability.

The tileset is rebuilt by an Airflow DAG that an operator triggers manually, a few times a year. Marked Trails change slowly, and Airflow already orchestrates the platform's data, so a schedule, retry guards and a separate build host are not worth their cost yet. The DAG has no schedule and allows one active run, so it cannot start by itself or run twice at once. The build may later move to ECS Fargate with an Airflow schedule, so the build code must not depend on the app host.

Builds are published to a private S3 bucket in us-east-1 and read through CloudFront by the editor and by the render Lambdas. Renders run in Lambda in the same region and serve the app from a local server inside the function, so a same-origin path on the VPS would not reach them. S3 also keeps tile traffic and finished builds off the VPS.

Each build is written once under its own version prefix, and a small `current.json` pointer selects the active version. Switching versions overwrites only the pointer, because S3 has no atomic rename. The current and previous builds are kept for rollback. The previous build is deleted when a new build starts, so the bucket never holds more than two builds. A render job stores the build id when it is enqueued, so a new build cannot change a film that was already previewed. If the pinned build was deleted meanwhile, the render uses the current build instead of failing. The tile base URL comes from runtime configuration.

The build is a standalone CLI using osmium and Planetiler from a dedicated builder image. The Airflow image builds the same profile from the same pinned Planetiler image, installs osmium and Java from Debian, and runs the CLI in process. On AWS the builder image can run as a Fargate task. Lambda does not fit, because its 15 minute and 10 GB limits are too small for the Europe extract.

## Considered options

- Waymarked Trails raster overlay: keyless, but fair use only and blurry in high-resolution video.
- Mapy.com, Thunderforest, Tracestrack: keyed, per-tile quotas, API key exposed in the browser.
- OpenTopoMap and openmaps.fr: non-commercial only, no bulk download, and the OpenTopoMap server is being reduced.
- Serving from the VPS through Caddy: Lambda renders would fetch tiles from the VPS over the internet, and the VPS disk would hold the builds.
- Serving from the RustFS bucket: would expose the object store and need CSP changes.
- Scheduled build in GitHub Actions with an rsync push: needs SSH deploy secrets, and instances would depend on the project's CI.
- Scheduled job on the app host: a monthly download of the Europe extract and guard logic against repeated failed builds, for data that rarely changes.
- Launching a separate build container from Airflow: requires the Docker socket, which is root on the host.

## Consequences

- Production needs an S3 bucket, a CloudFront distribution with origin access control and CORS for `Range` requests from the app origin, and an IAM identity for Airflow that can write only under the trail prefix. The CloudFront host is added to `CADDY_TILE_CSP_ORIGINS` and to the render `MAP_TILE_ALLOWLIST`.
- The trail store has an S3 implementation for production and a local disk implementation for development and tests only.
- The Europe extract (35.6 GB) does not fit on the VPS, so the build downloads, filters and deletes one Geofabrik country at a time and merges the small results. Peak disk is the largest country plus the outputs, about 10 GB.
- Build containers are capped at one CPU and 3 GB of memory, because the VPS has two vCPUs and no swap. A build takes an estimated 2 to 4 hours and slows the editor and the Strava sync meanwhile; the operator picks the time.
- The build uses its own Airflow pool with one slot, so it never takes both LocalExecutor slots from the Strava sync.
- Tiles stop at z13 and MapLibre overzooms above it, and local networks start at z9, to stay within a 1 GiB budget. A prototype measurement of Poland and Germany alone gave 304 MB at z14. If the full build still exceeds 1 GiB, the budget rises to 2 GiB.
- A build replaces the active version only if its size is within the budget and its size and trail count are within 20% of the previous build.
- Map attribution "(c) OpenStreetMap contributors" (ODbL) is always shown in the editor and in exported films, together with the trail data date while the overlay is on. It also covers the OpenFreeMap basemap, which needed it already.
- Projects store the enabled Marked Trail types in one list field; an empty list means the overlay is off, which is the default.
- Without a published build the trail toggles are disabled and renders still succeed.
- Coverage outside Europe needs a rebuild with a larger extract. Europe means the children of the Geofabrik `europe` extract without its overlapping special regions; Russia and Kaliningrad are not covered.
