locals {
  marked_trails_enabled     = length(var.marked_trails_app_origins) > 0
  marked_trails_count       = local.marked_trails_enabled ? 1 : 0
  marked_trails_bucket_name = "${var.name_prefix}-marked-trails-${data.aws_caller_identity.current.account_id}"
  marked_trails_prefix      = trim(var.marked_trails_prefix, "/")
}

resource "aws_s3_bucket" "marked_trails" {
  count = local.marked_trails_count

  bucket        = local.marked_trails_bucket_name
  force_destroy = false

  lifecycle {
    precondition {
      condition     = data.aws_region.current.name == "us-east-1"
      error_message = "The Marked Trails bucket must live in us-east-1, the region of the render Lambdas, so tile reads stay in-region."
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "marked_trails" {
  count = local.marked_trails_count

  bucket = aws_s3_bucket.marked_trails[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "marked_trails" {
  count = local.marked_trails_count

  bucket                  = aws_s3_bucket.marked_trails[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 rather than KMS: origin access control would otherwise need the key policy to trust CloudFront.
resource "aws_s3_bucket_server_side_encryption_configuration" "marked_trails" {
  count = local.marked_trails_count

  bucket = aws_s3_bucket.marked_trails[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Only incomplete multipart uploads expire. An expiration rule on builds/ or current.json could delete the
# build that readers use, because builds are replaced by the publisher and never age out on their own.
resource "aws_s3_bucket_lifecycle_configuration" "marked_trails" {
  count = local.marked_trails_count

  bucket = aws_s3_bucket.marked_trails[0].id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_cloudfront_origin_access_control" "marked_trails" {
  count = local.marked_trails_count

  name                              = "${var.name_prefix}-marked-trails"
  description                       = "Origin access control for the Marked Trails bucket."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "marked_trails_caching_optimized" {
  count = local.marked_trails_count

  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "marked_trails_caching_disabled" {
  count = local.marked_trails_count

  name = "Managed-CachingDisabled"
}

resource "aws_cloudfront_response_headers_policy" "marked_trails" {
  count = local.marked_trails_count

  name    = "${var.name_prefix}-marked-trails-cors"
  comment = "CORS for PMTiles range reads from the Trip Animator app origins."

  cors_config {
    access_control_allow_credentials = false
    origin_override                  = true
    access_control_max_age_sec       = 86400

    access_control_allow_methods {
      items = ["GET", "HEAD"]
    }

    access_control_allow_origins {
      items = var.marked_trails_app_origins
    }

    access_control_allow_headers {
      items = ["Range", "If-Match", "If-None-Match", "If-Range"]
    }

    access_control_expose_headers {
      items = ["Content-Range", "ETag", "Accept-Ranges"]
    }
  }
}

resource "aws_cloudfront_distribution" "marked_trails" {
  count = local.marked_trails_count

  enabled         = true
  comment         = "Marked Trails PMTiles for ${var.name_prefix}"
  http_version    = "http2and3"
  is_ipv6_enabled = true
  price_class     = var.marked_trails_price_class

  origin {
    domain_name              = aws_s3_bucket.marked_trails[0].bucket_regional_domain_name
    origin_id                = "marked-trails-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.marked_trails[0].id
  }

  default_cache_behavior {
    target_origin_id           = "marked-trails-s3"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.marked_trails_caching_optimized[0].id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.marked_trails[0].id
    # Compression would change byte offsets that PMTiles range requests rely on.
    compress = false
  }

  ordered_cache_behavior {
    path_pattern               = "${local.marked_trails_prefix}/current.json"
    target_origin_id           = "marked-trails-s3"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.marked_trails_caching_disabled[0].id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.marked_trails[0].id
    compress                   = false
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

data "aws_iam_policy_document" "marked_trails_bucket" {
  count = local.marked_trails_count

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.marked_trails[0].arn,
      "${aws_s3_bucket.marked_trails[0].arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "AllowCloudFrontRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.marked_trails[0].arn}/${local.marked_trails_prefix}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.marked_trails[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "marked_trails" {
  count = local.marked_trails_count

  bucket = aws_s3_bucket.marked_trails[0].id
  policy = data.aws_iam_policy_document.marked_trails_bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.marked_trails]
}

data "aws_iam_policy_document" "marked_trails_publisher" {
  count = local.marked_trails_count

  statement {
    sid    = "TrailPrefixObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.marked_trails[0].arn}/${local.marked_trails_prefix}/*"]
  }

  statement {
    sid       = "TrailPrefixList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.marked_trails[0].arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.marked_trails_prefix}/*"]
    }
  }
}

resource "aws_iam_policy" "marked_trails_publisher" {
  count = local.marked_trails_count

  name        = "${var.name_prefix}-marked-trails-publisher-policy"
  description = "Least-privilege policy for the Airflow Marked Trails publisher."
  policy      = data.aws_iam_policy_document.marked_trails_publisher[0].json
}
