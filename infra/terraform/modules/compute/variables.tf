variable "name_prefix" {
  description = "Resource naming prefix (e.g. trip-animator-dev)."
  type        = string
}

variable "vpc_id" {
  description = "VPC ID where ALB and ECS tasks run."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs for the Application Load Balancer."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS tasks and scheduled jobs."
  type        = list(string)
}

variable "alb_security_group_id" {
  description = "Security group ID attached to the ALB."
  type        = string
}

variable "api_security_group_id" {
  description = "Security group ID attached to API, migrate, reap, and dbt ECS tasks."
  type        = string
}

variable "worker_security_group_id" {
  description = "Security group ID attached to render worker ECS tasks."
  type        = string
}

variable "objects_bucket_name" {
  description = "Deterministic S3 objects bucket name passed as S3_BUCKET and scoped in IAM policies."
  type        = string
}

variable "db_secret_arn" {
  description = "Secrets Manager ARN containing RDS credentials and DATABASE_URL."
  type        = string
}

variable "rds_kms_key_arn" {
  description = "KMS key ARN encrypting the RDS Secrets Manager secret."
  type        = string
}

variable "domain_name" {
  description = "Optional custom HTTPS domain name for ALLOWED_ORIGINS, STRAVA_REDIRECT_URI, and RENDER_BASE_URL."
  type        = string
  default     = ""
}

variable "alb_acm_certificate_arn" {
  description = "Optional ACM certificate ARN in the deployment region for ALB HTTPS listener."
  type        = string
  default     = ""
}

variable "enable_origin_verify" {
  description = "Enforce X-Origin-Verify header check on ALB listener rules for CloudFront origin cloaking."
  type        = bool
  default     = true
}

variable "enable_nat_gateway" {
  description = "Whether private subnets have NAT Gateway egress (when false, ECS tasks run in public subnets with assign_public_ip = true)."
  type        = bool
  default     = true
}

variable "enable_queue_metric_publisher" {
  description = "Deploy a 1-minute Lambda metric publisher for TripAnimator/RenderQueueDepth (requires enable_nat_gateway = true)."
  type        = bool
  default     = true
}

variable "api_port" {
  description = "Container listening port for FastAPI."
  type        = number
  default     = 8000
}

variable "image_tag" {
  description = "Container image tag deployed to ECS task definitions."
  type        = string
  default     = "latest"
}

variable "ecr_image_tag_mutability" {
  description = "ECR tag mutability setting (IMMUTABLE recommended for prod)."
  type        = string
  default     = "IMMUTABLE"
}

variable "ecr_max_image_count" {
  description = "Maximum number of tagged images retained per ECR repository."
  type        = number
  default     = 10
}

variable "cpu_architecture" {
  description = "ECS Fargate CPU architecture (ARM64 Graviton or X86_64)."
  type        = string
  default     = "ARM64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

variable "api_cpu" {
  description = "Fargate CPU units for API tasks."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory (MiB) for API tasks."
  type        = number
  default     = 1024
}

variable "api_min_capacity" {
  description = "Minimum ECS API task count."
  type        = number
  default     = 1
}

variable "api_max_capacity" {
  description = "Maximum ECS API task count."
  type        = number
  default     = 4
}

variable "worker_cpu" {
  description = "Fargate CPU units for Playwright + FFmpeg render worker tasks."
  type        = number
  default     = 2048
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for render worker tasks."
  type        = number
  default     = 4096
}

variable "worker_ephemeral_storage_gib" {
  description = "Fargate ephemeral storage (GiB, 21-200) for worker temporary video frames and MP4 output."
  type        = number
  default     = 30

  validation {
    condition     = var.worker_ephemeral_storage_gib >= 21 && var.worker_ephemeral_storage_gib <= 200
    error_message = "worker_ephemeral_storage_gib must be between 21 and 200."
  }
}

variable "worker_concurrency" {
  description = "Concurrent render jobs per worker task."
  type        = number
  default     = 1
}

variable "worker_min_capacity" {
  description = "Minimum ECS worker task count (0 enables scale-to-zero)."
  type        = number
  default     = 0
}

variable "worker_max_capacity" {
  description = "Maximum ECS worker task count."
  type        = number
  default     = 4
}

variable "render_retention_days" {
  description = "Render retention window in days passed to API and reaper."
  type        = number
  default     = 7
}

variable "map_tile_allowlist" {
  description = "Comma-separated allowed map tile origin URLs."
  type        = string
  default     = "https://tiles.openfreemap.org"
}

variable "log_level" {
  description = "Application log level."
  type        = string
  default     = "INFO"
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 30
}

variable "enable_container_insights" {
  description = "Enable CloudWatch Container Insights on the ECS cluster."
  type        = bool
  default     = true
}

variable "reap_schedule_expression" {
  description = "EventBridge Scheduler cron/rate expression for the retention reaper task."
  type        = string
  default     = "cron(0 3 * * ? *)"
}

variable "enable_dbt_schedule" {
  description = "Enable EventBridge Scheduler recurring ECS RunTask for dbt Gold build."
  type        = bool
  default     = false
}

variable "dbt_schedule_expression" {
  description = "EventBridge Scheduler cron/rate expression for the dbt Gold build task."
  type        = string
  default     = "cron(30 3 * * ? *)"
}

variable "kms_deletion_window_days" {
  description = "Waiting period in days before deleting KMS keys."
  type        = number
  default     = 30
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window in days."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Common tags applied to all compute resources."
  type        = map(string)
  default     = {}
}
