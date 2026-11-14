# ---- multi-region / DR layer (reference) ---------------------------------------
#
# This file creates the cross-region primitives that hang off the PRIMARY stack.
# The secondary region's VPC/EKS/Aurora/Redis are a second instantiation of this
# module with `enable_multi_region=false`, wired to the global ids emitted here:
#
#   aurora_global_cluster_id = <output global_cluster_id>
#   redis global group       = <output redis_global_replication_group_id>
#
# Everything is gated on `enable_multi_region`, so the single-region plan is
# unchanged when the flag is off.

resource "aws_kms_replica_key" "data" {
  count    = var.enable_multi_region ? 1 : 0
  provider = aws.secondary

  description             = "Replica of ${local.name} data CMK"
  primary_key_arn         = aws_kms_key.data.arn
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.data_key.json

  tags = local.tags
}

# Secret replicas are declared as `replica` blocks on the primary secrets in
# `secrets.tf`; the AWS provider has no standalone replica resource.

# An Aurora Global Database built from the existing primary. The secondary cluster
# is created by the other region's module instance.
resource "aws_rds_global_cluster" "main" {
  count = var.enable_multi_region ? 1 : 0

  global_cluster_identifier    = coalesce(var.aurora_global_cluster_id, "${local.name}-global")
  engine                       = "aurora-postgresql"
  engine_version               = var.aurora_engine_version
  database_name                = var.database_name
  source_db_cluster_identifier = aws_rds_cluster.main.arn
  deletion_protection          = var.database_deletion_protection
  storage_encrypted            = true
  force_destroy                = false
}

resource "aws_elasticache_global_replication_group" "main" {
  count = var.enable_multi_region ? 1 : 0

  global_replication_group_id_suffix = local.name
  primary_replication_group_id       = aws_elasticache_replication_group.main.id
}

# ---- trace archive cross-region replication ------------------------------------

resource "aws_s3_bucket" "traces_secondary" {
  count    = var.enable_multi_region && var.enable_s3_replication ? 1 : 0
  provider = aws.secondary

  bucket        = "${var.trace_bucket_prefix}-traces-${local.account_id}-${random_id.bucket_suffix.hex}"
  force_destroy = false

  tags = merge(local.tags, { Purpose = "trace-archive-dr" })
}

resource "aws_s3_bucket_public_access_block" "traces_secondary" {
  count    = var.enable_multi_region && var.enable_s3_replication ? 1 : 0
  provider = aws.secondary

  bucket = aws_s3_bucket.traces_secondary[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "traces_secondary" {
  count    = var.enable_multi_region && var.enable_s3_replication ? 1 : 0
  provider = aws.secondary

  bucket = aws_s3_bucket.traces_secondary[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "traces_secondary" {
  count    = var.enable_multi_region && var.enable_s3_replication ? 1 : 0
  provider = aws.secondary

  bucket = aws_s3_bucket.traces_secondary[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_replica_key.data[0].arn
    }

    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "s3_replication_assume" {
  count = var.enable_multi_region && var.enable_s3_replication ? 1 : 0

  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "s3_replication" {
  count = var.enable_multi_region && var.enable_s3_replication ? 1 : 0

  name               = "${local.name}-s3-replication"
  assume_role_policy = data.aws_iam_policy_document.s3_replication_assume[0].json

  tags = local.tags
}

data "aws_iam_policy_document" "s3_replication" {
  count = var.enable_multi_region && var.enable_s3_replication ? 1 : 0

  statement {
    effect = "Allow"

    actions = [
      "s3:GetReplicationConfiguration",
      "s3:ListBucket",
    ]

    resources = [aws_s3_bucket.traces.arn]
  }

  statement {
    effect = "Allow"

    actions = [
      "s3:GetObjectVersionForReplication",
      "s3:GetObjectVersionAcl",
      "s3:GetObjectVersionTagging",
      "s3:GetObjectVersion",
    ]

    resources = ["${aws_s3_bucket.traces.arn}/*"]
  }

  statement {
    effect = "Allow"

    actions = [
      "s3:ReplicateObject",
      "s3:ReplicateDelete",
      "s3:ReplicateTags",
    ]

    resources = ["${aws_s3_bucket.traces_secondary[0].arn}/*"]
  }

  statement {
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = [
      aws_kms_key.data.arn,
      aws_kms_replica_key.data[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "s3_replication" {
  count = var.enable_multi_region && var.enable_s3_replication ? 1 : 0

  name   = "${local.name}-s3-replication"
  role   = aws_iam_role.s3_replication[0].id
  policy = data.aws_iam_policy_document.s3_replication[0].json
}

resource "aws_s3_bucket_replication_configuration" "traces" {
  count = var.enable_multi_region && var.enable_s3_replication ? 1 : 0

  # Replication requires versioning on both buckets; the explicit dependency keeps
  # the first apply from racing the versioning resources.
  depends_on = [
    aws_s3_bucket_versioning.traces,
    aws_s3_bucket_versioning.traces_secondary,
  ]

  bucket = aws_s3_bucket.traces.id
  role   = aws_iam_role.s3_replication[0].arn

  rule {
    id     = "traces-to-dr"
    status = "Enabled"

    filter {}

    delete_marker_replication {
      status = "Enabled"
    }

    destination {
      bucket        = aws_s3_bucket.traces_secondary[0].arn
      storage_class = "STANDARD"

      encryption_configuration {
        replica_kms_key_id = aws_kms_replica_key.data[0].arn
      }
    }
  }
}
