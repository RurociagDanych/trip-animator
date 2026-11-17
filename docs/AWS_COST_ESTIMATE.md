# Trip Animator — Monthly AWS Cost Estimate (`eu-central-1` Frankfurt)

> [!NOTE]
> **Approximate Cost Estimate Disclaimer**
> All figures below are **approximate monthly estimates in USD** based on public AWS **`eu-central-1` (Frankfurt)** On-Demand and Spot reference pricing (assuming a **730-hour month**). Actual bills vary with real-time Fargate Spot rates, GPX/video file sizes, user egress patterns, AWS Free Tier eligibility, and Savings Plans / Reserved Instance commitments.

---

## 1. Reference Unit Rates (`eu-central-1` Frankfurt)

| AWS Service / Resource | Approximate Reference Unit Rate | Billing Notes |
| :--- | :--- | :--- |
| **ECS Fargate (`ARM64` Graviton, On-Demand)** | **$0.03725** / vCPU-hr + **$0.00409** / GiB-hr | ~20% cheaper than `x86_64` (`$0.04656`/vCPU-hr + `$0.00511`/GiB-hr). |
| **ECS Fargate Spot (`ARM64` Graviton)** | ~**$0.01118** / vCPU-hr + **$0.00123** / GiB-hr | Assumes ~70% Spot discount off `ARM64` On-Demand (`2 vCPU/4 GiB` ≈ `$0.02726/hr`; `4 vCPU/8 GiB` ≈ `$0.05452/hr`). |
| **RDS PostgreSQL 17 (`db.t4g.micro`, Single-AZ)** | **$0.0192** / hr (~$14.02/mo) | 2 vCPU, 1 GiB RAM (Graviton2). |
| **RDS PostgreSQL 17 (`db.t4g.small`, Multi-AZ)** | **$0.0640** / hr (~$46.72/mo) | 2 vCPU, 2 GiB RAM (`$0.0320/hr` Single-AZ × 2 for Multi-AZ). |
| **RDS PostgreSQL 17 (`db.r6g.large`, Multi-AZ)** | **$0.5262** / hr (~$384.13/mo) | 2 vCPU, 16 GiB RAM (`$0.2631/hr` Single-AZ × 2 for Multi-AZ). |
| **RDS `gp3` Storage** | **$0.119** / GB-mo (Single-AZ) · **$0.238** / GB-mo (Multi-AZ) | Automated backups up to 100% of provisioned DB storage are included at $0; extra backup storage ~$0.095/GB-mo. |
| **Application Load Balancer (ALB)** | **$0.0225** / hr + **$0.005** / hr IPv4 + **~$0.0088** / LCU-hr | Base uptime ≈ **$20.07/mo** + ~$6.42/mo per average LCU. |
| **VPC NAT Gateway** | **$0.052** / hr + **$0.005** / hr IPv4 + **$0.052** / GB processed | Base uptime ≈ **$41.61/mo per NAT GW**. **S3 Gateway VPC Endpoint = $0/hr, $0/GB**. |
| **Amazon CloudFront** | **$0.085** / GB egress (first 10 TB) + **$0.012** / 10k HTTPS req | Origin fetches from S3 → CloudFront = **$0/GB**. *(AWS Always Free includes 1 TB + 10M req/mo if eligible; PAYG shown below).* |
| **AWS WAFv2 (`CLOUDFRONT`)** | **$5.00** / WebACL + **$1.00** / rule + **$0.60** / 1M requests | Managed rules count as 1 rule each (4–5 rules typical = $9–$10/mo base). |
| **Amazon S3 (`eu-central-1`)** | **$0.0245** / GB-mo Standard · **$0.0135** / GB-mo Standard-IA | Requests ~$0.005/1k PUT/LIST, ~$0.0004/1k GET. |
| **Secrets Manager / KMS / ECR** | **$0.40** / secret · **$1.00** / KMS CMK · **$0.10** / GB ECR | KMS requests ~$0.03/10k after 20k/mo free tier (S3 Bucket Key cuts KMS calls ~99%). |
| **CloudWatch Logs & Alarms** | **$0.50** / GB ingested + **$0.03** / GB-mo stored + **$0.10** / alarm | Assumes 14–30 day log retention. |
| **Amazon MWAA (`mw1.small`)** | **$0.49** / hr (~$357.70/mo) + **~$0.055** / extra worker-hr + **$0.10** / GB meta DB | Always-on managed Airflow (web server + scheduler + 1 base worker; no scale-to-zero). |

