output "db_instance_id" {
  description = "RDS instance identifier."
  value       = aws_db_instance.main.id
}

output "db_endpoint" {
  description = "RDS instance endpoint (host:port)."
  value       = aws_db_instance.main.endpoint
}

output "db_address" {
  description = "RDS instance hostname."
  value       = aws_db_instance.main.address
}

output "db_port" {
  description = "RDS instance port."
  value       = aws_db_instance.main.port
}

output "db_name" {
  description = "PostgreSQL database name."
  value       = aws_db_instance.main.db_name
}

output "db_secret_arn" {
  description = "ARN of the Secrets Manager secret containing RDS credentials and DATABASE_URL."
  value       = aws_secretsmanager_secret.db.arn
}

output "rds_kms_key_arn" {
  description = "ARN of the KMS key encrypting RDS and DB Secrets Manager secret."
  value       = aws_kms_key.rds.arn
}
