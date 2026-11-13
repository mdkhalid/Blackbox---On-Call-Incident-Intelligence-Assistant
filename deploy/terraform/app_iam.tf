# IRSA role for the API/worker ServiceAccount. The chart annotates the account
# with this role ARN and the EKS webhook projects a token for it, so the pods
# need no static AWS keys.
#
# The External Secrets Operator needs a *different* role (see
# deploy/external-secrets/README.md): it reads the secret values that this role
# must not be able to read at runtime.

data "aws_iam_policy_document" "app_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:sub"
      values   = ["system:serviceaccount:${var.namespace}:${var.service_account_name}"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${local.name}-app"
  description        = "Blackbox pods: trace archive writes only"
  assume_role_policy = data.aws_iam_policy_document.app_assume.json

  tags = local.tags
}

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "ListTraceArchive"
    effect = "Allow"

    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.traces.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        var.trace_archive_prefix,
        "${var.trace_archive_prefix}/*",
      ]
    }
  }

  statement {
    sid    = "ReadWriteTraceArchive"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]

    resources = ["${aws_s3_bucket.traces.arn}/${var.trace_archive_prefix}/*"]
  }

  statement {
    sid    = "TraceArchiveKey"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "trace-archive"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}
