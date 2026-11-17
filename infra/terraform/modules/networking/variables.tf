variable "name_prefix" {
  description = "Resource naming prefix (e.g. trip-animator-dev)."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of Availability Zones to span (minimum 2 for ALB and RDS)."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "enable_nat_gateway" {
  description = "Whether to provision NAT Gateway(s) for private subnet outbound internet access."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Use a single shared NAT Gateway instead of one per AZ to reduce cost."
  type        = bool
  default     = true
}

variable "enable_s3_gateway_endpoint" {
  description = "Provision a free S3 Gateway VPC Endpoint on private and isolated route tables."
  type        = bool
  default     = true
}

variable "restrict_alb_to_cloudfront" {
  description = "Restrict ALB HTTP/HTTPS ingress to the AWS CloudFront origin-facing managed prefix list."
  type        = bool
  default     = false
}

variable "alb_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the ALB on ports 80/443 when restrict_alb_to_cloudfront is false."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "api_port" {
  description = "Container listening port for the FastAPI service."
  type        = number
  default     = 8000
}

variable "enable_flow_logs" {
  description = "Enable VPC Flow Logs to CloudWatch Logs."
  type        = bool
  default     = false
}

variable "flow_logs_retention_days" {
  description = "Retention period in days for VPC Flow Logs."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Common tags applied to all networking resources."
  type        = map(string)
  default     = {}
}
