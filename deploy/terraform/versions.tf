terraform {
  # 1.7+ for `terraform test` and the modern provider lock format. The reference
  # is validated in CI with 1.9; local validation here used 1.14.
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state is mandatory for a shared environment: local state cannot be
  # locked and a lost laptop must not be a lost VPC. Create the bucket/table
  # out of band (or with a bootstrap stack), then uncomment and initialise with
  # `terraform init -backend-config=...`, or better keep the backend out of the
  # module and pass a partial configuration:
  #
  #   terraform init -backend-config="bucket=example-tfstate" \
  #     -backend-config="key=blackbox/prod/terraform.tfstate" \
  #     -backend-config="region=eu-west-1" \
  #     -backend-config="dynamodb_table=example-tfstate-lock" \
  #     -backend-config="encrypt=true"
  #
  # backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}
