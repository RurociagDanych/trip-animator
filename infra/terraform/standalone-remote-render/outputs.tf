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
