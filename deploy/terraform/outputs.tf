output "region" {
  description = "Region the stack was created in."
  value       = var.aws_region
}

output "vpc_id" {
  description = "VPC holding the cluster and the data plane."
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "Private subnets (nodes, Aurora, Redis)."
  value       = local.private_subnet_ids
}

output "public_subnet_ids" {
  description = "Public subnets (load balancers only)."
  value       = local.public_subnet_ids
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  description = "EKS API endpoint."
  value       = aws_eks_cluster.main.endpoint
}

output "cluster_certificate_authority" {
  description = "Base64 cluster CA, for a kubeconfig you build yourself."
  value       = aws_eks_cluster.main.certificate_authority[0].data
}

output "configure_kubectl" {
  description = "Command that writes a kubeconfig for the new cluster."
  value       = "aws eks update-kubeconfig --name ${aws_eks_cluster.main.name} --region ${var.aws_region}"
}

output "app_irsa_role_arn" {
  description = "Annotate the chart ServiceAccount with `eks.amazonaws.com/role-arn`."
  value       = aws_iam_role.app.arn
}

output "external_secrets_role_arn" {
  description = "Annotate the External Secrets controller ServiceAccount with this role (null when disabled)."
  value       = var.enable_external_secrets ? aws_iam_role.eso[0].arn : null
}

output "database_writer_endpoint" {
  description = "Aurora writer endpoint."
  value       = aws_rds_cluster.main.endpoint
}

output "database_reader_endpoint" {
  description = "Aurora reader endpoint (use it for read-only evaluation queries)."
  value       = aws_rds_cluster.main.reader_endpoint
}

output "database_port" {
  description = "Aurora port."
  value       = aws_rds_cluster.main.port
}

output "database_name" {
  description = "Initial database name."
  value       = aws_rds_cluster.main.database_name
}

output "database_master_secret_arn" {
  description = "RDS-managed master credential secret. External Secrets composes DATABASE_URL from it."
  value       = aws_rds_cluster.main.master_user_secret[0].secret_arn
}

output "redis_primary_endpoint" {
  description = "Redis primary endpoint (TLS-only; use rediss://)."
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "redis_reader_endpoint" {
  description = "Redis reader endpoint."
  value       = aws_elasticache_replication_group.main.reader_endpoint_address
}

output "trace_archive_bucket" {
  description = "S3 bucket for TRACE_ARCHIVE_BUCKET."
  value       = aws_s3_bucket.traces.bucket
}

output "trace_archive_prefix" {
  description = "S3 prefix for TRACE_ARCHIVE_PREFIX."
  value       = var.trace_archive_prefix
}

output "app_secret_arn" {
  description = "Secret holding AUTH_JWT_SECRET and the optional provider key."
  value       = aws_secretsmanager_secret.app.arn
}

output "redis_secret_arn" {
  description = "Secret holding REDIS_URL with the auth token."
  value       = aws_secretsmanager_secret.redis.arn
}

output "kms_data_key_arn" {
  description = "CMK for Aurora, traces and Secrets Manager."
  value       = aws_kms_key.data.arn
}

output "helm_values_hint" {
  description = "Non-secret chart values that wire the app to this stack. Secrets and the IRSA/node-selector wiring are documented in the module README."
  value = {
    "config.TRACE_ARCHIVE_BACKEND" = "s3"
    "config.TRACE_ARCHIVE_BUCKET"  = aws_s3_bucket.traces.bucket
    "config.TRACE_ARCHIVE_REGION"  = var.aws_region
    "config.TRACE_ARCHIVE_PREFIX"  = var.trace_archive_prefix
    "worker.keda.address"          = "${aws_elasticache_replication_group.main.primary_endpoint_address}:6379"
    "worker.keda.tls"              = "true"
  }
}
