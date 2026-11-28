# Marked Trails

> Purpose: Specify the Marked Trail overlay, its tile build and publication, and its use in the editor and in rendered films.
> Audience: contributors, reviewers.
> Last reviewed: 2026-11-28.

- Problem Statement
- Solution
- User Stories
- Implementation Decisions
- Testing Decisions
- Out of Scope
- Further Notes

Status: Draft. Decisions are recorded in [ADR 0001](../decisions/0001-own-marked-trail-tiles.md). Delivered so far: the Airflow DAG `trip_marked_trails_build` that builds Europe country by country (see [OPERATIONS.md](../OPERATIONS.md#marked-trails-build-in-airflow)), the hiking tracer bullet (local build with `make marked-trails-local`, `marked_trail_types` in render settings, the hiking layer in the editor through the local store), the publication policy in the CLI (two-build limit, size and count validation, failure keeps the pointer; rollback in [OPERATIONS.md](../OPERATIONS.md)), and the cycling and MTB types with full trail styling (per-type layers with signposted colours, line width by network level, trail labels from z13, dashed cycling and MTB lines). The three toggles are shown only when `MARKED_TRAILS_BASE_URL` is set; it is empty by default, so production shows no toggle until the render ticket lands. Until then, export and shared playback ignore the setting. The S3 trail store, the CLI selection between local and S3 and the Terraform for the bucket, CloudFront and publisher policy are written but not applied. Everything else in this spec is not implemented. Build cost was measured once with a prototype on the production VPS (Poland and Germany only); see Further Notes.

## Problem Statement

Trip films show the user's route on OpenFreeMap basemaps. Those basemaps draw paths and tracks but not signposted hiking, cycling or MTB trails, so a hiking or bike trip film cannot show which Marked Trail the user followed or what trail network surrounds the route. Hosted outdoor tile providers either forbid the bulk tile fetching that MP4 export does, charge per tile, expose an API key in the browser, or render raster tiles that blur in 4K video.

## Solution

Trip Animator builds its own vector tileset of Marked Trails in Europe from OpenStreetMap and publishes it to S3 behind CloudFront. In the editor the user switches on hiking, cycling and MTB Marked Trails per project, on top of any map style. The trails appear under the user's route in the preview, in shared playback and in the exported film, which shows the same trail data version as the preview. An operator refreshes the data a few times a year by triggering one Airflow DAG.

## User Stories

1. As a trip author, I want to switch on hiking Marked Trails for a project, so that my hiking film shows the trails I walked.
2. As a trip author, I want to switch on cycling and MTB Marked Trails independently of hiking trails, so that a bike trip shows only the networks that matter for it.
3. As a trip author, I want the overlay to work with every map style, so that I do not have to change the style I chose.
4. As a trip author, I want new and existing projects to start with the overlay off, so that my films do not change until I opt in.
5. As a trip author, I want trail lines in their signposted colours, so that I recognise the trails from the terrain.
6. As a trip author, I want international and national trails drawn more prominently than local ones, so that the map stays readable when zoomed out.
7. As a trip author, I want local trails hidden at low zoom, so that wide views do not turn into a coloured smear.
8. As a trip author, I want trail numbers or names shown only when zoomed in, so that labels do not clutter overview shots.
9. As a trip author, I want cycling and MTB trails drawn with a different line pattern from hiking trails, so that I can tell them apart in the film.
10. As a trip author, I want Marked Trails drawn beneath my route and semi-transparent, so that my own route stays the focus of the film.
11. As a trip author, I want the exported film to show the same trails as the preview, so that what I approved is what I get.
12. As a trip author, I want a film queued before a data refresh to keep the trail version it was previewed with, so that a refresh cannot change it.
13. As a trip author, I want the render to still succeed if its pinned trail version was removed, so that a long wait in the queue never fails my export.
14. As a trip author, I want trails to stay sharp in 4K exports, so that the film looks professional.
15. As a viewer of a shared trip, I want to see the same Marked Trails as the author chose, so that the shared playback matches the film.
16. As a trip author, I want the map attribution visible in the editor and in the film, so that the film meets the OpenStreetMap licence.
17. As a trip author, I want to see the date of the trail data while the overlay is on, so that I know how current the trails are.
18. As a trip author on an instance without published trail data, I want the trail toggles disabled with a short explanation, so that I understand why they do nothing.
19. As a trip author on such an instance, I want renders to succeed without trails, so that missing data never blocks an export.
20. As an operator, I want to refresh the trail data by triggering one Airflow DAG, so that I do not need extra tools or hosts.
21. As an operator, I want the DAG to never start on its own and never run twice at once, so that the VPS is not loaded unexpectedly.
22. As an operator, I want to choose the region when triggering the DAG, with Europe as the default, so that I can build a small region for testing.
23. As an operator, I want a build to replace the published data only when it passes validation, so that a broken build never reaches users.
24. As an operator, I want a failed build to leave the published data unchanged, so that the editor and renders keep working.
25. As an operator, I want at most two builds stored at any time, so that storage does not grow.
26. As an operator, I want to roll back to the previous build by changing one pointer, so that recovering from bad data is fast and needs no rebuild.
27. As an operator, I want the build to fit on the VPS disk, so that it does not endanger the app's data.
28. As an operator, I want the build capped in CPU and memory, so that the app keeps running on a host without swap.
29. As an operator, I want the build to use its own Airflow pool, so that the Strava sync keeps a free slot.
30. As an operator, I want build logs and metrics in Airflow, such as duration, sizes and trail count, so that I can judge each build.
31. As a contributor, I want to build a small region locally with one command, so that I can develop the overlay without a full build.
32. As a contributor, I want the build to be a standalone CLI in its own image, so that it can later run as an ECS Fargate task with an Airflow schedule.

## Implementation Decisions

### Marked Trail data

- Source: OpenStreetMap route relations tagged `route=hiking`, `route=foot`, `route=bicycle` or `route=mtb`, from Geofabrik extracts.
- Kept attributes: the trail type (hiking, cycling, MTB, with foot folded into hiking), `network` level (international, national, regional, local), `colour`, `ref` and `name`.
- Full `osmc:symbol` rendering is out of scope; `colour` with a per-type default is enough.
- Tiles cover z5 to z13; MapLibre overzooms above z13. Local networks start at z9, and so do relations without a recognised `network` tag. Names and refs are present from z13.
- One PMTiles file per build, with separate trail types so the client can filter them.

### Build pipeline

- A new Python package provides a build-and-publish CLI. Its inputs are a region (a Geofabrik region path, default Europe) and a trail store.
- Geofabrik extracts are processed one country at a time: download, filter with osmium `tags-filter` (without omitting referenced objects, so the input must be on disk), delete the extract, then merge the filtered files and run Planetiler with a custom profile. Peak disk is the largest country (France, 5.1 GB) plus the outputs, about 10 GB; a build does not start with less than 12 GiB free.
- Coverage of `europe`: the children listed in the Geofabrik index except the `special_regions` of `europe` (extracts that overlap their neighbours, read from the index), plus the federal districts of European Russia, which Geofabrik files under `russia` (`EXTRA_CHILDREN` in `regional.py`; Kaliningrad is inside the northwestern district). Asian Russia is out of scope.
- All children must come from one OpenStreetMap snapshot: their `osmosis_replication_sequence_number` values must be equal. Older children are downloaded once more; if the numbers still differ the run fails and publishes nothing.
- osmium and Planetiler come from a dedicated builder image for the local make target and AWS. The Airflow image builds the same Planetiler profile from the same pinned Planetiler image in a build stage and installs osmium and a Java runtime from Debian, and the DAG runs the CLI in process. Airflow does not get the Docker socket.
- Tool processes are capped at one CPU and 3 GB of memory and are killed with the task that started them.
- The publisher AWS keys are `MARKED_TRAILS_AWS_*` variables loaded only by the Airflow service from `.env.marked-trails` and used only by the trail store client.
- The Airflow DAG has no schedule, `max_active_runs=1`, a region parameter and its own pool with one slot.
- A make target runs the same CLI locally against a small region and the local trail store.

### Publication

- Store layout: one immutable prefix per build, named by build date with an optional numeric suffix for a later build on the same day (`2026-11-27-2`), holding the PMTiles file, plus a `current.json` pointer at the root.
- `current.json` holds at least the current and previous build ids, the OpenStreetMap data date, and the checksum, size and trail count (`marked_trail_count`, the number of route relations in the build) of the current build. Readers ignore unknown keys, and fields added later have defaults.
- Order of a build: check that `current.json` names an existing build, delete every other build, build the new one, validate it, upload it under its own prefix, then overwrite `current.json`. Build ids are never reused. The store never holds more than two builds.
- Validation before switching runs on the local file before it is uploaded: the file is not empty, has at least one Marked Trail relation, is at most 1 GiB, and its size and trail count are within 20% of the current build. The first build is checked only against the size limit. After the upload the stored object size must equal the local size. If the full Europe build exceeds 1 GiB with z13 and the local-network zoom cut, the limit rises to 2 GiB.
- A failed build or failed validation leaves `current.json` unchanged and fails the DAG run.
- Build files are written with a long immutable cache lifetime; `current.json` is written with no caching.
- The trail store uses the shared object store package (ADR 0002) for its S3 and local implementations. S3 is the only production target; the local implementation serves development and tests.

### AWS infrastructure

- A private S3 bucket in us-east-1, the region of the render Lambdas, so tile reads from renders stay in-region.
- A CloudFront distribution with origin access control, serving the bucket to the editor and to the render Lambdas, with CORS that allows `Range` requests from the app origin.
- An IAM identity for Airflow that can write and delete only under the trail prefix.
- The CloudFront host is added to the edge CSP tile origins and to the render tile allowlist.
- Infrastructure is added to the existing Terraform, in the `infra/terraform/standalone-remote-render` root; setup steps are in [Operations](../OPERATIONS.md#marked-trails-on-aws).

### Project settings and API

- The render specification gains `marked_trail_types`, a list of trail types with an empty default that means the overlay is off. Map settings already live in the render specification stored on the project, so no table migration is needed.
- Field and identifier names always say `marked_trail`, never a bare `trail`, because `trail_duration_seconds` already uses "trail" for the visible tail of the user's route.
- The API exposes the trail base URL and the current pointer to the editor through runtime configuration, so the browser never hard-codes the CloudFront host.
- When a render is enqueued, the API reads `current.json` and stores the current build id and trail base URL in the render bundle. Without a published build, the bundle carries no trail data and the render proceeds without trails.

### Editor, shared playback and render

- MapLibre reads PMTiles through the `pmtiles` protocol.
- The map style builder injects the Marked Trail source and layers, the same way it injects hillshade and satellite sources today, and places them under the route layers.
- The inspector shows three toggles. They are disabled with a short explanation when no build is published.
- Map attribution "(c) OpenStreetMap contributors" is always visible in the editor, in shared playback and burned into exported films, with the trail data date while the overlay is on.
- The render route loads the pinned build from the bundle. If that build no longer exists, it loads the current build.

## Testing Decisions

Good tests here check external behaviour at the highest seam: what a build publishes, what the API returns and stores, and what the map style and the rendered page contain. They do not assert on internal helper calls.

Proposed seams, to be confirmed:

1. **Build-and-publish CLI.** Publication policy (pointer, validation, two-build limit, failure keeps the pointer) is tested through the CLI with the local trail store and a fake build step. One integration test runs the real builder image on a tiny extract fixture and checks the published file and pointer; it is skipped without Docker, like the `postgres` tests.
2. **API.** Settings round-trip of `marked_trail_types`, rejection of unknown types, and render enqueue pinning the current build or carrying no trail data without a build. Prior art: the existing project settings and render API tests, including the PostgreSQL-marked ones.
3. **Map style builder.** Given trail types and a build, the built style contains the trail source and layers below the route, for every map style preset; without a build it contains none. Prior art: the existing map style and timeline unit tests in the web app.
4. **Editor.** Toggles, their disabled state without a build, and the attribution with the data date. Prior art: the web app smoke test.
5. **Render.** The render bundle carries the pinned build, and a missing pinned build falls back to the current one. Prior art: the render manifest and Lambda handler tests.
6. **DAG.** DAG integrity: no schedule, one active run, the dedicated pool. Prior art: the existing Airflow DAG integrity test.
7. **Infrastructure.** Terraform formatting and validation in CI.

## Out of Scope

- Coverage outside Europe.
- Full `osmc:symbol` rendering.
- A scheduled build, and running the build on ECS Fargate. The design keeps both possible.
- Serving trail tiles from the VPS in production.
- Other overlays or basemaps, such as Waymarked Trails rasters or keyed outdoor styles.

## Further Notes

- Prototype measurement on the production VPS (2 vCPU, 8 GB RAM, no swap, 33 GB free), Poland and Germany, z5 to z14: download 2.0 GB and 4.7 GB at 4 to 33 MB/s; osmium about 100 s per GB on one CPU with about 2.4 GB of memory; filtered data 45 MB and 242 MB; Planetiler 7.5 minutes with up to 2.3 GB of memory; output 304 MB. Load average reached about 6 during the run.
- Estimates for Europe, not measured: 2 to 4 hours per build and a file of 0.8 to 1.6 GB before the zoom cuts. The first full build has to confirm the size budget.
- Estimated AWS cost at current traffic: below $1 per month for S3 storage and requests, with CloudFront inside its free tier. A future Fargate build would cost about $0.20 per run in a public subnet without a NAT gateway.
