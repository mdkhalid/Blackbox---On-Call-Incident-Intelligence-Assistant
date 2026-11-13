data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  name = "${var.project_name}-${var.environment}"

  tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "blackbox"
    },
    var.tags,
  )

  # `aurora-postgresql16` from `16.4`: Aurora tracks the major version in the
  # parameter-group family, not the minor version.
  aurora_parameter_group_family = "aurora-postgresql${split(".", var.aurora_engine_version)[0]}"

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}