---

## 2. Executive Comparison Across 4 Scenarios

| Scenario | Traffic & Render Profile | Core Database & Compute | Monthly Total (Est.) |
| :--- | :--- | :--- | :---: |
| **Scenario 1: Portfolio / Demo** | ~50 renders/mo, low web traffic | `db.t4g.micro` (Single-AZ), 1× `0.5 vCPU/1 GiB` API, `0..2` Spot workers, **Zero-NAT** *(or +$41.87 with 1 NAT GW)* | **~$67.86/mo** *(~$109.73 w/ NAT)* |
| **Scenario 2: Early SaaS** | ~1,000 MAU, 2,000 renders/mo, 150 GB egress | `db.t4g.small` (**Multi-AZ**), 2× `1 vCPU/2 GiB` API, `0..6` Spot workers, 1 NAT GW | **~$237.66/mo** |
| **Scenario 3: Growth / High Load** | ~10,000 MAU, 20,000 renders/mo, 2 TB egress | `db.r6g.large` (**Multi-AZ**), 2–8× `2 vCPU/4 GiB` API (avg 4), `0..20` Spot workers (`4 vCPU/8 GiB`), 2 NAT GWs | **~$1,206.77/mo** |
| **Scenario 4: + Amazon MWAA (`mw1.small`)** | Scenario 2 or 3 + Managed Airflow 3 DAGs | Replaces EventBridge + ECS RunTask (~$0.52–$1.69/mo) with always-on `mw1.small` (+$362.67/mo gross) | **+$360.98 to +$362.15/mo delta** |

---

## 3. Itemized Scenario Breakdowns

### Scenario 1 — Portfolio / Demo / Low Traffic
*Assumptions: ~50 renders/mo (~3 min avg = 2.5 worker-hours/mo), scale-to-zero workers, ~10 GB CloudFront egress, 200k HTTPS requests, Single-AZ `db.t4g.micro`, S3 Gateway VPC Endpoint ($0).*

| Service | Sizing & Usage Calculation | Monthly Est. |
| :--- | :--- | :---: |
| **ECS Fargate (`api`)** | 1 × `0.5 vCPU / 1 GiB` ARM64 × 730 hrs *(note: `$33.16` if `1 vCPU / 2 GiB`)* | $16.58 |
| **ECS Fargate Spot (`worker`)** | `2 vCPU / 4 GiB` ARM64 (`$0.02726/hr`), scale `0..2`, 2.5 hrs/mo | $0.07 |
| **Scheduled ECS `RunTask`** | Daily `reap` + optional `dbt-gold` (`0.5 vCPU / 1 GiB` Spot, ~2.5 hrs/mo) | $0.02 |
| **RDS PostgreSQL 17** | `db.t4g.micro` Single-AZ (`$14.02`) + 20 GB `gp3` (`$2.38`) | $16.40 |
| **Application Load Balancer** | 1 ALB base (`$20.07`) + ~0.1 LCU avg (`$0.64`) | $20.72 |
| **CloudFront** | 10 GB (`$0.85`) + 200k HTTPS req (`$0.24`) *($0 if AWS Free Tier eligible)* | $1.09 |
| **AWS WAFv2** | 1 WebACL (`$5.00`) + 4 rules (`$4.00`) + 0.2M req (`$0.12`) *(optional in dev)* | $9.12 |
| **VPC Networking** | S3 Gateway Endpoint (`$0.00`) + Zero-NAT mode (`enable_nat_gateway = false`) | $0.00 |
| **Amazon S3 (`web` + `objects`)** | ~4 GB Standard (`$0.10`) + ~1 GB IA (`$0.01`) + requests (`$0.09`) | $0.20 |
| **Amazon ECR** | ~3 GB container images (`api`, `worker`, `airflow`) | $0.30 |
| **Secrets Manager + KMS** | 2 secrets (`$0.80`) + 1 KMS CMK (`$1.00`) | $1.80 |
| **CloudWatch Logs & Alarms** | 2 GB ingested/stored (`$1.06`) + 5 alarms (`$0.50`) | $1.56 |
| **Total (Zero-NAT Portfolio)** | *(Drops to **~$57.65/mo** if WAFv2 omitted & CloudFront Free Tier applies)* | **$67.86/mo** |
| *Optional Private-Subnet Dev (+1 NAT GW)* | *Adds 1 × Single NAT GW (`$41.61`) + 5 GB tile/Strava NAT traffic (`$0.26`)* | ***$109.73/mo*** |

