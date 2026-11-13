resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# ---- access-log bucket ---------------------------------------------------------
#
# Server access logging cannot write to a KMS-encrypted bucket without granting
# the log-delivery principal a key grant, so this bucket uses SSE-S3 and a
# bucket policy that admits `logging.s3.amazonaws.com` from this account only.

resource "aws_s3_bucket" "logs" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket        = "${var.trace_bucket_prefix}-access-logs-${local.account_id}-${random_id.bucket_suffix.hex}"
  force_destroy = false

  tags = merge(local.tags, { Purpose = "s3-access-logs" })
}

resource "aws_s3_bucket_public_access_block" "logs" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "logs" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }
}

data "aws_iam_policy_document" "logs_bucket" {
  count = var.enable_bucket_access_logging ? 1 : 0

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.logs[0].arn,
      "${aws_s3_bucket.logs[0].arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "AllowS3ServerAccessLogging"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs[0].arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:s3:::${var.trace_bucket_prefix}-traces-${local.account_id}-${random_id.bucket_suffix.hex}"]
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id
  policy = data.aws_iam_policy_document.logs_bucket[0].json
}

# ---- trace archive -------------------------------------------------------------
#
# The API archives turns here before retention deletes them from Postgres
# (`TRACE_ARCHIVE_BACKEND=s3`). Versioning protects against an object being
# overwritten by a retried archive write; lifecycle moves cold traces to cheaper
# storage without making them unreadable.

resource "aws_s3_bucket" "traces" {
  bucket        = "${var.trace_bucket_prefix}-traces-${local.account_id}-${random_id.bucket_suffix.hex}"
  force_destroy = false

  tags = merge(local.tags, { Purpose = "trace-archive" })
}

resource "aws_s3_bucket_public_access_block" "traces" {
  bucket = aws_s3_bucket.traces.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "traces" {
  bucket = aws_s3_bucket.traces.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "traces" {
  bucket = aws_s3_bucket.traces.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "traces" {
  bucket = aws_s3_bucket.traces.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }

    # Cuts KMS request volume (and cost) for many small trace objects.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "traces" {
  bucket = aws_s3_bucket.traces.id

  lifecycle {
    precondition {
      condition     = var.trace_ia_transition_days < var.trace_glacier_transition_days && var.trace_glacier_transition_days < var.trace_object_expiration_days
      error_message = "Lifecycle days must increase: Standard-IA < Glacier IR < expiration."
    }
  }

  rule {
    id     = "tier-and-expire-traces"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    transition {
      days          = var.trace_ia_transition_days
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = var.trace_glacier_transition_days
      storage_class = "GLACIER_IR"
    }

    expiration {
      days = var.trace_object_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "traces_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.traces.arn,
      "${aws_s3_bucket.traces.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "traces" {
  bucket = aws_s3_bucket.traces.id
  policy = data.aws_iam_policy_document.traces_bucket.json
}

resource "aws_s3_bucket_logging" "traces" {
  count = var.enable_bucket_access_logging ? 1 : 0

  bucket = aws_s3_bucket.traces.id

  target_bucket = aws_s3_bucket.logs[0].id
  target_prefix = "traces/"
}
