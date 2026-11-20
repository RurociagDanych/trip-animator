data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_vpc" "main" {
  id = var.vpc_id
}

locals {
  ecr_services = toset(["api", "worker", "airflow"])
  log_services = toset(["api", "worker", "migrate", "reap", "dbt", "ingest"])

  objects_bucket_arn  = "arn:aws:s3:::${var.objects_bucket_name}"
  s3_endpoint_url     = "https://s3.${data.aws_region.current.name}.amazonaws.com"
  has_custom_domain   = var.domain_name != ""
  public_base_url     = local.has_custom_domain ? "https://${var.domain_name}" : ""
  strava_redirect_uri = local.has_custom_domain ? "https://${var.domain_name}/strava/callback" : ""

  public_base_url_ssm_arn     = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${var.name_prefix}/public-base-url"
  strava_redirect_uri_ssm_arn = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${var.name_prefix}/strava-redirect-uri"

  ecs_task_subnets     = var.enable_nat_gateway ? var.private_subnet_ids : var.public_subnet_ids
  ecs_assign_public_ip = !var.enable_nat_gateway

  enable_metric_publisher = var.enable_nat_gateway && var.enable_queue_metric_publisher

  active_listener_arn = var.alb_acm_certificate_arn != "" ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
}

data "aws_iam_policy_document" "compute_kms" {
  statement {
    sid    = "EnableAccountAdministration"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudWatchLogs"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.name}.amazonaws.com"]
    }

    actions = [
      "kms:Encrypt*",
      "kms:Decrypt*",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_kms_key" "compute" {
  description             = "KMS key for ${var.name_prefix} ECR, CloudWatch Logs, and app secrets"
  deletion_window_in_days = var.kms_deletion_window_days
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.compute_kms.json

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-compute-kms"
  })
}

resource "aws_kms_alias" "compute" {
  name          = "alias/${var.name_prefix}-compute"
  target_key_id = aws_kms_key.compute.key_id
}

resource "aws_ecr_repository" "services" {
  for_each = local.ecr_services

  name                 = "${var.name_prefix}-${each.key}"
  image_tag_mutability = var.ecr_image_tag_mutability

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.compute.arn
  }

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-${each.key}"
    Service = each.key
  })
}

resource "aws_ecr_lifecycle_policy" "services" {
  for_each = aws_ecr_repository.services

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 7 days"
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
        description  = "Keep last ${var.ecr_max_image_count} tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "sha-", "latest", "main"]
          countType     = "imageCountMoreThan"
          countNumber   = var.ecr_max_image_count
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "random_password" "session_secret" {
  length  = 48
  special = false
}

resource "random_bytes" "token_encryption_key" {
  length = 32
}

resource "random_password" "metrics_token" {
  length  = 32
  special = false
}

resource "random_password" "origin_verify" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name_prefix             = "${var.name_prefix}-app-secrets-"
  description             = "Runtime secrets for ${var.name_prefix} (populate Strava credentials out-of-band)"
  kms_key_id              = aws_kms_key.compute.arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-app-secrets"
  })
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    SESSION_SECRET       = random_password.session_secret.result
    TOKEN_ENCRYPTION_KEY = random_bytes.token_encryption_key.base64
    METRICS_TOKEN        = random_password.metrics_token.result
    ORIGIN_VERIFY_SECRET = random_password.origin_verify.result
    STRAVA_CLIENT_ID     = ""
    STRAVA_CLIENT_SECRET = ""
    STRAVA_VERIFY_TOKEN  = ""
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_cloudwatch_log_group" "ecs" {
  for_each = local.log_services

  name              = "/ecs/${var.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.compute.arn

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-${each.key}-logs"
    Service = each.key
  })
}

resource "aws_lb" "main" {
  name_prefix                = "ta-"
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [var.alb_security_group_id]
  subnets                    = var.public_subnet_ids
  drop_invalid_header_fields = true
  idle_timeout               = 120

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-alb"
  })
}

