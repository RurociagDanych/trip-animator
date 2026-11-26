variable "aws_region" {
  type        = string
  default     = "eu-central-1"
  description = "AWS region for the standalone remote render worker stack."
}

variable "name_prefix" {
  type        = string
  default     = "trip-animator-remote-render"
  description = "Prefix applied to AWS resources."
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Container image tag pushed to the worker ECR repository."
}

variable "lambda_memory_mb" {
  type        = number
  default     = 10240
  description = "Memory in MB allocated to the classic Lambda worker (10240 MB yields 6 vCPU)."
}

variable "lambda_timeout_seconds" {
  type        = number
  default     = 900
  description = "Maximum per-invocation Lambda timeout in seconds."
}

variable "lambda_ephemeral_storage_mb" {
  type        = number
  default     = 4096
  description = "Ephemeral /tmp storage in MB for frame encoding and chunk concatenation."
}

variable "reserved_concurrent_executions" {
  type        = number
  default     = -1
  description = "Optional Lambda reserved concurrency (-1 uses unreserved account concurrency)."
}

variable "sfn_max_concurrency" {
  type        = number
  default     = 8
  description = "Maximum parallel chunk Lambdas in Step Functions Map state."
}

variable "staging_retention_days" {
  type        = number
  default     = 3
  description = "Lifecycle expiration in days for objects in the staging S3 bucket."
}

variable "log_retention_days" {
  type        = number
  default     = 14
  description = "CloudWatch Logs retention in days for the Lambda worker."
}

variable "map_tile_allowlist" {
  type        = string
  default     = "https://tiles.openfreemap.org"
  description = "Comma-separated allowlist of map tile origins for Playwright SSRF guard."
}

variable "render_callback_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional HMAC-SHA256 shared secret for signed progress and completion callbacks."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional AWS resource tags."
}
