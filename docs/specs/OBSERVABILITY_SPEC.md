# Observability Specification

> Purpose: Define what operators need to diagnose Trip Animator using centralized logs and metrics.
> Audience: operators, contributors, reviewers.
> Last reviewed: 2026-11-26

Sections:

- Confirmed direction
- Existing system
- Design and acceptance scenarios
- Release acceptance
- References

Status: implemented in Docker Compose; optional and off by default (`OBSERVABILITY_ENABLED=false`). Production VPS activation remains operator-controlled.

## Confirmed direction

- Q1: Add Grafana, Loki, Alloy, and Prometheus to the production Docker Compose VPS, using the same stack for local verification. Leave AWS and CloudWatch unchanged.
- Q2: The first release covers the application as a whole. Render troubleshooting is one acceptance scenario, not the release boundary.
- Q3: Grafana is operator-only, accessed over an SSH tunnel with Grafana login and no public endpoint. Loki, Prometheus, Alloy, and internal exporters remain private.
- Q5: Cover Caddy and web serving, API, ingestion, the render worker, and Airflow. Include basic health signals for PostgreSQL, RustFS, and the VPS; defer detailed dependency and host telemetry.
- Q6: Include an optional synthetic browser smoke check for editor startup. Do not collect error reports from real users' browsers in the first release.
- Q7: Run all observability components and checks on the same VPS. No off-host monitoring in the first release. A total VPS outage can make both the application and Grafana unavailable and leave a monitoring gap.
- Q8 (supersedes Q4): Use Grafana for viewing dashboards, check results, and logs on demand. Do not configure alert rules or automated notifications, including Telegram, in the first release.
- Q9: Cover API, worker, and ingest logs in Loki and API, HTTP, ingest, render-queue, internal service probe, disk headroom, and synthetic editor metrics in Prometheus, visible in private Grafana dashboards and Explore.
- Q10: Target 7 days of logs and 15 days of metrics. Do not back up observability history. Protect the application with a separate disk budget even if history is lost or shortened. Validate the numeric budget against VPS capacity before production activation.
- Q11: The application must keep serving requests, importing, and rendering when the observability stack is down. Accept bounded gaps. Display missing or stale signals as unknown, not zero or healthy.
- Q12: Run the browser check through normal sign-in using a dedicated non-admin account and a private synthetic project. Verify editor readiness without writing data on each run or retaining screenshots and browser traces.
- Q13 (updated): Collect application container stdout/stderr logs (`api`, `worker`, `ingest`, `web`, `caddy`, `airflow`, `postgres`, `minio`) through Alloy over a read-only Docker socket (`source="container"`), scoped to the active Compose project and excluding monitoring containers to prevent feedback loops. Alongside container logs, collect explicitly emitted reviewed operational events over internal UDP (`source="operational"`) for `api` (1514), `worker` (1515), and `ingest` (1516), including startup and five-minute heartbeats so Loki labels remain populated even on quiet deployments. Measure HTTP errors and latency by route template, and show depth and oldest queued age for job queues without interpreting persisted row counts as event counters.
- Q14: Control monitoring with one flag in the deployment `.env`, off by default. Applying the deployment with the flag on starts monitoring; applying it with the flag off stops only monitoring. Do not require an application rollback or delete observability data just to disable the stack.

## Existing system

- The documented self-hosted VPS deployment uses Docker Compose. It includes Caddy and a web service, FastAPI (`api`), a separate `ingest` worker and `worker` render process, Airflow, PostgreSQL, and RustFS (`minio`). AWS Terraform already includes CloudWatch logging and queue monitoring.
- The FastAPI service exposes an internal `/metrics` endpoint. Caddy blocks it at the public edge; production requires a bearer token (`METRICS_TOKEN`). It reports database health, persisted render-job counts and oldest queued age, ingest queue depth and oldest queued age, and HTTP error rate / latency histograms by route template.
- The render worker emits structured JSON to stdout with allowlisted context fields (`job_id`, `chunk_id`, `stage`, `status`, `duration_ms`, `rss_mb`) and redaction rules for common credential formats.

## Design and acceptance scenarios

- Use single-binary Loki, Grafana Alloy, and Prometheus with persistent local storage, and provision both data sources (`Trip Loki`, `Prometheus`) and dashboards in Grafana from version-controlled configuration.
- Mount `/var/run/docker.sock:ro` only on the optional `alloy` container so enabling or disabling observability never requires recreating application containers or changing Docker's default logging driver. Restrict `discovery.docker` + `discovery.relabel` to the active Compose project's application services and exclude `loki`, `alloy`, `grafana`, `prometheus`, `service-probes`, `disk-metrics`, and `editor-probe`.
- Promote only bounded low-cardinality Loki labels (`service`, `source`, `stream`, `level`, and `event`) via Alloy `loki.process`. Keep per-job and per-request fields (`job_id`, `route`, `status`, `outcome`) inside the JSON log body for LogQL `| json` filtering rather than index labels.
- For an unavailable or slow application, distinguish an edge or frontend-serving failure from API errors or latency, and show the health of critical dependencies. For a stalled import or data refresh, distinguish ingest backlog or failure from a failed Airflow job. For a failed render, show queue state and trace worker container logs and operational outcomes by render `job_id`.
- Configure Loki retention explicitly (`168h`) and bound ingestion (`1 MB/s`, `4 MB` burst); age-based retention and rate limits do not enforce a hard disk quota. Validate the disk headroom guard independently before production activation.
- Operator toggle: `OBSERVABILITY_ENABLED=true` enables monitoring; `false` or unset disables it. Apply both states through `infra/scripts/apply-observability.sh`. Editing `.env` alone does not change running services.

## Release acceptance

- Grafana dashboards and Explore cover public-site and web-serving health, API availability, HTTP error rate and latency, ingest and render queue depth and age, Airflow health, PostgreSQL and RustFS health, Docker volume disk headroom, optional synthetic editor startup, and live Loki logs with immediately available labels (`service`, `source`, `stream`, `level`, `event`).
- In local Compose smoke verification (`infra/scripts/smoke-observability.sh`), confirm Loki labels and container logs exist before injecting failures; exercise API failures, worker and ingest operational events, synthetic editor sign-in/cleanup, and controlled dependency outages so missing or stale signals never look healthy.
- Verify that monitoring endpoints are not public, that persistence survives monitoring restarts, and that disabling monitoring stops only observability containers without restarting the API or deleting monitoring volumes. Confirm no alert rules or notification destinations are configured.
- Before production activation, verify VPS disk headroom under full container log volume and confirm losing the monitoring stack does not block normal operations.

## References

- [Trip Animator operations](../OPERATIONS.md#observability-and-retention-reaper)
- [Loki deployment modes](https://grafana.com/docs/loki/latest/get-started/deployment-modes/)
- [Loki label cardinality](https://grafana.com/docs/loki/latest/get-started/labels/cardinality/)
- [Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)
- [Docker Engine security](https://docs.docker.com/engine/security/)
- [Docker Compose profiles](https://docs.docker.com/compose/how-tos/profiles/)
- [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
- [Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/)
