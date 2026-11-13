resource "random_id" "final_snapshot" {
  byte_length = 3
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = local.private_subnet_ids

  tags = { Name = "${local.name}-db" }
}

resource "aws_security_group" "database" {
  name        = "${local.name}-aurora"
  description = "Aurora PostgreSQL"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-aurora" }
}

resource "aws_security_group_rule" "database_from_nodes" {
  type                     = "ingress"
  security_group_id        = aws_security_group.database.id
  source_security_group_id = aws_security_group.nodes.id
  protocol                 = "tcp"
  from_port                = 5432
  to_port                  = 5432
  description              = "API, worker and migration pods"
}

# RDS exports to a log group with this exact name. Creating it up front is the
# only way to control retention and encryption of the exported PostgreSQL logs.
resource "aws_cloudwatch_log_group" "postgres" {
  name              = "/aws/rds/cluster/${local.name}-pg/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn

  tags = local.tags
}

resource "aws_rds_cluster_parameter_group" "main" {
  name        = "${local.name}-pg"
  family      = local.aurora_parameter_group_family
  description = "Blackbox Aurora PostgreSQL parameters"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
  }

  parameter {
    name  = "pg_stat_statements.track"
    value = "all"
  }

  parameter {
    # Slow-query visibility for the SLO work; 500ms is below the retrieval SLO.
    name  = "log_min_duration_statement"
    value = "500"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  parameter {
    name  = "log_lock_waits"
    value = "1"
  }

  parameter {
    name  = "log_statement"
    value = "ddl"
  }

  parameter {
    # A leaked connection must not hold a row lock forever.
    name  = "idle_in_transaction_session_timeout"
    value = "60000"
  }

  tags = local.tags
}

data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_rds_cluster" "main" {
  cluster_identifier = "${local.name}-pg"

  engine         = "aurora-postgresql"
  engine_version = var.aurora_engine_version
  engine_mode    = "provisioned"

  database_name   = var.database_name
  master_username = var.database_master_username

  # RDS creates and rotates the master credential in Secrets Manager. The secret
  # is encrypted with the AWS-managed `aws/secretsmanager` key: pointing it at a
  # CMK additionally requires an explicit grant for the RDS service role.
  manage_master_user_password = true

  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.main.name

  storage_encrypted = true
  kms_key_id        = aws_kms_key.data.arn

  backup_retention_period      = var.database_backup_retention_days
  preferred_backup_window      = "01:00-02:00"
  preferred_maintenance_window = "sun:03:00-sun:04:00"
  copy_tags_to_snapshot        = true

  deletion_protection       = var.database_deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-pg-final-${random_id.final_snapshot.hex}"

  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]

  apply_immediately = false

  tags = local.tags

  depends_on = [aws_cloudwatch_log_group.postgres]
}

resource "aws_rds_cluster_instance" "main" {
  count = var.aurora_instance_count

  identifier         = "${local.name}-pg-${count.index}"
  cluster_identifier = aws_rds_cluster.main.id
  instance_class     = var.aurora_instance_class
  engine             = aws_rds_cluster.main.engine
  engine_version     = aws_rds_cluster.main.engine_version

  db_subnet_group_name = aws_db_subnet_group.main.name
  publicly_accessible  = false

  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  monitoring_interval = 60
  monitoring_role_arn = aws_iam_role.rds_monitoring.arn

  promotion_tier             = count.index
  auto_minor_version_upgrade = false
  apply_immediately          = false

  tags = merge(local.tags, { Name = "${local.name}-pg-${count.index}" })
}