---

### Scenario 2 — Early SaaS / Active Community
*Assumptions: ~1,000 MAU, 2,000 renders/mo (~3 min avg = 100 worker-hours/mo), 2× API Fargate (`1 vCPU / 2 GiB`), Multi-AZ `db.t4g.small`, Single NAT Gateway (cost-optimized HA trade-off) + S3 Gateway Endpoint, 150 GB CloudFront egress, 5M HTTPS requests.*

| Service | Sizing & Usage Calculation | Monthly Est. |
| :--- | :--- | :---: |
| **ECS Fargate (`api`)** | 2 × `1 vCPU / 2 GiB` ARM64 across 2 AZs × 730 hrs (`2 × $33.16`) | $66.33 |
| **ECS Fargate Spot (`worker`)** | `2 vCPU / 4 GiB` ARM64 Spot (`$0.02726/hr`), autoscale `0..6`, 100 hrs/mo | $2.73 |
| **Scheduled ECS `RunTask`** | Daily `reap` + hourly `dbt-gold` + 30m `strava-sync` (`1 vCPU / 2 GiB` Spot, ~38 hrs/mo) | $0.52 |
| **RDS PostgreSQL 17 (Multi-AZ)** | `db.t4g.small` Multi-AZ (`$46.72`) + 50 GB `gp3` Multi-AZ (`$11.90`) | $58.62 |
| **Application Load Balancer** | 1 ALB base (`$20.07`) + ~0.5 LCU avg (`$3.21`) | $23.29 |
| **VPC Networking** | 1 × Single NAT GW (`$41.61`) + 50 GB tile/Strava traffic (`$2.60`) + S3 Gateway EP (`$0`) | $44.21 |
| **CloudFront** | 150 GB egress (`$12.75`) + 5M HTTPS req (`$6.00`) | $18.75 |
| **AWS WAFv2** | 1 WebACL (`$5.00`) + 4 rules (`$4.00`) + 5M req (`$3.00`) | $12.00 |
| **Amazon S3 (`web` + `objects`)** | ~30 GB Standard (`$0.74`) + ~20 GB Bronze IA (`$0.27`) + ~35 GB 7d `outputs/` (`$0.86`) + reqs (`$0.45`) | $2.31 |
| **Amazon ECR** | ~8 GB stored across repositories | $0.80 |
| **Secrets Manager + KMS** | 2 secrets (`$0.80`) + 1 KMS CMK (`$1.00`) | $1.80 |
| **CloudWatch Logs & Alarms** | 10 GB ingested/stored (`$5.30`) + 10 alarms (`$1.00`) | $6.30 |
| **Total (Early SaaS)** | *(Add **+$41.61/mo** for 2nd NAT GW if strict AZ-level NAT redundancy required)* | **$237.66/mo** |

---

### Scenario 3 — Growth / High Load
*Assumptions: ~10,000 MAU, 20,000 renders/mo (~2 min avg on 4 vCPU = 666.7 worker-hours/mo), autoscaling API 2–8 tasks (avg 4 tasks active at `2 vCPU / 4 GiB`), Multi-AZ `db.r6g.large`, 2× Multi-AZ NAT Gateways + S3 Gateway Endpoint, 2,000 GB CloudFront egress, 50M HTTPS requests.*

