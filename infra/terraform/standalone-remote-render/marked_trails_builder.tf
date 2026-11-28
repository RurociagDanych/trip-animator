# Marked Trails builder: an ECS Fargate task that runs the build-publish CLI, started by the
# Airflow DAG with boto3. It shares the trail bucket and prefix with the publisher policy.
# Everything here exists only when marked_trails_builder_enabled is true.

locals {
  marked_trails_builder_count = local.marked_trails_enabled && var.marked_trails_builder_enabled ? 1 : 0
  marked_trails_builder_name  = "${var.name_prefix}-marked-trails-builder"

  marked_trails_builder_tool_memory_mib   = var.marked_trails_builder_memory_mib - var.marked_trails_builder_headroom_mib
  marked_trails_builder_tool_memory_bytes = local.marked_trails_builder_tool_memory_mib * 1024 * 1024
  marked_trails_builder_tool_cpus         = var.marked_trails_builder_cpu / 1024
  marked_trails_builder_jvm_mib           = var.marked_trails_builder_planetiler_heap_mib + var.marked_trails_builder_planetiler_direct_memory_mib + var.marked_trails_builder_jvm_overhead_mib

  # Memory a task of each CPU size may have, in MiB: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html
  marked_trails_fargate_memory_rules = {
    "1024"  = { min = 2048, max = 8192, step = 1024 }
    "2048"  = { min = 4096, max = 16384, step = 1024 }
    "4096"  = { min = 8192, max = 30720, step = 1024 }
    "8192"  = { min = 16384, max = 61440, step = 4096 }
    "16384" = { min = 32768, max = 122880, step = 8192 }
  }
  marked_trails_memory_rule = local.marked_trails_fargate_memory_rules[tostring(var.marked_trails_builder_cpu)]

  # Heap needed next to the in-heap node map: Planetiler's feature sort buffers, the profile and
  # G1 regions that cannot be filled completely around one multi-GiB array.
  marked_trails_builder_heap_working_mib = 4096

  marked_trails_builder_task_arn_pattern = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task/${local.marked_trails_builder_name}/*"
}

resource "terraform_data" "marked_trails_builder_guard" {
  lifecycle {
    precondition {
      condition     = !var.marked_trails_builder_enabled || local.marked_trails_enabled
      error_message = "marked_trails_builder_enabled needs marked_trails_app_origins: the builder publishes to the Marked Trails bucket and uses its publisher policy."
    }
  }
}

data "aws_availability_zones" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  state = "available"
}

resource "aws_ecr_repository" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  name                 = local.marked_trails_builder_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# ECR lifecycle rules cannot exempt one tag, so the image the task definition pins is expired
# after five newer pushes. Pushing a new image means applying its tag first (see OPERATIONS.md),
# so the pinned image is always the newest one at deploy time and a rollback to any of the last
# five tags still works.
resource "aws_ecr_lifecycle_policy" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  repository = aws_ecr_repository.marked_trails_builder[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last 5 builder images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  name              = "/ecs/${local.marked_trails_builder_name}"
  retention_in_days = var.marked_trails_log_retention_days
}

resource "aws_vpc" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  cidr_block           = var.marked_trails_builder_vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = local.marked_trails_builder_name
  }
}

resource "aws_internet_gateway" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  vpc_id = aws_vpc.marked_trails_builder[0].id
}

# Two public subnets in different zones and no NAT gateway: the task reaches the internet through
# its own public IP, and RunTask can use the second zone when Fargate has no capacity in the first.
resource "aws_subnet" "marked_trails_builder" {
  count = local.marked_trails_builder_count * 2

  vpc_id                  = aws_vpc.marked_trails_builder[0].id
  cidr_block              = cidrsubnet(var.marked_trails_builder_vpc_cidr, 1, count.index)
  availability_zone       = data.aws_availability_zones.marked_trails_builder[0].names[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.marked_trails_builder_name}-public-${count.index}"
  }
}

resource "aws_route_table" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  vpc_id = aws_vpc.marked_trails_builder[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.marked_trails_builder[0].id
  }
}

resource "aws_route_table_association" "marked_trails_builder" {
  count = local.marked_trails_builder_count * 2

  subnet_id      = aws_subnet.marked_trails_builder[count.index].id
  route_table_id = aws_route_table.marked_trails_builder[0].id
}

# No ingress rules: the task only makes outbound calls. HTTPS covers Geofabrik, ECR, S3,
# CloudWatch Logs and ECS. Security groups do not filter queries to the VPC resolver (the VPC
# base address plus 2); the DNS rules only keep a resolver address inside the VPC CIDR reachable.
resource "aws_security_group" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  name_prefix = "${local.marked_trails_builder_name}-"
  description = "Marked Trails builder: outbound HTTPS and VPC DNS only"
  vpc_id      = aws_vpc.marked_trails_builder[0].id

  egress {
    description = "HTTPS to Geofabrik, ECR, S3, CloudWatch Logs and ECS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS over UDP inside the VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.marked_trails_builder_vpc_cidr]
  }

  egress {
    description = "DNS over TCP inside the VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.marked_trails_builder_vpc_cidr]
  }
}

resource "aws_ecs_cluster" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  name = local.marked_trails_builder_name
}

# aws:SourceAccount stops another account's ECS from assuming the roles through a confused deputy.
data "aws_iam_policy_document" "marked_trails_builder_assume" {
  count = local.marked_trails_builder_count

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# The execution role only pulls the image and writes the task's logs; the build itself uses the
# task role below.
resource "aws_iam_role" "marked_trails_builder_execution" {
  count = local.marked_trails_builder_count

  name               = "${local.marked_trails_builder_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.marked_trails_builder_assume[0].json
}

data "aws_iam_policy_document" "marked_trails_builder_execution" {
  count = local.marked_trails_builder_count

  statement {
    sid       = "EcrAuthorization"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullBuilderImage"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.marked_trails_builder[0].arn]
  }

  statement {
    sid    = "WriteBuilderLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.marked_trails_builder[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "marked_trails_builder_execution" {
  count = local.marked_trails_builder_count

  name   = "${local.marked_trails_builder_name}-execution"
  role   = aws_iam_role.marked_trails_builder_execution[0].id
  policy = data.aws_iam_policy_document.marked_trails_builder_execution[0].json
}

resource "aws_iam_role" "marked_trails_builder_task" {
  count = local.marked_trails_builder_count

  name               = "${local.marked_trails_builder_name}-task"
  assume_role_policy = data.aws_iam_policy_document.marked_trails_builder_assume[0].json
}

# The same policy as the Airflow publisher user: it can write and delete only under the trail
# prefix. The CLI reads its credentials from the task role when AWS_ACCESS_KEY_ID is not set.
resource "aws_iam_role_policy_attachment" "marked_trails_builder_task" {
  count = local.marked_trails_builder_count

  role       = aws_iam_role.marked_trails_builder_task[0].name
  policy_arn = aws_iam_policy.marked_trails_publisher[0].arn
}

resource "aws_ecs_task_definition" "marked_trails_builder" {
  count = local.marked_trails_builder_count

  family                   = local.marked_trails_builder_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.marked_trails_builder_cpu)
  memory                   = tostring(var.marked_trails_builder_memory_mib)
  execution_role_arn       = aws_iam_role.marked_trails_builder_execution[0].arn
  task_role_arn            = aws_iam_role.marked_trails_builder_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # Estimated peak disk of a Europe build is 17 to 19 GB and the worst case about 25 GB: the
  # largest country extract, the filtered and merged files, Planetiler's temporary files and the
  # output. 50 GiB leaves about twice the peak; the storage above 20 GiB is billed per GB-hour.
  ephemeral_storage {
    size_in_gib = var.marked_trails_builder_ephemeral_gib
  }

  # The container name must match CONTAINER_NAME in airflow/src/trip_airflow/marked_trails_fargate.py.
  # The tool caps leave var.marked_trails_builder_headroom_mib of the task memory for the Python CLI
  # and the page cache of Planetiler's feature storage, which prlimit does not count.
  container_definitions = jsonencode([
    {
      name      = "marked-trails-builder"
      image     = "${aws_ecr_repository.marked_trails_builder[0].repository_url}:${var.marked_trails_builder_image_tag}"
      essential = true
      # SIGTERM from a task stop reaches the build through an init process, and the 120 s grace
      # period is the longest Fargate allows.
      stopTimeout = 120
      linuxParameters = {
        initProcessEnabled = true
      }
      environment = [
        { name = "MARKED_TRAILS_STORE", value = "s3" },
        { name = "MARKED_TRAILS_S3_BUCKET", value = aws_s3_bucket.marked_trails[0].bucket },
        { name = "MARKED_TRAILS_S3_PREFIX", value = local.marked_trails_prefix },
        { name = "AWS_REGION", value = data.aws_region.current.name },
        { name = "MARKED_TRAILS_TOOL_CPUS", value = tostring(local.marked_trails_builder_tool_cpus) },
        { name = "MARKED_TRAILS_TOOL_MEMORY_BYTES", value = tostring(local.marked_trails_builder_tool_memory_bytes) },
        { name = "MARKED_TRAILS_PLANETILER_HEAP", value = "-Xmx${var.marked_trails_builder_planetiler_heap_mib}m" },
        { name = "MARKED_TRAILS_PLANETILER_DIRECT_MEMORY", value = "-XX:MaxDirectMemorySize=${var.marked_trails_builder_planetiler_direct_memory_mib}m" },
        { name = "MARKED_TRAILS_PLANETILER_NODEMAP_STORAGE", value = var.marked_trails_builder_nodemap_storage },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.marked_trails_builder[0].name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "marked-trails"
        }
      }
    },
  ])

  lifecycle {
    precondition {
      condition     = var.marked_trails_builder_image_tag != ""
      error_message = "Set marked_trails_builder_image_tag to the tag pushed by make marked-trails-ecr-push; it has no default so that no task definition points at an image that does not exist."
    }

    precondition {
      condition     = contains(keys(local.marked_trails_fargate_memory_rules), tostring(var.marked_trails_builder_cpu))
      error_message = "marked_trails_builder_cpu must be 1024, 2048, 4096, 8192 or 16384."
    }

    precondition {
      condition = (
        var.marked_trails_builder_memory_mib >= local.marked_trails_memory_rule.min &&
        var.marked_trails_builder_memory_mib <= local.marked_trails_memory_rule.max &&
        (var.marked_trails_builder_memory_mib - local.marked_trails_memory_rule.min) % local.marked_trails_memory_rule.step == 0
      )
      error_message = "marked_trails_builder_memory_mib is not a valid Fargate memory size for marked_trails_builder_cpu: it must be ${local.marked_trails_memory_rule.min} to ${local.marked_trails_memory_rule.max} MiB in steps of ${local.marked_trails_memory_rule.step}."
    }

    precondition {
      condition     = local.marked_trails_builder_tool_memory_mib > 0 && local.marked_trails_builder_jvm_mib <= local.marked_trails_builder_tool_memory_mib
      error_message = "Planetiler heap plus direct memory plus JVM overhead (${local.marked_trails_builder_jvm_mib} MiB) must fit under the tool memory cap, which is marked_trails_builder_memory_mib minus marked_trails_builder_headroom_mib (${local.marked_trails_builder_tool_memory_mib} MiB)."
    }

    precondition {
      condition     = var.marked_trails_builder_nodemap_storage != "ram" || var.marked_trails_builder_planetiler_heap_mib >= var.marked_trails_builder_node_map_mib + local.marked_trails_builder_heap_working_mib
      error_message = "With marked_trails_builder_nodemap_storage = \"ram\" the heap must hold the node map (marked_trails_builder_node_map_mib) plus ${local.marked_trails_builder_heap_working_mib} MiB of working memory."
    }
  }
}

# Lets the Airflow publisher user start, find, stop and describe the builder tasks and read their
# logs, and nothing else. StopTask is needed so that a killed or timed-out Airflow task can stop
# its Fargate task, and ListTasks so that the next run can stop tasks a killed launcher left.
data "aws_iam_policy_document" "marked_trails_launcher" {
  count = local.marked_trails_builder_count

  statement {
    sid       = "RunBuilderTask"
    effect    = "Allow"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.marked_trails_builder[0].arn, "/:[0-9]+$/", ":*")]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.marked_trails_builder[0].arn]
    }
  }

  statement {
    sid    = "ManageBuilderTasks"
    effect = "Allow"
    actions = [
      "ecs:DescribeTasks",
      "ecs:StopTask",
    ]
    resources = [local.marked_trails_builder_task_arn_pattern]
  }

  # ListTasks has no resource level permissions, so the cluster condition is the scope.
  statement {
    sid       = "ListBuilderTasks"
    effect    = "Allow"
    actions   = ["ecs:ListTasks"]
    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.marked_trails_builder[0].arn]
    }
  }

  statement {
    sid       = "PassBuilderRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.marked_trails_builder_execution[0].arn, aws_iam_role.marked_trails_builder_task[0].arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid    = "ReadBuilderLogs"
    effect = "Allow"
    actions = [
      "logs:DescribeLogStreams",
      "logs:FilterLogEvents",
      "logs:GetLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.marked_trails_builder[0].arn,
      "${aws_cloudwatch_log_group.marked_trails_builder[0].arn}:*",
    ]
  }
}

resource "aws_iam_policy" "marked_trails_launcher" {
  count = local.marked_trails_builder_count

  name        = "${var.name_prefix}-marked-trails-launcher-policy"
  description = "Least-privilege policy for the Airflow Marked Trails Fargate launcher."
  policy      = data.aws_iam_policy_document.marked_trails_launcher[0].json
}