resource "aws_lb_target_group" "api" {
  name_prefix          = "ta-api"
  port                 = var.api_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-api-tg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.alb_acm_certificate_arn != "" ? [1] : []

    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.alb_acm_certificate_arn == "" && var.enable_origin_verify ? [1] : []

    content {
      type = "fixed-response"
      fixed_response {
        content_type = "text/plain"
        message_body = "Forbidden"
        status_code  = "403"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.alb_acm_certificate_arn == "" && !var.enable_origin_verify ? [1] : []

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.alb_acm_certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.alb_acm_certificate_arn

  dynamic "default_action" {
    for_each = var.enable_origin_verify ? [1] : []

    content {
      type = "fixed-response"
      fixed_response {
        content_type = "text/plain"
        message_body = "Forbidden"
        status_code  = "403"
      }
    }
  }

  dynamic "default_action" {
    for_each = !var.enable_origin_verify ? [1] : []

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }
}

resource "aws_lb_listener_rule" "block_metrics" {
  listener_arn = local.active_listener_arn
  priority     = 10

  action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }

  condition {
    path_pattern {
      values = ["/metrics", "/metrics/*"]
    }
  }
}

resource "aws_lb_listener_rule" "allow_verified_origin" {
  count = var.enable_origin_verify ? 1 : 0

  listener_arn = local.active_listener_arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    path_pattern {
      values = ["/api/*", "/health", "/ready"]
    }
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_verify.result]
    }
  }
}

