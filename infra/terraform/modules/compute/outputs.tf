output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer."
  value       = aws_lb.main.dns_name
}

output "alb_arn" {
  description = "ARN of the Application Load Balancer."
  value       = aws_lb.main.arn
}

output "origin_verify_header_value" {
  description = "Shared secret for CloudFront X-Origin-Verify origin cloaking header."
  value       = var.enable_origin_verify ? random_password.origin_verify.result : ""
  sensitive   = true
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster."
  value       = aws_ecs_cluster.main.name
}

output "ecs_cluster_arn" {
  description = "ARN of the ECS cluster."
  value       = aws_ecs_cluster.main.arn
}

output "ecr_repository_urls" {
  description = "Map of service name to ECR repository URL."
  value       = { for k, repo in aws_ecr_repository.services : k => repo.repository_url }
}

output "app_secret_arn" {
  description = "Secrets Manager ARN containing runtime application secrets."
  value       = aws_secretsmanager_secret.app.arn
}

output "api_service_name" {
  description = "Name of the API ECS service."
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "Name of the render worker ECS service."
  value       = aws_ecs_service.worker.name
}

output "migrate_task_definition_arn" {
  description = "ARN of the Alembic database migration ECS task definition."
  value       = aws_ecs_task_definition.migrate.arn
}

output "reap_task_definition_arn" {
  description = "ARN of the retention reaper ECS task definition."
  value       = aws_ecs_task_definition.reap.arn
}

output "dbt_task_definition_arn" {
  description = "ARN of the dbt Gold build ECS task definition."
  value       = aws_ecs_task_definition.dbt.arn
}
