# IRSA role for the External Secrets Operator. The app role above can write
# traces but cannot read the secrets; this role can read the secrets but has no
# S3 access. Splitting them keeps the blast radius of a compromised pod smaller
# than handing every capability to one role.

data "aws_iam_policy_document" "eso_assume" {
  count = var.enable_external_secrets ? 1 : 0

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
      values   = ["system:serviceaccount:${var.external_secrets_namespace}:${var.external_secrets_service_account}"]
    }
  }
}

resource "aws_iam_role" "eso" {
  count = var.enable_external_secrets ? 1 : 0

  name               = "${local.name}-external-secrets"
  description        = "External Secrets Operator: read stack secrets only"
  assume_role_policy = data.aws_iam_policy_document.eso_assume[0].json

  tags = local.tags
}

data "aws_iam_policy_document" "eso" {
  count = var.enable_external_secrets ? 1 : 0

  statement {
    sid    = "ReadStackSecrets"
    effect = "Allow"

    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
    ]

    resources = [
      aws_secretsmanager_secret.app.arn,
      aws_secretsmanager_secret.redis.arn,
      aws_rds_cluster.main.master_user_secret[0].secret_arn,
    ]
  }

  statement {
    sid       = "DecryptStackSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "eso" {
  count = var.enable_external_secrets ? 1 : 0

  name   = "read-secrets"
  role   = aws_iam_role.eso[0].id
  policy = data.aws_iam_policy_document.eso[0].json
}