resource "aws_ecs_cluster" "main" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-cluster"
  })
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 1
    capacity_provider = "FARGATE"
  }
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name_prefix        = "${var.name_prefix}-ecs-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "ReadScopedSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.db_secret_arn, aws_secretsmanager_secret.app.arn]
  }

  statement {
    sid       = "ReadScopedSsmParameters"
    effect    = "Allow"
    actions   = ["ssm:GetParameters"]
    resources = [local.public_base_url_ssm_arn, local.strava_redirect_uri_ssm_arn]
  }

  statement {
    sid       = "DecryptScopedKmsKeys"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.rds_kms_key_arn, aws_kms_key.compute.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.name_prefix}-exec-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "api_task" {
  name_prefix        = "${var.name_prefix}-api-task-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "api_task_s3" {
  statement {
    sid       = "ListObjectsBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.objects_bucket_arn]
  }

  statement {
    sid    = "ReadWriteDeleteObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${local.objects_bucket_arn}/*"]
  }

  statement {
    sid    = "UseObjectsKmsViaS3"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["arn:aws:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:key/*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "api_task_s3" {
  name   = "${var.name_prefix}-api-s3"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task_s3.json
}

resource "aws_iam_role" "worker_task" {
  name_prefix        = "${var.name_prefix}-worker-task-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "worker_task_s3" {
  statement {
    sid       = "ListObjectsBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.objects_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["bronze/*", "silver/*", "outputs/*", "renders/*"]
    }
  }

  statement {
    sid     = "ReadBronzeAndSilverObjects"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${local.objects_bucket_arn}/bronze/*",
      "${local.objects_bucket_arn}/silver/*",
    ]
  }

  statement {
    sid    = "WriteAndCancelRenderArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${local.objects_bucket_arn}/outputs/*",
      "${local.objects_bucket_arn}/renders/*",
    ]
  }

  statement {
    sid    = "UseObjectsKmsViaS3"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["arn:aws:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:key/*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "worker_task_s3" {
  name   = "${var.name_prefix}-worker-s3"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task_s3.json
}

# Migrate task role intentionally has no S3 permissions
resource "aws_iam_role" "migrate_task" {
  name_prefix        = "${var.name_prefix}-migrate-task-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

resource "aws_iam_role" "reap_task" {
  name_prefix        = "${var.name_prefix}-reap-task-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "reap_task_s3" {
  statement {
    sid       = "ListObjectsBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.objects_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["outputs/*", "renders/*"]
    }
  }

  statement {
    sid    = "DeleteExpiredRenderObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${local.objects_bucket_arn}/outputs/*",
      "${local.objects_bucket_arn}/renders/*",
    ]
  }

  statement {
    sid       = "DecryptObjectsKmsViaS3"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:key/*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "reap_task_s3" {
  name   = "${var.name_prefix}-reap-s3"
  role   = aws_iam_role.reap_task.id
  policy = data.aws_iam_policy_document.reap_task_s3.json
}

resource "aws_iam_role" "dbt_task" {
  name_prefix        = "${var.name_prefix}-dbt-task-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "dbt_task_s3" {
  statement {
    sid       = "ListObjectsBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.objects_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["silver/*", "gold/*"]
    }
  }

  statement {
    sid       = "ReadSilverObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${local.objects_bucket_arn}/silver/*"]
  }

  statement {
    sid       = "WriteGoldObjects"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${local.objects_bucket_arn}/gold/*"]
  }

  statement {
    sid    = "UseObjectsKmsViaS3"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["arn:aws:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:key/*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "dbt_task_s3" {
  name   = "${var.name_prefix}-dbt-s3"
  role   = aws_iam_role.dbt_task.id
  policy = data.aws_iam_policy_document.dbt_task_s3.json
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.api_cpu)
  memory                   = tostring(var.api_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "api"
      image                  = "${aws_ecr_repository.services["api"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true

      linuxParameters = {
        capabilities = {
          drop = ["ALL"]
        }
      }

      portMappings = [
        {
          containerPort = var.api_port
          hostPort      = var.api_port
          protocol      = "tcp"
        }
      ]

      mountPoints = [
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
          readOnly      = false
        }
      ]

      environment = concat([
        { name = "APP_ENV", value = "production" },
        { name = "LOG_LEVEL", value = var.log_level },
        { name = "DEV_AUTH_ENABLED", value = "false" },
        { name = "OBJECT_STORE_BACKEND", value = "s3" },
        { name = "S3_ENDPOINT_URL", value = local.s3_endpoint_url },
        { name = "S3_BUCKET", value = var.objects_bucket_name },
        { name = "S3_REGION", value = data.aws_region.current.name },
        { name = "GOLD_OUTPUT_DIR", value = "/tmp/gold" },
        { name = "GOLD_SYNC_INTERVAL_S", value = "60" },
        { name = "MAP_TILE_ALLOWLIST", value = var.map_tile_allowlist },
        { name = "RENDER_RETENTION_DAYS", value = tostring(var.render_retention_days) },
        { name = "FORWARDED_ALLOW_IPS", value = data.aws_vpc.main.cidr_block },
        ], local.has_custom_domain ? [
        { name = "ALLOWED_ORIGINS", value = local.public_base_url },
        { name = "STRAVA_REDIRECT_URI", value = local.strava_redirect_uri },
      ] : [])

      secrets = concat([
        { name = "DATABASE_URL", valueFrom = "${var.db_secret_arn}:database_url::" },
        { name = "SESSION_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:SESSION_SECRET::" },
        { name = "TOKEN_ENCRYPTION_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:TOKEN_ENCRYPTION_KEY::" },
        { name = "STRAVA_CLIENT_ID", valueFrom = "${aws_secretsmanager_secret.app.arn}:STRAVA_CLIENT_ID::" },
        { name = "STRAVA_CLIENT_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:STRAVA_CLIENT_SECRET::" },
        { name = "METRICS_TOKEN", valueFrom = "${aws_secretsmanager_secret.app.arn}:METRICS_TOKEN::" },
        ], local.has_custom_domain ? [] : [
        { name = "ALLOWED_ORIGINS", valueFrom = local.public_base_url_ssm_arn },
        { name = "STRAVA_REDIRECT_URI", valueFrom = local.strava_redirect_uri_ssm_arn },
      ])

      healthCheck = {
        command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.api_port}/health', timeout=2)"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs["api"].name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "api"
        }
      }
    }
  ])

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-api-td"
  })
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name_prefix}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.worker_cpu)
  memory                   = tostring(var.worker_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  ephemeral_storage {
    size_in_gib = var.worker_ephemeral_storage_gib
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "worker"
      image                  = "${aws_ecr_repository.services["worker"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true

      linuxParameters = {
        capabilities = {
          drop = ["ALL"]
        }
      }

      mountPoints = [
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
          readOnly      = false
        }
      ]

      environment = concat([
        { name = "APP_ENV", value = "production" },
        { name = "LOG_LEVEL", value = var.log_level },
        { name = "OBJECT_STORE_BACKEND", value = "s3" },
        { name = "S3_ENDPOINT_URL", value = local.s3_endpoint_url },
        { name = "S3_BUCKET", value = var.objects_bucket_name },
        { name = "S3_REGION", value = data.aws_region.current.name },
        { name = "WORKER_CONCURRENCY", value = tostring(var.worker_concurrency) },
        { name = "MAP_TILE_ALLOWLIST", value = var.map_tile_allowlist },
        ], local.has_custom_domain ? [
        { name = "RENDER_BASE_URL", value = local.public_base_url },
      ] : [])

      secrets = concat([
        { name = "DATABASE_URL", valueFrom = "${var.db_secret_arn}:database_url::" },
        ], local.has_custom_domain ? [] : [
        { name = "RENDER_BASE_URL", valueFrom = local.public_base_url_ssm_arn },
      ])

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs["worker"].name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-worker-td"
  })
}

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name_prefix}-migrate"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.migrate_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "migrate"
      image                  = "${aws_ecr_repository.services["api"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      workingDirectory       = "/app/apps/api"
      command                = ["alembic", "upgrade", "head"]

      linuxParameters = {
        capabilities = {
          drop = ["ALL"]
        }
      }

      mountPoints = [
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
          readOnly      = false
        }
      ]

      environment = [
        { name = "APP_ENV", value = "production" },
        { name = "LOG_LEVEL", value = var.log_level },
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = "${var.db_secret_arn}:database_url::" },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs["migrate"].name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "migrate"
        }
      }
    }
  ])

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-migrate-td"
  })
}

resource "aws_ecs_task_definition" "reap" {
  family                   = "${var.name_prefix}-reap"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.reap_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "reap"
      image                  = "${aws_ecr_repository.services["api"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      command                = ["python", "-m", "trip_api.cli", "reap", "--json"]

      linuxParameters = {
        capabilities = {
          drop = ["ALL"]
        }
      }

      mountPoints = [
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
          readOnly      = false
        }
      ]

      environment = concat([
        { name = "APP_ENV", value = "production" },
        { name = "LOG_LEVEL", value = var.log_level },
        { name = "DEV_AUTH_ENABLED", value = "false" },
        { name = "OBJECT_STORE_BACKEND", value = "s3" },
        { name = "S3_ENDPOINT_URL", value = local.s3_endpoint_url },
        { name = "S3_BUCKET", value = var.objects_bucket_name },
        { name = "S3_REGION", value = data.aws_region.current.name },
        { name = "RENDER_RETENTION_DAYS", value = tostring(var.render_retention_days) },
        ], local.has_custom_domain ? [
        { name = "ALLOWED_ORIGINS", value = local.public_base_url },
      ] : [])

      secrets = concat([
        { name = "DATABASE_URL", valueFrom = "${var.db_secret_arn}:database_url::" },
        { name = "SESSION_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:SESSION_SECRET::" },
        { name = "TOKEN_ENCRYPTION_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:TOKEN_ENCRYPTION_KEY::" },
        ], local.has_custom_domain ? [] : [
        { name = "ALLOWED_ORIGINS", valueFrom = local.public_base_url_ssm_arn },
      ])

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs["reap"].name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "reap"
        }
      }
    }
  ])

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-reap-td"
  })
}

resource "aws_ecs_task_definition" "dbt" {
  family                   = "${var.name_prefix}-dbt"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.dbt_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "dbt"
      image                  = "${aws_ecr_repository.services["airflow"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      command = [
        "python",
        "-c",
        "from pathlib import Path; from trip_airflow.dbt_gold import discover_silver_parquets, materialize_gold_marts, validate_gold_marts; from trip_airflow.runtime import open_runtime; rt = open_runtime().__enter__(); disc = discover_silver_parquets(rt.store); res = materialize_gold_marts(rt.store) if disc.get('silver_count', 0) > 0 else None; validate_gold_marts(expected_stage_count=res['silver_count']) if res else None; rt.store.put_bytes('gold/trip_analytics.duckdb', Path('/tmp/gold/trip_analytics.duckdb').read_bytes(), content_type='application/octet-stream') if res else None",
      ]

      linuxParameters = {
        capabilities = {
          drop = ["ALL"]
        }
      }

      mountPoints = [
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
          readOnly      = false
        }
      ]

      environment = [
        { name = "APP_ENV", value = "production" },
        { name = "HOME", value = "/tmp" },
        { name = "DBT_SEND_ANONYMOUS_USAGE_STATS", value = "false" },
        { name = "LOG_LEVEL", value = var.log_level },
        { name = "DEV_AUTH_ENABLED", value = "false" },
        { name = "OBJECT_STORE_BACKEND", value = "s3" },
        { name = "S3_ENDPOINT_URL", value = local.s3_endpoint_url },
        { name = "S3_BUCKET", value = var.objects_bucket_name },
        { name = "S3_REGION", value = data.aws_region.current.name },
        { name = "GOLD_OUTPUT_DIR", value = "/tmp/gold" },
        { name = "TRIP_DBT_PROJECT_DIR", value = "/app/dbt" },
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = "${var.db_secret_arn}:database_url::" },
        { name = "SESSION_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:SESSION_SECRET::" },
        { name = "TOKEN_ENCRYPTION_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:TOKEN_ENCRYPTION_KEY::" },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs["dbt"].name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "dbt"
        }
      }
    }
  ])

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-dbt-td"
  })
}

resource "aws_ecs_task_definition" "ingest" {
  family                   = "${var.name_prefix}-ingest"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "ingest"
      image                  = "${aws_ecr_repository.services["api"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      command                = ["python", "-m", "trip_api.cli", "process-ingest-jobs", "--json"]

      linuxParameters = {
        capabilities = {
          drop = ["ALL"]
        }
      }

      mountPoints = [
        {
          sourceVolume  = "tmp"
          containerPath = "/tmp"
          readOnly      = false
        }
      ]

      environment = concat([
        { name = "APP_ENV", value = "production" },
        { name = "LOG_LEVEL", value = var.log_level },
        { name = "DEV_AUTH_ENABLED", value = "false" },
        { name = "OBJECT_STORE_BACKEND", value = "s3" },
        { name = "S3_ENDPOINT_URL", value = local.s3_endpoint_url },
        { name = "S3_BUCKET", value = var.objects_bucket_name },
        { name = "S3_REGION", value = data.aws_region.current.name },
        ], local.has_custom_domain ? [
        { name = "ALLOWED_ORIGINS", value = local.public_base_url },
        { name = "STRAVA_REDIRECT_URI", value = local.strava_redirect_uri },
      ] : [])

      secrets = concat([
        { name = "DATABASE_URL", valueFrom = "${var.db_secret_arn}:database_url::" },
        { name = "SESSION_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:SESSION_SECRET::" },
        { name = "TOKEN_ENCRYPTION_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:TOKEN_ENCRYPTION_KEY::" },
        { name = "STRAVA_CLIENT_ID", valueFrom = "${aws_secretsmanager_secret.app.arn}:STRAVA_CLIENT_ID::" },
        { name = "STRAVA_CLIENT_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:STRAVA_CLIENT_SECRET::" },
        ], local.has_custom_domain ? [] : [
        { name = "ALLOWED_ORIGINS", valueFrom = local.public_base_url_ssm_arn },
        { name = "STRAVA_REDIRECT_URI", valueFrom = local.strava_redirect_uri_ssm_arn },
      ])

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs["ingest"].name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "ingest"
        }
      }
    }
  ])

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-ingest-td"
  })
}

resource "aws_ecs_service" "api" {
  name                               = "${var.name_prefix}-api"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.api.arn
  desired_count                      = var.api_min_capacity
  launch_type                        = "FARGATE"
  health_check_grace_period_seconds  = 60
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  network_configuration {
    subnets          = local.ecs_task_subnets
    security_groups  = [var.api_security_group_id]
    assign_public_ip = local.ecs_assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_port
  }

  dynamic "service_registries" {
    for_each = local.enable_metric_publisher ? [1] : []

    content {
      registry_arn = aws_service_discovery_service.api[0].arn
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.http]

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-api-service"
  })
}

resource "aws_appautoscaling_target" "api" {
  max_capacity       = var.api_max_capacity
  min_capacity       = var.api_min_capacity
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${var.name_prefix}-api-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 65.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_policy" "api_requests" {
  name               = "${var.name_prefix}-api-alb-requests-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.main.arn_suffix}/${aws_lb_target_group.api.arn_suffix}"
    }
    target_value       = 1000.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_ecs_service" "worker" {
  name                               = "${var.name_prefix}-worker"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.worker.arn
  desired_count                      = var.worker_min_capacity
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 100
    base              = 0
  }

  network_configuration {
    subnets          = local.ecs_task_subnets
    security_groups  = [var.worker_security_group_id]
    assign_public_ip = local.ecs_assign_public_ip
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-worker-service"
  })
}

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = var.worker_max_capacity
  min_capacity       = var.worker_min_capacity
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_scale_out" {
  name               = "${var.name_prefix}-worker-queue-scale-out"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 2
      scaling_adjustment          = 1
    }

    step_adjustment {
      metric_interval_lower_bound = 2
      scaling_adjustment          = 2
    }
  }
}

resource "aws_appautoscaling_policy" "worker_scale_in" {
  name               = "${var.name_prefix}-worker-queue-scale-in"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 300
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = var.worker_min_capacity
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_queue_nonempty" {
  alarm_name          = "${var.name_prefix}-render-queue-nonempty"
  alarm_description   = "Scale out ECS render worker when ActiveRenderJobs >= 1"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ActiveRenderJobs"
  namespace           = "TripAnimator/RenderQueueDepth"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    Environment = var.name_prefix
  }

  alarm_actions = [aws_appautoscaling_policy.worker_scale_out.arn]
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "worker_queue_empty" {
  alarm_name          = "${var.name_prefix}-render-queue-empty"
  alarm_description   = "Scale in ECS render worker to min_capacity after 15m of zero ActiveRenderJobs"
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = 15
  metric_name         = "ActiveRenderJobs"
  namespace           = "TripAnimator/RenderQueueDepth"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    Environment = var.name_prefix
  }

  alarm_actions = [aws_appautoscaling_policy.worker_scale_in.arn]
  tags          = var.tags
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name_prefix        = "${var.name_prefix}-scheduler-"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "scheduler_ecs" {
  statement {
    sid     = "RunScheduledEcsTasks"
    effect  = "Allow"
    actions = ["ecs:RunTask"]
    resources = compact([
      aws_ecs_task_definition.reap.arn,
      var.enable_dbt_schedule ? aws_ecs_task_definition.dbt.arn : "",
      var.enable_ingest_schedule ? aws_ecs_task_definition.ingest.arn : "",
    ])

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }

  statement {
    sid     = "PassTaskRoles"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = compact([
      aws_iam_role.execution.arn,
      aws_iam_role.reap_task.arn,
      var.enable_dbt_schedule ? aws_iam_role.dbt_task.arn : "",
      var.enable_ingest_schedule ? aws_iam_role.api_task.arn : "",
    ])
  }
}

resource "aws_iam_role_policy" "scheduler_ecs" {
  name   = "${var.name_prefix}-scheduler-ecs"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_ecs.json
}

resource "aws_scheduler_schedule" "reap" {
  name       = "${var.name_prefix}-reap-daily"
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = var.reap_schedule_expression

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.reap.arn
      task_count          = 1

      capacity_provider_strategy {
        capacity_provider = "FARGATE_SPOT"
        weight            = 100
      }

      network_configuration {
        subnets          = local.ecs_task_subnets
        security_groups  = [var.api_security_group_id]
        assign_public_ip = local.ecs_assign_public_ip
      }
    }
  }
}

resource "aws_scheduler_schedule" "dbt" {
  count = var.enable_dbt_schedule ? 1 : 0

  name       = "${var.name_prefix}-dbt-gold"
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = var.dbt_schedule_expression

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.dbt.arn
      task_count          = 1

      capacity_provider_strategy {
        capacity_provider = "FARGATE_SPOT"
        weight            = 100
      }

      network_configuration {
        subnets          = local.ecs_task_subnets
        security_groups  = [var.api_security_group_id]
        assign_public_ip = local.ecs_assign_public_ip
      }
    }
  }
}

resource "aws_scheduler_schedule" "ingest" {
  count = var.enable_ingest_schedule ? 1 : 0

  name       = "${var.name_prefix}-ingest-jobs"
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = var.ingest_schedule_expression

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.ingest.arn
      task_count          = 1

      capacity_provider_strategy {
        capacity_provider = "FARGATE_SPOT"
        weight            = 100
      }

      network_configuration {
        subnets          = local.ecs_task_subnets
        security_groups  = [var.api_security_group_id]
        assign_public_ip = local.ecs_assign_public_ip
      }
    }
  }
}

resource "aws_service_discovery_private_dns_namespace" "internal" {
  count = local.enable_metric_publisher ? 1 : 0

  name        = "${var.name_prefix}.internal"
  description = "Private Cloud Map namespace for internal API metrics collection"
  vpc         = var.vpc_id

  tags = var.tags
}

resource "aws_service_discovery_service" "api" {
  count = local.enable_metric_publisher ? 1 : 0

  name = "api"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal[0].id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = var.tags
}

resource "aws_security_group" "metrics_lambda" {
  count = local.enable_metric_publisher ? 1 : 0

  name_prefix = "${var.name_prefix}-metrics-lambda-"
  description = "Queue depth metric publisher Lambda security group"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-metrics-lambda-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "metrics_lambda_to_api" {
  count = local.enable_metric_publisher ? 1 : 0

  security_group_id            = aws_security_group.metrics_lambda[0].id
  description                  = "Scrape internal /metrics endpoint on API tasks"
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
  referenced_security_group_id = var.api_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "metrics_lambda_https" {
  count = local.enable_metric_publisher ? 1 : 0

  security_group_id = aws_security_group.metrics_lambda[0].id
  description       = "Allow HTTPS to Secrets Manager and CloudWatch PutMetricData"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "api_from_metrics_lambda" {
  count = local.enable_metric_publisher ? 1 : 0

  security_group_id            = var.api_security_group_id
  description                  = "Allow internal /metrics scrape from queue-depth Lambda"
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
  referenced_security_group_id = aws_security_group.metrics_lambda[0].id
}

data "archive_file" "queue_depth" {
  count = local.enable_metric_publisher ? 1 : 0

  type        = "zip"
  output_path = "${path.module}/.queue_depth_lambda.zip"

  source {
    filename = "index.py"
    content  = <<-PY
      import json
      import os
      import re
      import urllib.request
      import boto3

      _METRIC_RE = re.compile(r'^trip_render_jobs_total\{status="(queued|running)"\}\s+(\d+(?:\.\d+)?)$', re.MULTILINE)
      _SM = boto3.client("secretsmanager")
      _CW = boto3.client("cloudwatch")
      _CACHED_TOKEN = None


      def _get_metrics_token() -> str:
          global _CACHED_TOKEN
          if _CACHED_TOKEN:
              return _CACHED_TOKEN
          resp = _SM.get_secret_value(SecretId=os.environ["APP_SECRET_ARN"])
          data = json.loads(resp["SecretString"])
          _CACHED_TOKEN = data["METRICS_TOKEN"]
          return _CACHED_TOKEN


      def handler(event, context):
          token = _get_metrics_token()
          req = urllib.request.Request(
              os.environ["METRICS_URL"],
              headers={"Authorization": f"Bearer {token}"},
          )
          with urllib.request.urlopen(req, timeout=5) as resp:
              body = resp.read().decode("utf-8")

          active = sum(int(float(match.group(2))) for match in _METRIC_RE.finditer(body))
          _CW.put_metric_data(
              Namespace="TripAnimator/RenderQueueDepth",
              MetricData=[
                  {
                      "MetricName": "ActiveRenderJobs",
                      "Dimensions": [
                          {"Name": "Environment", "Value": os.environ["ENV_NAME"]},
                      ],
                      "Value": active,
                      "Unit": "Count",
                  }
              ],
          )
          return {"active_render_jobs": active}
    PY
  }
}

data "aws_iam_policy_document" "queue_depth_assume" {
  count = local.enable_metric_publisher ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "queue_depth_lambda" {
  count = local.enable_metric_publisher ? 1 : 0

  name_prefix        = "${var.name_prefix}-qdepth-lambda-"
  assume_role_policy = data.aws_iam_policy_document.queue_depth_assume[0].json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "queue_depth_vpc" {
  count = local.enable_metric_publisher ? 1 : 0

  role       = aws_iam_role.queue_depth_lambda[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "queue_depth_policy" {
  count = local.enable_metric_publisher ? 1 : 0

  statement {
    sid       = "ReadAppMetricsToken"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
  }

  statement {
    sid       = "DecryptComputeKmsKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.compute.arn]
  }

  statement {
    sid       = "PublishRenderQueueMetric"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["TripAnimator/RenderQueueDepth"]
    }
  }
}

resource "aws_iam_role_policy" "queue_depth_lambda" {
  count = local.enable_metric_publisher ? 1 : 0

  name   = "${var.name_prefix}-qdepth-policy"
  role   = aws_iam_role.queue_depth_lambda[0].id
  policy = data.aws_iam_policy_document.queue_depth_policy[0].json
}

resource "aws_cloudwatch_log_group" "queue_depth_lambda" {
  count = local.enable_metric_publisher ? 1 : 0

  name              = "/aws/lambda/${var.name_prefix}-queue-depth-publisher"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.compute.arn

  tags = var.tags
}

resource "aws_lambda_function" "queue_depth_publisher" {
  count = local.enable_metric_publisher ? 1 : 0

  function_name    = "${var.name_prefix}-queue-depth-publisher"
  role             = aws_iam_role.queue_depth_lambda[0].arn
  handler          = "index.handler"
  runtime          = "python3.12"
  architectures    = [var.cpu_architecture == "ARM64" ? "arm64" : "x86_64"]
  timeout          = 15
  memory_size      = 128
  filename         = data.archive_file.queue_depth[0].output_path
  source_code_hash = data.archive_file.queue_depth[0].output_base64sha256

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.metrics_lambda[0].id]
  }

  environment {
    variables = {
      APP_SECRET_ARN = aws_secretsmanager_secret.app.arn
      METRICS_URL    = "http://api.${var.name_prefix}.internal:${var.api_port}/metrics"
      ENV_NAME       = var.name_prefix
    }
  }

  depends_on = [aws_cloudwatch_log_group.queue_depth_lambda]
  tags       = var.tags
}

resource "aws_cloudwatch_event_rule" "queue_depth_1m" {
  count = local.enable_metric_publisher ? 1 : 0

  name                = "${var.name_prefix}-queue-depth-1m"
  description         = "Publish TripAnimator/RenderQueueDepth metric every 1 minute"
  schedule_expression = "rate(1 minute)"

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "queue_depth_1m" {
  count = local.enable_metric_publisher ? 1 : 0

  rule      = aws_cloudwatch_event_rule.queue_depth_1m[0].name
  target_id = "queue-depth-publisher"
  arn       = aws_lambda_function.queue_depth_publisher[0].arn
}

resource "aws_lambda_permission" "queue_depth_events" {
  count = local.enable_metric_publisher ? 1 : 0

  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.queue_depth_publisher[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.queue_depth_1m[0].arn
}
