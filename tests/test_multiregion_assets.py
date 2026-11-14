"""Multi-region deployment assets: ApplicationSet, region overlays, Terraform, DR compose."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GITOPS = ROOT / "deploy" / "gitops"
TERRAFORM = ROOT / "deploy" / "terraform"


def test_applicationset_deploys_one_app_per_region():
    document = yaml.safe_load((GITOPS / "applicationset.yaml").read_text(encoding="utf-8"))
    assert document["kind"] == "ApplicationSet"
    elements = document["spec"]["generators"][0]["list"]["elements"]
    regions = {element["region"] for element in elements}
    assert regions == {"eu-west-1", "us-east-1"}
    assert document["spec"]["template"]["spec"]["destination"]["server"] == "{{.cluster}}"


def test_region_overlays_set_region_and_default_home():
    eu = yaml.safe_load((GITOPS / "regions" / "values-eu-west-1.yaml").read_text(encoding="utf-8"))
    us = yaml.safe_load((GITOPS / "regions" / "values-us-east-1.yaml").read_text(encoding="utf-8"))
    assert eu["config"]["REGION"] == "eu-west-1"
    assert us["config"]["REGION"] == "us-east-1"
    # The US overlay keeps the EU default: an unmapped tenant is not homed there.
    assert us["config"]["DEFAULT_HOME_REGION"] == "eu-west-1"
    assert us["config"]["REGION_WRITE_PINNING"] == "true"


def test_terraform_multiregion_is_gated():
    text = (TERRAFORM / "multiregion.tf").read_text(encoding="utf-8")
    assert "var.enable_multi_region ? 1 : 0" in text
    assert "aws_rds_global_cluster" in text
    assert "aws_elasticache_global_replication_group" in text
    assert "aws_s3_bucket_replication_configuration" in text


def test_terraform_dns_failover_records():
    text = (TERRAFORM / "dns.tf").read_text(encoding="utf-8")
    assert 'type = "PRIMARY"' in text
    assert 'type = "SECONDARY"' in text
    assert "aws_route53_health_check" in text


def test_dr_compose_runs_two_regions():
    document = yaml.safe_load((ROOT / "docker-compose.dr.yml").read_text(encoding="utf-8"))
    services = document["services"]
    assert services["api-a"]["environment"][1] == "REGION=eu-west-1"
    assert services["api-b"]["environment"][1] == "REGION=us-east-1"
    assert "postgres-replica" in services
