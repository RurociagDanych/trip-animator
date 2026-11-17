variable "name_prefix" {
  description = "Resource naming prefix (e.g. trip-animator-dev)."
  type        = string
}

variable "objects_bucket_name" {
  description = "Deterministic name for the S3 data lake objects bucket."
  type        = string
}

variable "alb_dns_name" {
  description = "DNS name of the Application Load Balancer origin for /api/*, /health, and /ready."
  type        = string
}

variable "alb_origin_protocol_policy" {
  description = "Protocol policy CloudFront uses when connecting to the ALB origin (https-only or http-only)."
  type        = string
  default     = "https-only"

  validation {
    condition     = contains(["http-only", "https-only", "match-viewer"], var.alb_origin_protocol_policy)
    error_message = "alb_origin_protocol_policy must be http-only, https-only, or match-viewer."
  }
}

variable "origin_verify_header_value" {
  description = "Shared secret sent by CloudFront to ALB in X-Origin-Verify header for origin cloaking."
  type        = string
  default     = ""
  sensitive   = true
}

variable "render_retention_days" {
  description = "Number of days before objects under outputs/ and renders/ expire (matches RENDER_RETENTION_DAYS)."
  type        = number
  default     = 7

  validation {
    condition     = var.render_retention_days >= 1 && var.render_retention_days <= 3650
    error_message = "render_retention_days must be between 1 and 3650."
  }
}

variable "enable_lakehouse_tiering" {
  description = "Enable S3 lifecycle transitions on bronze/ and gold/ prefixes to STANDARD_IA at 30 days."
  type        = bool
  default     = true
}

variable "force_destroy_buckets" {
  description = "Allow Terraform to destroy non-empty S3 buckets (recommended false in prod)."
  type        = bool
  default     = false
}

variable "enable_waf" {
  description = "Attach an AWS WAFv2 WebACL (us-east-1 CLOUDFRONT scope) to CloudFront."
  type        = bool
  default     = false
}

variable "cloudfront_price_class" {
  description = "CloudFront distribution price class."
  type        = string
  default     = "PriceClass_100"
}

variable "domain_name" {
  description = "Optional custom HTTPS domain name (leave empty to publish CloudFront domain to SSM)."
  type        = string
  default     = ""
}

variable "aliases" {
  description = "Custom domain aliases (CNAMEs) for CloudFront."
  type        = list(string)
  default     = []
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN in us-east-1 for CloudFront custom domain HTTPS (leave empty for default CloudFront cert)."
  type        = string
  default     = ""
}

variable "map_tile_csp_origins" {
  description = "Space-separated map tile origins allowed in Content-Security-Policy."
  type        = string
  default     = "https://tiles.openfreemap.org"
}

variable "kms_deletion_window_days" {
  description = "Waiting period in days before deleting KMS keys."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Common tags applied to all storage resources."
  type        = map(string)
  default     = {}
}