| Service | Sizing & Usage Calculation | Monthly Est. |
| :--- | :--- | :---: |
| **ECS Fargate (`api`)** | Avg 4 × `2 vCPU / 4 GiB` ARM64 × 730 hrs *(range: $132.66 @ 2 tasks – $530.62 @ 8 tasks)* | $265.31 |
| **ECS Fargate Spot (`worker`)** | `4 vCPU / 8 GiB` ARM64 Spot (`$0.05452/hr`), autoscale `0..20`, 666.7 hrs/mo | $36.35 |
| **Scheduled ECS `RunTask`** | Daily `reap` + hourly `dbt-gold` + 30m `strava-sync` (`2 vCPU / 4 GiB` Spot, ~62 hrs/mo) | $1.69 |
| **RDS PostgreSQL 17 (Multi-AZ)** | `db.r6g.large` Multi-AZ (`$384.13`) + 200 GB `gp3` Multi-AZ (`$47.60`) + 100 GB extra backups (`$9.50`) | $441.23 |
| **Application Load Balancer** | 1 ALB base (`$20.07`) + ~3.0 LCU avg (`$19.27`) | $39.35 |
| **VPC Networking** | 2 × Multi-AZ NAT GWs (`$83.22`) + 400 GB tile/Strava traffic (`$20.80`) + S3 Gateway EP (`$0`) | $104.02 |
| **CloudFront** | 2,000 GB egress (`$170.00`) + 50M HTTPS req (`$60.00`) | $230.00 |
| **AWS WAFv2** | 1 WebACL (`$5.00`) + 5 rules (`$5.00`) + 50M req (`$30.00`) | $40.00 |
| **Amazon S3 (`web` + `objects`)** | ~200 GB Silver/Gold (`$4.90`) + ~300 GB Bronze IA (`$4.05`) + ~350 GB 7d `outputs/` (`$8.58`) + reqs (`$5.00`) | $22.52 |
| **Amazon ECR** | ~15 GB stored across repositories | $1.50 |
| **Secrets Manager + KMS** | 2 secrets (`$0.80`) + 1 KMS CMK (`$1.00`) + extra KMS API calls (`$0.30`) | $2.10 |
| **CloudWatch Logs, Metrics & Alarms** | 40 GB ingested/stored (`$21.20`) + 15 alarms/metrics (`$1.50`) | $22.70 |
| **Total (Growth / High Load)** | *(1-yr RDS RI + Compute Savings Plan reduces steady-state compute/DB by ~25–35%)* | **$1,206.77/mo** |

> **S3 Gateway Endpoint Savings Proof (Scenario 3)**: At 20,000 renders/mo (~40 MB avg MP4 + Silver Parquet reads/writes ≈ ~2.5 TB/mo internal S3 traffic), routing S3 via the **S3 Gateway VPC Endpoint** instead of NAT Gateways directly avoids **~$130/mo** (`2,500 GB × $0.052/GB`) in NAT processing fees alone.

---

### Scenario 4 — Full Data Engineering Stack (`+ Amazon MWAA`)
Scenario 4 compares three orchestration options for `airflow/dags/` (`trip_dbt_gold_pipeline`, `trip_strava_incremental_sync`, `trip_platform_maintenance_and_audit`):

| Orchestration Component | Option A: EventBridge + ECS `RunTask` *(Default)* | Option B: Self-Managed `airflow` Container on ECS | Option C: Managed **Amazon MWAA (`mw1.small`)** |
| :--- | :---: | :---: | :---: |
| **Control Plane / Scheduler** | EventBridge Scheduler (`$0` within 14M free invocations/mo) | 1 × `1 vCPU / 2 GiB` Fargate On-Demand (`$33.16/mo`) | `mw1.small` always-on (730h × `$0.49/hr` = **$357.70/mo**) |
| **Task Execution / Workers** | Fargate Spot `RunTask` (`$0.52`–`$1.69/mo`) | Included in single `LocalExecutor` container (`$0`) | Included base worker + ~20 extra worker-hrs (`$1.10/mo`) |
| **Metadata DB & Storage** | None (stateless runs; audit in app RDS) | Shared app RDS (`airflow` DB, `$0` extra compute) | Managed metadata storage (10 GB = `$1.00/mo`) + DAG S3 (`$0.02/mo`) |
| **Logs & Monitoring** | Covered in ECS CloudWatch Logs | `~$1.50/mo` extra CloudWatch Logs | ~5 GB MWAA Logs + 2 alarms (**$2.85/mo**) |
| **Gross Orchestration Cost** | **$0.52 – $1.69/mo** | **~$34.66/mo** | **$362.67/mo** |
| **Net Delta vs Option A** | Baseline | **+$33 to +$34/mo** | **+$360.98 to +$362.15/mo** |

