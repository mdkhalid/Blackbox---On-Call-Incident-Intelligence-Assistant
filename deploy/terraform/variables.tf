variable "project_name" {
  description = "Prefix for every resource name (`<project>-<environment>`)."
  type        = string
  default     = "blackbox"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "Use 2-21 lowercase alphanumeric characters or hyphens."
  }
}

variable "environment" {
  description = "Deployment environment; part of every resource name."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "aws_region" {
  description = "AWS region for the whole stack."
  type        = string
  default     = "eu-west-1"
}

variable "tags" {
  description = "Extra tags merged over the defaults."
  type        = map(string)
  default     = {}
}

# ---- network ------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR for the VPC. /16 gives /20 private and /24 public subnets."
  type        = string
  default     = "10.60.0.0/16"
}

variable "availability_zone_count" {
  description = "Number of AZs to spread subnets and node groups across."
  type        = number
  default     = 3

  validation {
    condition     = var.availability_zone_count >= 2 && var.availability_zone_count <= 4
    error_message = "Use 2-4 AZs; one AZ cannot satisfy the availability target."
  }
}

variable "single_nat_gateway" {
  description = "Use one NAT gateway instead of one per AZ. Cheaper, but an AZ outage breaks egress for every subnet."
  type        = bool
  default     = false
}

variable "enable_interface_vpc_endpoints" {
  description = "Create interface endpoints (ECR, STS, KMS, Secrets Manager, CloudWatch Logs). Costs ~5 × $7/month per AZ; off by default because NAT already exists."
  type        = bool
  default     = false
}

variable "flow_log_retention_days" {
  description = "Retention for VPC flow logs."
  type        = number
  default     = 90
}

# ---- EKS ----------------------------------------------------------------------

variable "kubernetes_version" {
  description = "EKS control-plane version. Check `aws eks describe-cluster-versions` before applying; EKS standard support is 14 months."
  type        = string
  default     = "1.35"
}

variable "cluster_endpoint_public_access" {
  description = "Expose the Kubernetes API endpoint publicly. Leave false and reach it through a VPN/bastion."
  type        = bool
  default     = false
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "CIDRs allowed to reach the public endpoint. Ignored when public access is disabled."
  type        = list(string)
  default     = []
}

variable "cluster_admin_principal_arns" {
  description = "IAM principals (users/roles) granted EKS cluster-admin through an access entry, e.g. [\"arn:aws:iam::123456789012:role/PlatformAdmin\"]."
  type        = list(string)
  default     = []
}

variable "system_instance_types" {
  description = "Instance types for the system/API node group."
  type        = list(string)
  default     = ["m6i.large"]
}

variable "system_node_min" {
  description = "Minimum system nodes (one per AZ is recommended)."
  type        = number
  default     = 3
}

variable "system_node_max" {
  description = "Maximum system nodes."
  type        = number
  default     = 9
}

variable "system_node_desired" {
  description = "Desired system nodes. The chart's HPA scales pods, not this group."
  type        = number
  default     = 3
}

variable "batch_instance_types" {
  description = "Instance types for the tainted batch/worker node group."
  type        = list(string)
  default     = ["m6i.xlarge"]
}

variable "batch_node_min" {
  description = "Minimum batch nodes. KEDA adds worker pods; the node group adds capacity."
  type        = number
  default     = 1
}

variable "batch_node_max" {
  description = "Maximum batch nodes."
  type        = number
  default     = 12
}

variable "batch_node_desired" {
  description = "Desired batch nodes."
  type        = number
  default     = 1
}

variable "node_disk_size_gb" {
  description = "Root volume size per node. Image layers and container logs share it."
  type        = number
  default     = 100
}

# ---- Aurora PostgreSQL --------------------------------------------------------

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL engine version."
  type        = string
  default     = "16.4"
}

variable "aurora_instance_class" {
  description = "Aurora instance class."
  type        = string
  default     = "db.r6g.large"
}

variable "aurora_instance_count" {
  description = "Aurora instances: 1 writer + N-1 readers."
  type        = number
  default     = 2

  validation {
    condition     = var.aurora_instance_count >= 2
    error_message = "At least two instances are required for a highly available writer."
  }
}

variable "database_name" {
  description = "Initial database name."
  type        = string
  default     = "blackbox"
}

