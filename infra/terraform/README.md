# Trip Animator — AWS Terraform Infrastructure

Modular Terraform (`>= 1.6.0`, `hashicorp/aws ~> 5.0`) provisioning the Trip Animator cloud runtime on AWS (`eu-central-1` default).

## Directory Layout

```text
infra/terraform/
├── modules/
│   ├── networking/   # 2-AZ VPC, public/private/isolated subnets, IGW, NAT, S3 Gateway Endpoint, SGs
│   ├── storage/      # KMS-encrypted S3 web & objects buckets, lifecycle rules, CloudFront + OAC
│   ├── database/     # RDS PostgreSQL 17 (isolated subnets, KMS, force_ssl, Secrets Manager)
│   └── compute/      # ECR, ECS Cluster (Fargate + Spot), ALB, API/Worker services, migrate/reap/dbt tasks, EventBridge Scheduler
└── environments/
    ├── dev/          # Cost-optimized portfolio/dev configuration (single NAT, db.t4g.micro, scale-to-zero workers)
    └── prod/         # Multi-AZ HA configuration (Multi-AZ RDS, 2-AZ NAT, CloudFront-locked ALB, immutable ECR)
```

## Security & Architecture Highlights

- **Zero hardcoded secrets**: RDS credentials and runtime app secrets (`SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY`, `METRICS_TOKEN`, Strava OAuth credentials) live in AWS Secrets Manager encrypted with customer-managed KMS keys and are injected into ECS containers via `secrets` ARN references.
- **Network isolation**: ALB in public subnets (optionally locked to the CloudFront origin-facing managed prefix list), ECS API & Spot workers in private subnets, RDS PostgreSQL 17 in isolated subnets with no internet route and `rds.force_ssl = 1`.
- **Private S3 access**: S3 Gateway VPC Endpoint routes bucket traffic privately at zero data-processing cost; both S3 buckets enforce `BucketOwnerEnforced`, `PublicAccessBlock`, TLS-only transport, and KMS encryption.
- **Least-privilege IAM**: Dedicated task roles per workload (`api`, `worker`, `migrate`, `reap`, `dbt`) and scoped `iam:PassRole` / `ecs:RunTask` permissions for EventBridge Scheduler.

## Standalone Remote Render Root

`standalone-remote-render/` is a separate root, deployed in `us-east-1`, that holds the render Lambdas, the Step Functions state machine and the Marked Trails storage: a private S3 bucket, a CloudFront distribution with origin access control and a CORS response headers policy, and the IAM policy of the Airflow publisher. `marked_trails_app_origins` defaults to an empty list, which creates none of the Marked Trails resources; set it in the tfvars to enable them. See [Marked Trails on AWS](../../docs/OPERATIONS.md#marked-trails-on-aws) for the apply steps and for adding the CloudFront host to `map_tile_allowlist` and `CADDY_TILE_CSP_ORIGINS`.

## Quick Validation

```bash
terraform fmt -check -recursive infra/terraform

cd infra/terraform/environments/dev
terraform init -backend=false && terraform validate

cd ../prod
terraform init -backend=false && terraform validate

cd ../../standalone-remote-render
terraform init -backend=false && terraform validate
```

## Deployment & Remote State

1. Provision an encrypted S3 state bucket and DynamoDB lock table, then uncomment the `backend "s3"` block in `environments/<env>/main.tf`.
2. Copy `terraform.tfvars.example` to `terraform.tfvars` and customize domain/ACM/sizing variables.
3. Run `terraform init && terraform apply`.
4. Populate `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in the Secrets Manager secret referenced by the `app_secret_arn` output.
5. Run the Alembic migration task (`migrate_task_definition_arn`) via `aws ecs run-task` before rolling out a new API/worker image tag.
