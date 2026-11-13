# Optional managed OpenSearch domain for the sparse retrieval leg. Disabled by
# default: the Phase 2 backend benchmark decides whether the sparse leg runs on
# OpenSearch alone or alongside Qdrant, and the instance size follows from that.
# Enable with `enable_opensearch = true` once the shape is chosen.

resource "aws_security_group" "search" {
  count = var.enable_opensearch ? 1 : 0

  name        = "${local.name}-opensearch"
  description = "OpenSearch domain for the sparse retrieval leg"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-opensearch" }
}

resource "aws_security_group_rule" "search_ingress" {
  count = var.enable_opensearch ? 1 : 0

  type                     = "ingress"
  security_group_id        = aws_security_group.search[0].id
  source_security_group_id = aws_security_group.nodes.id
  protocol                 = "tcp"
  from_port                = 443
  to_port                  = 443
  description              = "EKS nodes to OpenSearch HTTPS"
}

resource "aws_opensearch_domain" "search" {
  count = var.enable_opensearch ? 1 : 0

  domain_name    = "${local.name}-chunks"
  engine_version = "OpenSearch_2.13"

  cluster_config {
    instance_type  = var.opensearch_instance_type
    instance_count = 3

    zone_awareness_config {
      availability_zone_count = 3
    }
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = 100
  }

  vpc_options {
    subnet_ids         = slice(local.private_subnet_ids, 0, 3)
    security_group_ids = [aws_security_group.search[0].id]
  }

  encrypt_at_rest {
    enabled    = true
    kms_key_id = aws_kms_key.data.arn
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  advanced_security_options {
    enabled                        = true
    internal_user_database_enabled = true
    master_user_options {
      master_user_name     = "blackbox"
      master_user_password = random_password.opensearch[0].result
    }
  }

  tags = local.tags

  depends_on = [aws_iam_service_linked_role.opensearch]
}

resource "aws_iam_service_linked_role" "opensearch" {
  count = var.enable_opensearch ? 1 : 0

  aws_service_name = "opensearchservice.amazonaws.com"
}

resource "random_password" "opensearch" {
  count = var.enable_opensearch ? 1 : 0

  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}
