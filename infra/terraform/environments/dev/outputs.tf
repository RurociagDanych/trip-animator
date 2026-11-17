output "vpc_id" {
  description = "VPC ID."
  value       = module.networking.vpc_id
}

output "cloudfront_domain_name" {
  description = "Public CloudFront domain name for the dev environment."
  value       = module.storage.cloudfront_domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidation)."
  value       = module.storage.cloudfront_distribution_id
}

output "web_bucket_id" {
  description = "S3 bucket name for static frontend assets."
  value       = module.storage.web_bucket_id
}

output "objects_bucket_id" {
  description = "S3 bucket name for bronze/silver/gold/renders data lake."
  value       = module.storage.objects_bucket_id
}

output "alb_dns_name" {
  description = "Application Load Balancer DNS name."
  value       = module.compute.alb_dns_name
}

output "ecr_repository_urls" {
  description = "ECR repository URLs for api, worker, and airflow images."
  value       = module.compute.ecr_repository_urls
}

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = module.compute.ecs_cluster_name
}

output "api_service_name" {
  description = "ECS API service name."
  value       = module.compute.api_service_name
}

output "worker_service_name" {
  description = "ECS worker service name."
  value       = module.compute.worker_service_name
}

output "migrate_task_definition_arn" {
  description = "Alembic migration task definition ARN for CI/CD RunTask."
  value       = module.compute.migrate_task_definition_arn
}

output "rds_endpoint" {
  description = "RDS PostgreSQL 17 endpoint."
  value       = module.database.db_endpoint
}

output "db_secret_arn" {
  description = "Secrets Manager ARN for RDS credentials."
  value       = module.database.db_secret_arn
}

output "app_secret_arn" {
  description = "Secrets Manager ARN for application runtime secrets."
  value       = module.compute.app_secret_arn
}