- **Scenario 2 + Amazon MWAA (`mw1.small`)**: `$237.66 - $0.52 + $362.67` = **`$599.81/mo`** (**+$362.15/mo delta**).
- **Scenario 3 + Amazon MWAA (`mw1.small`)**: `$1,206.77 - $1.69 + $362.67` = **`$1,567.75/mo`** (**+$360.98/mo delta**).
- **Architectural Recommendation**: Start with **Option A (EventBridge Scheduler + ECS `RunTask`)** for Scenarios 1–3 because `airflow/src/trip_airflow/` exposes pure-Python entrypoints without Airflow worker overhead. Upgrade to **Option C (Amazon MWAA)** only when multiple data engineers require managed Airflow UI visibility, DAG backfills, SLA alerting, and multi-DAG upstream sensors that justify the ~$361/mo fixed floor.

---

## 4. Cost-Optimization Checklist

- [x] **Fargate Spot (`FARGATE_SPOT`) for Workers & Batch Tasks**: Cuts Playwright + FFmpeg render compute by **~70%** (`$0.02726/hr` vs `$0.09086/hr` for `2 vCPU / 4 GiB` ARM64). Safe due to PostgreSQL `FOR UPDATE SKIP LOCKED` 120-second leases and `SIGTERM` handling.
- [x] **Scale-to-Zero Workers (`min_capacity = 0`)**: Driven by `TripAnimator/RenderQueueDepth` (`queued + running` `render_jobs`), dropping idle worker compute to **$0/mo** outside active exports.
- [x] **S3 Gateway VPC Endpoint (`$0/hr`, `$0/GB`)**: Attached to `private_app` route tables so Silver GeoParquet, Bronze GPX/JSON, Gold DuckDB, and rendered MP4 transfers bypass NAT Gateways (`$0.052/GB` saved).
- [x] **S3 Lifecycle Expiration & Tiering**:
  - Expire `outputs/` (and `renders/`) after `RENDER_RETENTION_DAYS` (`7` days) + abort incomplete multipart uploads after `1` day, capping video storage linearly.
  - Transition immutable `bronze/` payloads to **`STANDARD_IA`** after `30` days (~45% storage discount vs S3 Standard).
  - Enable **S3 Bucket Key** on SSE-KMS to cut KMS API request fees by ~99%.
- [x] **AWS Graviton (`ARM64`) Across Compute & RDS**: `db.t4g.*` / `db.r6g.*` RDS instances and `ARM64` Fargate tasks (`api` + `worker`) save **~20%** over `x86_64` at equal or better Python/Polars/DuckDB/FFmpeg performance.
- [x] **Serverless Batch Before Provisioned MWAA**: EventBridge Scheduler + ECS `RunTask` runs `reap`, `dbt` Gold builds, and Strava sync for **<$2/mo** vs **$362.67/mo** for always-on MWAA `mw1.small`.
- [x] **Commitments at Steady State (Growth Tier)**: Once baseline API (`2–4` tasks) and Multi-AZ RDS (`db.r6g.large`) stabilize, a **1-Year Compute Savings Plan** + **1-Year No-Upfront RDS Reserved Instance** reduces Scenario 3 monthly spend by ~**$200–$300/mo**.
- [x] **CloudFront Caching for Static SPA & Assets**: Serving `apps/web/dist` via S3 + OAC eliminates ECS Nginx web containers entirely, caches `/assets/*` immutably at the edge (free S3 → CloudFront origin fetch), and avoids ALB LCU charges on static traffic.
