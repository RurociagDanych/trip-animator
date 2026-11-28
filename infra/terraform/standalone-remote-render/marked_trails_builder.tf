# Marked Trails builder: an ECS Fargate task that runs the build-publish CLI, started by the
# Airflow DAG with boto3. It shares the trail bucket and prefix with the publisher policy.

data "aws_availability_zones" "marked_trails_builder" {
  count = local.marked_trails_count

  state = "available"
}

locals {
  marked_trails_builder_name = "${var.name_prefix}-marked-trails-builder"
  marked_trails_builder_tool_memory_bytes = (
    var.marked_trails_builder_memory_mib - var.marked_trails_builder_headroom_mib
  ) * 1024 * 1024
  marked_trails_builder_tool_cpus = var.marked_trails_builder_cpu / 1024
}

resource "aws_ecr_repository" "marked_trails_builder" {
  count = local.marked_trails_count

  name                 = local.marked_trails_builder_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "marked_trails_builder" {
  count = local.marked_trails_count

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
  count = local.marked_trails_count

  name              = "/ecs/${local.marked_trails_builder_name}"
  retention_in_days = var.marked_trails_log_retention_days
}

resource "aws_vpc" "marked_trails_builder" {
  count = local.marked_trails_count

  cidr_block           = var.marked_trails_builder_vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = local.marked_trails_builder_name
  }
}

resource "aws_internet_gateway" "marked_trails_builder" {
  count = local.marked_trails_count

  vpc_id = aws_vpc.marked_trails_builder[0].id
}

# One public subnet and no NAT gateway: the task reaches the internet through its own public IP.
resource "aws_subnet" "marked_trails_builder" {
  count = local.marked_trails_count

  vpc_id                  = aws_vpc.marked_trails_builder[0].id
  cidr_block              = var.marked_trails_builder_vpc_cidr
  availability_zone       = data.aws_availability_zones.marked_trails_builder[0].names[0]
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.marked_trails_builder_name}-public"
  }
}

resource "aws_route_table" "marked_trails_builder" {
  count = local.marked_trails_count

  vpc_id = aws_vpc.marked_trails_builder[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.marked_trails_builder[0].id
  }
}

resource "aws_route_table_association" "marked_trails_builder" {
  count = local.marked_trails_count

  subnet_id      = aws_subnet.marked_trails_builder[0].id
  route_table_id = aws_route_table.marked_trails_builder[0].id
}

# No ingress rules: the task only makes outbound calls. HTTPS covers Geofabrik, ECR, S3,
# CloudWatch Logs and ECS; DNS is allowed only to the VPC CIDR, where the VPC resolver listens.
resource "aws_security_group" "marked_trails_builder" {
  count = local.marked_trails_count

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
    description = "DNS over UDP to the VPC resolver"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.marked_trails_builder_vpc_cidr]
  }

  egress {
    description = "DNS over TCP to the VPC resolver"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.marked_trails_builder_vpc_cidr]
  }
}

resource "aws_ecs_cluster" "marked_trails_builder" {
  count = local.marked_trails_count

  name = local.marked_trails_builder_name
}

data "aws_iam_policy_document" "marked_trails_builder_assume" {
  count = local.marked_trails_count

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# The execution role only pulls the image and writes the task's logs; the build itself uses the
# task role below.
resource "aws_iam_role" "marked_trails_builder_execution" {
  count = local.marked_trails_count

  name               = "${local.marked_trails_builder_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.marked_trails_builder_assume[0].json
}

data "aws_iam_policy_document" "marked_trails_builder_execution" {
  count = local.marked_trails_count

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
  count = local.marked_trails_count

  name   = "${local.marked_trails_builder_name}-execution"
  role   = aws_iam_role.marked_trails_builder_execution[0].id
  policy = data.aws_iam_policy_document.marked_trails_builder_execution[0].json
}

resource "aws_iam_role" "marked_trails_builder_task" {
  count = local.marked_trails_count

  name               = "${local.marked_trails_builder_name}-task"
  assume_role_policy = data.aws_iam_policy_document.marked_trails_builder_assume[0].json
}

# The same policy as the Airflow publisher user: it can write and delete only under the trail
# prefix. The CLI reads its credentials from the task role when AWS_ACCESS_KEY_ID is not set.
resource "aws_iam_role_policy_attachment" "marked_trails_builder_task" {
  count = local.marked_trails_count

  role       = aws_iam_role.marked_trails_builder_task[0].name
  policy_arn = aws_iam_policy.marked_trails_publisher[0].arn
}

resource "aws_ecs_task_definition" "marked_trails_builder" {
  count = local.marked_trails_count

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

  # Europe is built country by country: the largest extract is France at 5.1 GB, and the peak
  # with the filtered files and the PMTiles output is about 10 GB. 50 GiB leaves room for the
  # Planetiler temporary files; the storage above 20 GiB is billed per GB-hour.
  ephemeral_storage {
    size_in_gib = var.marked_trails_builder_ephemeral_gib
  }

  # The container name must match CONTAINER_NAME in airflow/src/trip_airflow/marked_trails_fargate.py.
  container_definitions = jsonencode([
    {
      name      = "marked-trails-builder"
      image     = "${aws_ecr_repository.marked_trails_builder[0].repository_url}:${var.marked_trails_builder_image_tag}"
      essential = true
      environment = [
        { name = "MARKED_TRAILS_STORE", value = "s3" },
        { name = "MARKED_TRAILS_S3_BUCKET", value = aws_s3_bucket.marked_trails[0].bucket },
        { name = "MARKED_TRAILS_S3_PREFIX", value = local.marked_trails_prefix },
        { name = "AWS_REGION", value = data.aws_region.current.name },
        { name = "MARKED_TRAILS_TOOL_CPUS", value = tostring(local.marked_trails_builder_tool_cpus) },
        { name = "MARKED_TRAILS_TOOL_MEMORY_BYTES", value = tostring(local.marked_trails_builder_tool_memory_bytes) },
        { name = "MARKED_TRAILS_PLANETILER_HEAP", value = var.marked_trails_builder_planetiler_heap },
        { name = "MARKED_TRAILS_PLANETILER_DIRECT_MEMORY", value = var.marked_trails_builder_planetiler_direct_memory },
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
}

# Lets the Airflow publisher user start and stop the builder and read its logs, and nothing else.
# StopTask is needed so that a killed or timed-out Airflow task can stop its Fargate task.
data "aws_iam_policy_document" "marked_trails_launcher" {
  count = local.marked_trails_count

  statement {
    sid     = "RunBuilderTask"
    effect  = "Allow"
    actions = ["ecs:RunTask"]
    resources = [
      replace(aws_ecs_task_definition.marked_trails_builder[0].arn, "/:[0-9]+$/", ":*"),
      aws_ecs_cluster.marked_trails_builder[0].arn,
    ]
  }

  statement {
    sid    = "ManageBuilderTasks"
    effect = "Allow"
    actions = [
      "ecs:DescribeTasks",
      "ecs:StopTask",
    ]
    resources = [
      "${replace(aws_ecs_cluster.marked_trails_builder[0].arn, "cluster/", "task/")}/*",
    ]
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
  count = local.marked_trails_count

  name        = "${var.name_prefix}-marked-trails-launcher-policy"
  description = "Least-privilege policy for the Airflow Marked Trails Fargate launcher."
  policy      = data.aws_iam_policy_document.marked_trails_launcher[0].json
}
