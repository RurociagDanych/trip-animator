variable "name_prefix" {
  description = "Resource naming prefix (e.g. trip-animator-dev)."
  type        = string
}

variable "subnet_ids" {
  description = "Isolated subnet IDs for the RDS DB subnet group."
  type        = list(string)
}

variable "rds_security_group_id" {
  description = "Security group ID attached to the RDS instance."
  type        = string
}

variable "engine_version" {
  description = "RDS PostgreSQL 17 engine version."
  type        = string
  default     = "17.6"
}

variable "db_instance_class" {
  description = "RDS instance class (e.g. db.t4g.micro, db.t4g.small, db.r6g.large)."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Initial allocated gp3 storage in GiB."
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = "Maximum storage autoscaling limit in GiB."
  type        = number
  default     = 100
}

variable "multi_az" {
  description = "Enable Multi-AZ standby replica for high availability."
  type        = bool
  default     = false
}

variable "db_name" {
  description = "Initial PostgreSQL database name."
  type        = string
  default     = "trip_animator"
}

variable "db_username" {
  description = "PostgreSQL master username."
  type        = string
  default     = "trip_app"
}

variable "backup_retention_period" {
  description = "Automated backup retention period in days."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Enable RDS deletion protection."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Skip final snapshot on DB destruction (true for dev, false for prod)."
  type        = bool
  default     = false
}

variable "kms_deletion_window_days" {
  description = "Waiting period in days before deleting KMS key."
  type        = number
  default     = 30
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window in days (0 forces immediate deletion in dev)."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Common tags applied to all database resources."
  type        = map(string)
  default     = {}
}
