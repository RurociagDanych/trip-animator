variable "aws_region" {
  description = "AWS deployment region."
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Project identifier used in resource names and tags."
  type        = string
  default     = "trip-animator"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.0.0.0/16"
}

variable "enable_nat_gateway" {
  description = "Enable NAT Gateways for private ECS subnets."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Use one NAT Gateway per AZ in prod for high availability (false = multi-AZ)."
  type        = bool
  default     = false
}

variable "restrict_alb_to_cloudfront" {
  description = "Restrict ALB ingress to CloudFront origin-facing prefix list."
  type        = bool
  default     = true
}

variable "alb_ingress_cidrs" {
  description = "Allowed ALB ingress CIDRs when restrict_alb_to_cloudfront is false."
  type        = list(string)
  default     = []
}

variable "enable_origin_verify" {
  description = "Enforce X-Origin-Verify shared secret header between CloudFront and ALB."
  type        = bool
  default     = true
}

variable "enable_waf" {
  description = "Enable AWS WAFv2 WebACL (us-east-1 CLOUDFRONT scope) on CloudFront."
  type        = bool
  default     = true
}

variable "enable_flow_logs" {
  description = "Enable VPC Flow Logs."
  type        = bool
  default     = true
}

variable "domain_name" {
  description = "Custom domain name for CloudFront (e.g. trips.example.com)."
  type        = string
  default     = ""
}

variable "cloudfront_acm_certificate_arn" {
  description = "ACM certificate ARN in us-east-1 for CloudFront custom domain HTTPS."
  type        = string
  default     = ""
}

variable "alb_acm_certificate_arn" {
  description = "Regional ACM certificate ARN for ALB HTTPS listener."
  type        = string
  default     = ""
}

variable "cloudfront_price_class" {
  description = "CloudFront price class."
  type        = string
  default     = "PriceClass_100"
}

variable "map_tile_csp_origins" {
  description = "Space-separated map tile origins for CloudFront CSP header."
  type        = string
  default     = "https://tiles.openfreemap.org"
}

variable "map_tile_allowlist" {
  description = "Comma-separated map tile origins for API and worker containers."
  type        = string
  default     = "https://tiles.openfreemap.org"
}

variable "render_retention_days" {
  description = "Days before rendered MP4 outputs expire in S3 and DB."
  type        = number
  default     = 7
}

variable "enable_lakehouse_tiering" {
  description = "Enable S3 lifecycle transitions on bronze/ and gold/ prefixes."
  type        = bool
  default     = true
}

variable "force_destroy_buckets" {
  description = "Prevent accidental destruction of non-empty S3 buckets in prod."
  type        = bool
  default     = false
}

variable "db_engine_version" {
  description = "RDS PostgreSQL 17 engine version."
  type        = string
  default     = "17.6"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.small"
}

variable "db_allocated_storage" {
  description = "Initial RDS gp3 storage in GiB."
  type        = number
  default     = 50
}

variable "db_max_allocated_storage" {
  description = "Max RDS autoscaling storage in GiB."
  type        = number
  default     = 500
}

variable "db_multi_az" {
  description = "Enable RDS Multi-AZ standby replica in prod."
  type        = bool
  default     = true
}

variable "db_backup_retention_period" {
  description = "RDS backup retention in days."
  type        = number
  default     = 14
}

variable "db_deletion_protection" {
  description = "Enable RDS deletion protection in prod."
  type        = bool
  default     = true
}

variable "db_skip_final_snapshot" {
  description = "Take a final RDS snapshot on destroy in prod."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Immutable container image tag deployed to ECS (e.g. git SHA)."
  type        = string
  default     = "latest"
}

variable "ecr_image_tag_mutability" {
  description = "ECR tag mutability (IMMUTABLE in prod)."
  type        = string
  default     = "IMMUTABLE"
}

variable "cpu_architecture" {
  description = "Fargate CPU architecture (ARM64 Graviton or X86_64)."
  type        = string
  default     = "ARM64"
}

variable "api_cpu" {
  description = "API Fargate task CPU units."
  type        = number
  default     = 1024
}

variable "api_memory" {
  description = "API Fargate task memory (MiB)."
  type        = number
  default     = 2048
}

variable "api_min_capacity" {
  description = "Minimum API ECS tasks (2 across 2 AZs for HA)."
  type        = number
  default     = 2
}

variable "api_max_capacity" {
  description = "Maximum API ECS tasks."
  type        = number
  default     = 8
}

variable "worker_cpu" {
  description = "Worker Fargate task CPU units."
  type        = number
  default     = 2048
}

variable "worker_memory" {
  description = "Worker Fargate task memory (MiB)."
  type        = number
  default     = 4096
}

variable "worker_ephemeral_storage_gib" {
  description = "Worker ephemeral storage in GiB."
  type        = number
  default     = 30
}

variable "worker_concurrency" {
  description = "Worker concurrent render jobs."
  type        = number
  default     = 1
}

variable "worker_min_capacity" {
  description = "Minimum worker tasks (0 scales to zero on idle)."
  type        = number
  default     = 0
}

variable "worker_max_capacity" {
  description = "Maximum worker tasks."
  type        = number
  default     = 6
}

variable "log_level" {
  description = "Application log level."
  type        = string
  default     = "INFO"
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 90
}

variable "enable_container_insights" {
  description = "Enable ECS Container Insights."
  type        = bool
  default     = true
}

variable "reap_schedule_expression" {
  description = "EventBridge schedule for daily retention reaper."
  type        = string
  default     = "cron(0 3 * * ? *)"
}

variable "enable_dbt_schedule" {
  description = "Enable scheduled ECS RunTask for dbt Gold build."
  type        = bool
  default     = true
}

variable "dbt_schedule_expression" {
  description = "EventBridge schedule for dbt Gold build."
  type        = string
  default     = "cron(30 3 * * ? *)"
}

variable "kms_deletion_window_days" {
  description = "KMS key deletion window in days."
  type        = number
  default     = 30
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window in days."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags applied to all resources."
  type        = map(string)
  default     = {}
}
