data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs                = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)
  private_subnet_ids = aws_subnet.private[*].id
  public_subnet_ids  = aws_subnet.public[*].id
  cluster_name       = "${local.name}-eks"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = local.name }
}

# Private subnets host the nodes, Aurora and Redis. /20 each: a node group that
# scales to dozens of pods consumes IPs quickly, and pods are not the only
# consumer.
resource "aws_subnet" "private" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name                                          = "${local.name}-private-${local.azs[count.index]}"
    "kubernetes.io/role/internal-elb"             = "1"
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
  }
}

# Public subnets exist only for load balancers; no workloads are scheduled here.
resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index + 128)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name                                          = "${local.name}-public-${local.azs[count.index]}"
    "kubernetes.io/role/elb"                      = "1"
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.name }
}

resource "aws_eip" "nat" {
  count = var.single_nat_gateway ? 1 : length(local.azs)

  domain = "vpc"

  tags = { Name = "${local.name}-nat-${count.index}" }
}

resource "aws_nat_gateway" "main" {
  count = var.single_nat_gateway ? 1 : length(local.azs)

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = { Name = "${local.name}-nat-${count.index}" }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = length(local.azs)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One route table per AZ so an AZ failure isolates egress instead of taking the
# whole VPC with it. With a single NAT every table points at the same gateway.
resource "aws_route_table" "private" {
  count = length(local.azs)

  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[var.single_nat_gateway ? 0 : count.index].id
  }

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table_association" "private" {
  count = length(local.azs)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# ---- flow logs ----------------------------------------------------------------

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/aws/vpc/${local.name}-flow-logs"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = aws_kms_key.logs.arn

  tags = local.tags
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${local.name}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json

  tags = local.tags
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.flow.arn,
      "${aws_cloudwatch_log_group.flow.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

resource "aws_flow_log" "main" {
  vpc_id                   = aws_vpc.main.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow.arn
  iam_role_arn             = aws_iam_role.flow_logs.arn
  max_aggregation_interval = 60

  tags = local.tags

  depends_on = [aws_iam_role_policy.flow_logs]
}

# ---- security groups ----------------------------------------------------------

resource "aws_security_group" "cluster" {
  name        = "${local.name}-eks-cluster"
  description = "EKS control plane"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-eks-cluster" }
}

resource "aws_security_group" "nodes" {
  name        = "${local.name}-eks-nodes"
  description = "EKS nodes and pods"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-eks-nodes" }
}

# The control plane must reach kubelet and the pods must reach the API server.
resource "aws_security_group_rule" "nodes_from_cluster" {
  type                     = "ingress"
  security_group_id        = aws_security_group.nodes.id
  source_security_group_id = aws_security_group.cluster.id
  protocol                 = "-1"
  from_port                = 0
  to_port                  = 0
  description              = "Control plane to kubelet and webhooks"
}

resource "aws_security_group_rule" "nodes_internal" {
  type              = "ingress"
  security_group_id = aws_security_group.nodes.id
  self              = true
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  description       = "Pod and node-to-node traffic"
}

resource "aws_security_group_rule" "cluster_from_nodes" {
  type                     = "ingress"
  security_group_id        = aws_security_group.cluster.id
  source_security_group_id = aws_security_group.nodes.id
  protocol                 = "tcp"
  from_port                = 443
  to_port                  = 443
  description              = "Nodes to the Kubernetes API server"
}

resource "aws_security_group_rule" "cluster_egress" {
  type              = "egress"
  security_group_id = aws_security_group.cluster.id
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  cidr_blocks       = [var.vpc_cidr]
  description       = "Control plane to nodes, webhooks and the VPC endpoints"
}

resource "aws_security_group_rule" "cluster_egress_https" {
  type              = "egress"
  security_group_id = aws_security_group.cluster.id
  protocol          = "tcp"
  from_port         = 443
  to_port           = 443
  #trivy:ignore:AWS-0104 EKS control-plane ENIs call AWS APIs whose addresses are not static.
  cidr_blocks = ["0.0.0.0/0"]
  description = "Control plane to AWS APIs (EKS, ECR, STS) outside the VPC"
}

# Nodes and pods need the internet: image pulls, the LLM providers, OpenSearch.
# Restricted to HTTPS in the general case; the VPC CIDR is open because Aurora,
# Redis and pod-to-pod traffic use their own ports.
resource "aws_security_group_rule" "nodes_egress" {
  type              = "egress"
  security_group_id = aws_security_group.nodes.id
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  cidr_blocks       = [var.vpc_cidr]
  description       = "Aurora, Redis and in-VPC endpoints"
}

resource "aws_security_group_rule" "nodes_egress_https" {
  type              = "egress"
  security_group_id = aws_security_group.nodes.id
  protocol          = "tcp"
  from_port         = 443
  to_port           = 443
  #trivy:ignore:AWS-0104 Image pulls, AWS APIs and LLM providers have no static CIDR.
  cidr_blocks = ["0.0.0.0/0"]
  description = "ECR, S3, the AWS APIs and the LLM providers"
}

# ---- optional interface endpoints ---------------------------------------------

resource "aws_security_group" "endpoints" {
  count = var.enable_interface_vpc_endpoints ? 1 : 0

  name        = "${local.name}-vpc-endpoints"
  description = "Interface endpoints from inside the VPC"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-vpc-endpoints" }
}

resource "aws_security_group_rule" "endpoints_ingress" {
  count = var.enable_interface_vpc_endpoints ? 1 : 0

  type              = "ingress"
  security_group_id = aws_security_group.endpoints[0].id
  protocol          = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_blocks       = [var.vpc_cidr]
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = { Name = "${local.name}-s3" }
}

locals {
  interface_endpoints = var.enable_interface_vpc_endpoints ? toset([
    "ecr.api",
    "ecr.dkr",
    "sts",
    "kms",
    "secretsmanager",
    "logs",
  ]) : toset([])
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.private_subnet_ids
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${replace(each.key, ".", "-")}" }
}
