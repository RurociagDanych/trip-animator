data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  staging_bucket_name = "${var.name_prefix}-staging-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}"
}

resource "aws_ecr_repository" "worker" {
  name                 = "${var.name_prefix}-worker"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Retain last 10 tagged worker images"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_s3_bucket" "staging" {
  bucket        = local.staging_bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_ownership_controls" "staging" {
  bucket = aws_s3_bucket.staging.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "staging" {
  bucket                  = aws_s3_bucket.staging.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "staging" {
  bucket = aws_s3_bucket.staging.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "staging" {
  bucket = aws_s3_bucket.staging.id

  rule {
    id     = "expire-render-staging"
    status = "Enabled"

    filter {
      prefix = "staging/"
    }

    expiration {
      days = var.staging_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "staging_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.staging.arn,
      "${aws_s3_bucket.staging.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "staging_tls_only" {
  bucket = aws_s3_bucket.staging.id
  policy = data.aws_iam_policy_document.staging_tls_only.json
}

resource "aws_cloudwatch_log_group" "lambda_worker" {
  name              = "/aws/lambda/${var.name_prefix}-worker"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_worker" {
  name               = "${var.name_prefix}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_worker" {
  statement {
    sid    = "WriteLambdaLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lambda_worker.arn}:*"]
  }

  statement {
    sid    = "StagingBucketObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.staging.arn}/staging/*"]
  }

  statement {
    sid       = "StagingBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.staging.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["staging/*"]
    }
  }
}

resource "aws_iam_role_policy" "lambda_worker" {
  name   = "${var.name_prefix}-lambda-policy"
  role   = aws_iam_role.lambda_worker.id
  policy = data.aws_iam_policy_document.lambda_worker.json
}

resource "aws_lambda_function" "worker" {
  function_name                  = "${var.name_prefix}-worker"
  role                           = aws_iam_role.lambda_worker.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.worker.repository_url}:${var.image_tag}"
  memory_size                    = var.lambda_memory_mb
  timeout                        = var.lambda_timeout_seconds
  reserved_concurrent_executions = var.reserved_concurrent_executions

  ephemeral_storage {
    size = var.lambda_ephemeral_storage_mb
  }

  environment {
    variables = {
      LOG_LEVEL              = "INFO"
      MAP_TILE_ALLOWLIST     = var.map_tile_allowlist
      RENDER_CALLBACK_SECRET = var.render_callback_secret
      WORKER_GPU_ENABLED     = "false"
      WORKER_CPU_RENDERER    = "lavapipe"
      CHROMIUM_NO_SANDBOX    = "true"
      WORKER_LOCAL_SPA_DIR   = "/app/web-dist"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_worker,
    aws_iam_role_policy.lambda_worker,
  ]
}

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${var.name_prefix}-sfn-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn_invoke" {
  statement {
    sid       = "InvokeRenderWorkerLambda"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn]
  }
}

resource "aws_iam_role_policy" "sfn_invoke" {
  name   = "${var.name_prefix}-sfn-invoke"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn_invoke.json
}

resource "aws_sfn_state_machine" "render" {
  name     = "${var.name_prefix}-orchestrator"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    StartAt = "Prepare"
    States = {
      Prepare = {
        Type     = "Task"
        Resource = aws_lambda_function.worker.arn
        Parameters = {
          "action"                   = "prepare"
          "job_id.$"                 = "$.job_id"
          "staging_bucket.$"         = "$.staging_bucket"
          "staging_prefix.$"         = "$.staging_prefix"
          "manifest_key.$"           = "$.manifest_key"
          "photo_keys.$"             = "$.photo_keys"
          "output_key.$"             = "$.output_key"
          "thumbnail_key.$"          = "$.thumbnail_key"
          "chunk_threshold_frames.$" = "$.chunk_threshold_frames"
          "chunk_size_frames.$"      = "$.chunk_size_frames"
          "preroll_frames.$"         = "$.preroll_frames"
          "callback_url.$"           = "$.callback_url"
        }
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
            IntervalSeconds = 2
            MaxAttempts     = 3
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error_detail"
            Next        = "ReportFailure"
          }
        ]
        Next = "SelectMode"
      }
      SelectMode = {
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.mode"
            StringEquals = "single"
            Next         = "RenderSingle"
          }
        ]
        Default = "RenderChunks"
      }
      RenderSingle = {
        Type     = "Task"
        Resource = aws_lambda_function.worker.arn
        Parameters = {
          "action"           = "render_single"
          "job_id.$"         = "$.job_id"
          "staging_bucket.$" = "$.staging_bucket"
          "staging_prefix.$" = "$.staging_prefix"
          "manifest_key.$"   = "$.manifest_key"
          "photo_keys.$"     = "$.photo_keys"
          "output_key.$"     = "$.output_key"
          "thumbnail_key.$"  = "$.thumbnail_key"
          "callback_url.$"   = "$.callback_url"
        }
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
            IntervalSeconds = 2
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error_detail"
            Next        = "ReportFailure"
          }
        ]
        End = true
      }
      RenderChunks = {
        Type           = "Map"
        ItemsPath      = "$.chunks"
        MaxConcurrency = var.sfn_max_concurrency
        ResultPath     = "$.chunk_results"
        ItemSelector = {
          "action"           = "render_chunk"
          "job_id.$"         = "$.job_id"
          "staging_bucket.$" = "$.staging_bucket"
          "staging_prefix.$" = "$.staging_prefix"
          "manifest_key.$"   = "$.manifest_key"
          "photo_keys.$"     = "$.photo_keys"
          "total_chunks.$"   = "$.total_chunks"
          "callback_url.$"   = "$.callback_url"
          "chunk_index.$"    = "$$.Map.Item.Value.chunk_index"
          "start_frame.$"    = "$$.Map.Item.Value.start_frame"
          "end_frame.$"      = "$$.Map.Item.Value.end_frame"
          "preroll_frames.$" = "$$.Map.Item.Value.preroll_frames"
        }
        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
          StartAt = "RenderChunkTask"
          States = {
            RenderChunkTask = {
              Type     = "Task"
              Resource = aws_lambda_function.worker.arn
              Retry = [
                {
                  ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
                  IntervalSeconds = 2
                  MaxAttempts     = 3
                  BackoffRate     = 2.0
                }
              ]
              End = true
            }
          }
        }
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error_detail"
            Next        = "ReportFailure"
          }
        ]
        Next = "Finalize"
      }
      Finalize = {
        Type     = "Task"
        Resource = aws_lambda_function.worker.arn
        Parameters = {
          "action"           = "finalize"
          "job_id.$"         = "$.job_id"
          "staging_bucket.$" = "$.staging_bucket"
          "manifest_key.$"   = "$.manifest_key"
          "output_key.$"     = "$.output_key"
          "thumbnail_key.$"  = "$.thumbnail_key"
          "chunk_results.$"  = "$.chunk_results"
          "callback_url.$"   = "$.callback_url"
        }
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error_detail"
            Next        = "ReportFailure"
          }
        ]
        End = true
      }
      ReportFailure = {
        Type     = "Task"
        Resource = aws_lambda_function.worker.arn
        Parameters = {
          "action"         = "report_failure"
          "job_id.$"       = "$.job_id"
          "callback_url.$" = "$.callback_url"
          "error_detail.$" = "$.error_detail"
        }
        Next = "FailExecution"
      }
      FailExecution = {
        Type  = "Fail"
        Error = "RemoteRenderFailed"
        Cause = "Remote render workflow failed; inspect CloudWatch logs for details."
      }
    }
  })
}

data "aws_iam_policy_document" "dispatcher" {
  statement {
    sid    = "ControlRenderStateMachine"
    effect = "Allow"
    actions = [
      "states:StartExecution",
    ]
    resources = [aws_sfn_state_machine.render.arn]
  }

  statement {
    sid    = "InspectAndStopExecutions"
    effect = "Allow"
    actions = [
      "states:DescribeExecution",
      "states:StopExecution",
    ]
    resources = [
      "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:execution:${aws_sfn_state_machine.render.name}:*",
    ]
  }

  statement {
    sid    = "StagingBucketReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.staging.arn}/staging/*"]
  }

  statement {
    sid       = "StagingBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.staging.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["staging/*"]
    }
  }
}

resource "aws_iam_policy" "dispatcher" {
  name        = "${var.name_prefix}-dispatcher-policy"
  description = "Least-privilege policy for Trip Animator API dispatching remote renders."
  policy      = data.aws_iam_policy_document.dispatcher.json
}
