output "s3_kms_key_arn" {
  description = "ARN of the KMS key encrypting S3 web and objects buckets."
  value       = aws_kms_key.s3.arn
}

output "web_bucket_id" {
  description = "Name of the S3 bucket hosting the SPA static bundle."
  value       = aws_s3_bucket.web.id
}

output "web_bucket_arn" {
  description = "ARN of the S3 bucket hosting the SPA static bundle."
  value       = aws_s3_bucket.web.arn
}

output "objects_bucket_id" {
  description = "Name of the S3 bucket storing bronze/, silver/, gold/, and renders/ objects."
  value       = aws_s3_bucket.objects.id
}

output "objects_bucket_arn" {
  description = "ARN of the S3 bucket storing bronze/, silver/, gold/, and renders/ objects."
  value       = aws_s3_bucket.objects.arn
}

output "cloudfront_distribution_id" {
  description = "ID of the CloudFront distribution."
  value       = aws_cloudfront_distribution.main.id
}

output "cloudfront_distribution_arn" {
  description = "ARN of the CloudFront distribution."
  value       = aws_cloudfront_distribution.main.arn
}

output "cloudfront_domain_name" {
  description = "Domain name of the CloudFront distribution."
  value       = aws_cloudfront_distribution.main.domain_name
}
