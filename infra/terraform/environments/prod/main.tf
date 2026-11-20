terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  name_prefix         = "${var.project_name}-${var.environment}"
  objects_bucket_name = "${local.name_prefix}-objects-${data.aws_caller_identity.current.account_id}"
  alb_protocol        = var.alb_acm_certificate_arn != "" ? "https-only" : "http-only"

  common_tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  })
}

module "networking" {
  source = "../../modules/networking"

  name_prefix                = local.name_prefix
  vpc_cidr                   = var.vpc_cidr
  az_count                   = 2
  enable_nat_gateway         = var.enable_nat_gateway
  single_nat_gateway         = var.single_nat_gateway
  enable_s3_gateway_endpoint = true
  restrict_alb_to_cloudfront = var.restrict_alb_to_cloudfront
  alb_ingress_cidrs          = var.alb_ingress_cidrs
  enable_flow_logs           = var.enable_flow_logs
  flow_logs_retention_days   = var.log_retention_days
  tags                       = local.common_tags
}

module "database" {
  source = "../../modules/database"

  name_prefix                 = local.name_prefix
  subnet_ids                  = module.networking.isolated_subnet_ids
  rds_security_group_id       = module.networking.rds_security_group_id
  engine_version              = var.db_engine_version
  db_instance_class           = var.db_instance_class
  allocated_storage           = var.db_allocated_storage
  max_allocated_storage       = var.db_max_allocated_storage
  multi_az                    = var.db_multi_az
  backup_retention_period     = var.db_backup_retention_period
  deletion_protection         = var.db_deletion_protection
  skip_final_snapshot         = var.db_skip_final_snapshot
  kms_deletion_window_days    = var.kms_deletion_window_days
  secret_recovery_window_days = var.secret_recovery_window_days
  tags                        = local.common_tags
}

module "compute" {
  source = "../../modules/compute"

  name_prefix                  = local.name_prefix
  vpc_id                       = module.networking.vpc_id
  public_subnet_ids            = module.networking.public_subnet_ids
  private_subnet_ids           = module.networking.private_subnet_ids
  alb_security_group_id        = module.networking.alb_security_group_id
  api_security_group_id        = module.networking.api_security_group_id
  worker_security_group_id     = module.networking.worker_security_group_id
  objects_bucket_name          = local.objects_bucket_name
  db_secret_arn                = module.database.db_secret_arn
  rds_kms_key_arn              = module.database.rds_kms_key_arn
  domain_name                  = var.domain_name
  alb_acm_certificate_arn      = var.alb_acm_certificate_arn
  enable_origin_verify         = var.enable_origin_verify
  enable_nat_gateway           = var.enable_nat_gateway
  image_tag                    = var.image_tag
  ecr_image_tag_mutability     = var.ecr_image_tag_mutability
  cpu_architecture             = var.cpu_architecture
  api_cpu                      = var.api_cpu
  api_memory                   = var.api_memory
  api_min_capacity             = var.api_min_capacity
  api_max_capacity             = var.api_max_capacity
  worker_cpu                   = var.worker_cpu
  worker_memory                = var.worker_memory
  worker_ephemeral_storage_gib = var.worker_ephemeral_storage_gib
  worker_concurrency           = var.worker_concurrency
  worker_min_capacity          = var.worker_min_capacity
  worker_max_capacity          = var.worker_max_capacity
  render_retention_days        = var.render_retention_days
  map_tile_allowlist           = var.map_tile_allowlist
  log_level                    = var.log_level
  log_retention_days           = var.log_retention_days
  enable_container_insights    = var.enable_container_insights
  reap_schedule_expression     = var.reap_schedule_expression
  enable_dbt_schedule          = var.enable_dbt_schedule
  dbt_schedule_expression      = var.dbt_schedule_expression
  kms_deletion_window_days     = var.kms_deletion_window_days
  secret_recovery_window_days  = var.secret_recovery_window_days
  tags                         = local.common_tags
}

module "storage" {
  source = "../../modules/storage"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix                = local.name_prefix
  objects_bucket_name        = local.objects_bucket_name
  alb_dns_name               = module.compute.alb_dns_name
  alb_origin_protocol_policy = local.alb_protocol
  origin_verify_header_value = module.compute.origin_verify_header_value
  render_retention_days      = var.render_retention_days
  enable_lakehouse_tiering   = var.enable_lakehouse_tiering
  force_destroy_buckets      = var.force_destroy_buckets
  enable_waf                 = var.enable_waf
  cloudfront_price_class     = var.cloudfront_price_class
  domain_name                = var.domain_name
  aliases                    = var.domain_name != "" ? [var.domain_name] : []
  acm_certificate_arn        = var.cloudfront_acm_certificate_arn
  map_tile_csp_origins       = var.map_tile_csp_origins
  kms_deletion_window_days   = var.kms_deletion_window_days
  tags                       = local.common_tags
}