variable "database_master_username" {
  description = "Aurora master username. The password is generated and stored by RDS in Secrets Manager."
  type        = string
  default     = "blackbox_admin"
}

variable "database_backup_retention_days" {
  description = "Automated backup retention (point-in-time recovery window)."
  type        = number
  default     = 14
}

variable "database_deletion_protection" {
  description = "Block accidental cluster deletion. Keep true in prod."
  type        = bool
  default     = true
}

# ---- ElastiCache Redis --------------------------------------------------------

variable "redis_engine_version" {
  description = "Redis engine version."
  type        = string
  default     = "7.1"
}

variable "redis_node_type" {
  description = "ElastiCache node type. Queue and rate-limit data must fit in memory."
  type        = string
  default     = "cache.r7g.large"
}

variable "redis_replica_count" {
  description = "Read replicas per shard (0 = single node, no failover)."
  type        = number
  default     = 1

  validation {
    condition     = var.redis_replica_count >= 1
    error_message = "At least one replica is required for automatic failover and multi-AZ."
  }
}

variable "redis_snapshot_retention_days" {
  description = "Daily Redis snapshot retention."
  type        = number
  default     = 5
}

# ---- trace archive ------------------------------------------------------------

variable "trace_bucket_prefix" {
  description = "Prefix for the globally unique trace-archive bucket name."
  type        = string
  default     = "blackbox-traces"
}

variable "trace_archive_prefix" {
  description = "Key prefix the app writes under (TRACE_ARCHIVE_PREFIX). The IRSA policy is scoped to it."
  type        = string
  default     = "traces"
}

variable "trace_object_expiration_days" {
  description = "Delete archived traces after this many days (compliance window)."
  type        = number
  default     = 2555
}

variable "trace_ia_transition_days" {
  description = "Move trace objects to Standard-IA after this many days."
  type        = number
  default     = 30
}

variable "trace_glacier_transition_days" {
  description = "Move trace objects to Glacier Instant Retrieval after this many days."
  type        = number
  default     = 90
}

variable "enable_bucket_access_logging" {
  description = "Write S3 server access logs to a separate bucket."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch retention for control-plane, flow, RDS and Redis logs."
  type        = number
  default     = 90
}

# ---- Kubernetes names and IRSA ------------------------------------------------

variable "namespace" {
  description = "Namespace the Helm release uses. The IRSA trust policy is scoped to it."
  type        = string
  default     = "blackbox"
}

variable "service_account_name" {
  description = "ServiceAccount name created by the chart (defaults to the release name)."
  type        = string
  default     = "blackbox"
}

variable "enable_external_secrets" {
  description = "Create the IRSA role External Secrets uses to read this stack's secrets."
  type        = bool
  default     = true
}

variable "external_secrets_namespace" {
  description = "Namespace of the External Secrets Operator."
  type        = string
  default     = "external-secrets"
}

variable "external_secrets_service_account" {
  description = "ServiceAccount the External Secrets Operator controller uses."
  type        = string
  default     = "external-secrets"
}

# ---- application secrets ------------------------------------------------------

variable "openai_api_key" {
  description = "Optional provider key written to the app secret. Prefer injecting it outside Terraform; an empty string leaves the key out."
  type        = string
  default     = ""
  sensitive   = true
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window. 0 forces immediate deletion (dev only)."
  type        = number
  default     = 7

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "Use 0 or 7-30 days; Secrets Manager rejects other values."
  }
}
# ---- backend shapes (Phase 2 benchmark decides the final shape) ---------------

variable "enable_opensearch" {
  description = "Create a managed OpenSearch domain for the sparse retrieval leg. Leave false until the Phase 2 backend benchmark picks OpenSearch-only vs Qdrant+OpenSearch."
  type        = bool
  default     = false
}

variable "opensearch_instance_type" {
  description = "Instance type for the optional OpenSearch domain."
  type        = string
  default     = "t3.small.search"
}

variable "enable_waf" {
  description = "Create a regional WAFv2 web ACL for the ALB. Associate it with the ingress ALB ARN via aws_wafv2_web_acl_association in your overlay."
  type        = bool
  default     = false
}

variable "enable_route53" {
  description = "Create a Route 53 hosted zone for var.domain_name. Records are managed in the overlay."
  type        = bool
  default     = false
}

variable "domain_name" {
  description = "Public domain name for the optional Route 53 zone."
  type        = string
  default     = ""
}
