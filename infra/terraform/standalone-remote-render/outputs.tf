output "ecr_repository_url" {
  value       = aws_ecr_repository.worker.repository_url
  description = "ECR repository URL for Dockerfile.worker-aws-lambda images."
}

output "staging_bucket_name" {
  value       = aws_s3_bucket.staging.bucket
  description = "S3 staging bucket name for RENDER_AWS_STAGING_BUCKET."
}

output "lambda_function_arn" {
  value       = aws_lambda_function.worker.arn
  description = "ARN of the classic Lambda render worker function."
}

output "state_machine_arn" {
  value       = aws_sfn_state_machine.render.arn
  description = "ARN of the Step Functions state machine for RENDER_AWS_STATE_MACHINE_ARN."
}

output "dispatcher_policy_arn" {
  value       = aws_iam_policy.dispatcher.arn
  description = "Least-privilege IAM policy ARN to attach to the VPS dispatcher user or role."
}

output "marked_trails_bucket_name" {
  value       = try(aws_s3_bucket.marked_trails[0].bucket, null)
  description = "Marked Trails bucket name for MARKED_TRAILS_S3_BUCKET."
}

output "marked_trails_base_url" {
  value       = try("https://${aws_cloudfront_distribution.marked_trails[0].domain_name}/${local.marked_trails_prefix}", null)
  description = "Marked Trails base URL for MARKED_TRAILS_BASE_URL; it serves current.json."
}

output "marked_trails_cloudfront_origin" {
  value       = try("https://${aws_cloudfront_distribution.marked_trails[0].domain_name}", null)
  description = "Origin to add to MAP_TILE_ALLOWLIST and CADDY_TILE_CSP_ORIGINS."
}

output "marked_trails_publisher_policy_arn" {
  value       = try(aws_iam_policy.marked_trails_publisher[0].arn, null)
  description = "IAM policy ARN to attach to the Airflow Marked Trails publisher user or role."
}
