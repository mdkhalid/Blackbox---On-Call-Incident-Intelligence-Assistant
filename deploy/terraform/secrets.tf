# ---- KMS ----------------------------------------------------------------------

# One CMK for Aurora storage, the trace bucket and Secrets Manager. The key
# policy carries the documented service statements; a CMK without them fails at
# the first RDS/S3 call, not at apply time.
data "aws_iam_policy_document" "data_key" {
  statement {
    sid    = "AccountAdmin"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AuroraUseOfKey"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
      "kms:CreateGrant",
      "kms:DescribeKey",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["rds.${var.aws_region}.amazonaws.com"]
    }
  }

  statement {
    sid    = "S3UseOfKey"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "data" {
  description             = "${local.name} data at rest (Aurora, traces, secrets)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.data_key.json
  # A replica key in the DR region requires the primary to be multi-region. This is
  # set at creation time only: enabling multi-region on an existing key replaces it,
  # so plan the change in a maintenance window.
  multi_region = var.enable_multi_region

  tags = local.tags
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

data "aws_iam_policy_document" "logs_key" {
  statement {
    sid    = "AccountAdmin"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "CloudWatchLogsUseOfKey"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["logs.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "${local.name} CloudWatch Logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.logs_key.json

  tags = local.tags
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${local.name}-logs"
  target_key_id = aws_kms_key.logs.key_id
}

# ---- application secrets ------------------------------------------------------
#
# The chart consumes a single Secret via `existingSecret`. External Secrets (see
# deploy/external-secrets) assembles that Secret from these entries plus the
# RDS-managed master credential. Terraform never needs to read the database
# password, and rotating it in Secrets Manager does not require a Terraform run.

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "random_password" "redis" {
  length  = 40
  special = false
}

locals {
  app_secret_values = merge(
    { AUTH_JWT_SECRET = random_password.jwt.result },
    var.openai_api_key != "" ? { OPENAI_API_KEY = var.openai_api_key } : {},
  )
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/app"
  description             = "Blackbox application secrets (JWT signing key, provider key)"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = var.secret_recovery_window_days

  # Cross-region replica in the DR region, encrypted with the replica CMK. Dynamic
  # so the single-region plan is unchanged.
  dynamic "replica" {
    for_each = var.enable_multi_region ? [1] : []

    content {
      region     = var.secondary_region
      kms_key_id = aws_kms_replica_key.data[0].arn
    }
  }

  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id     = aws_secretsmanager_secret.app.id
  secret_string = jsonencode(local.app_secret_values)
}

resource "aws_secretsmanager_secret" "redis" {
  name                    = "${local.name}/redis"
  description             = "Blackbox Redis connection URL including the auth token"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = var.secret_recovery_window_days

  dynamic "replica" {
    for_each = var.enable_multi_region ? [1] : []

    content {
      region     = var.secondary_region
      kms_key_id = aws_kms_replica_key.data[0].arn
    }
  }

  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "redis" {
  secret_id = aws_secretsmanager_secret.redis.id

  secret_string = jsonencode({
    # Transit encryption is on, so the scheme must be `rediss://`. The username
    # is empty because AUTH uses the default user with a token.
    REDIS_URL = "rediss://:${urlencode(random_password.redis.result)}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
    # KEDA's Redis scaler wants an address and a password, not a URL; the
    # TriggerAuthentication in the chart reads this key.
    REDIS_PASSWORD = random_password.redis.result
  })
}
