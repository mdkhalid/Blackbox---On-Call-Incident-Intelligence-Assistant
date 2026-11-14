# ---- Route 53 failover records (reference) -------------------------------------
#
# Requires enable_route53, a domain_name, multi-region enabled, and both regional
# ingress hostnames. The records are CNAMEs at `app.<domain>` because a CNAME cannot
# sit at the zone apex.
#
# This is the global-DNS half of the failover runbook: a failed primary health check
# makes Route 53 answer with the secondary region.

locals {
  dns_failover = (
    var.enable_route53
    && var.domain_name != ""
    && var.enable_multi_region
    && var.primary_ingress_dns != ""
    && var.secondary_ingress_dns != ""
  )
}

resource "aws_route53_health_check" "primary" {
  count = local.dns_failover ? 1 : 0

  fqdn              = var.primary_ingress_dns
  port              = 443
  type              = "HTTPS"
  resource_path     = "/health"
  request_interval  = 30
  failure_threshold = 3

  tags = merge(local.tags, { Name = "${local.name}-primary-health" })
}

resource "aws_route53_health_check" "secondary" {
  count = local.dns_failover ? 1 : 0

  fqdn              = var.secondary_ingress_dns
  port              = 443
  type              = "HTTPS"
  resource_path     = "/health"
  request_interval  = 30
  failure_threshold = 3

  tags = merge(local.tags, { Name = "${local.name}-secondary-health" })
}

resource "aws_route53_record" "app_primary" {
  count = local.dns_failover ? 1 : 0

  zone_id = aws_route53_zone.main[0].zone_id
  name    = "app.${var.domain_name}"
  type    = "CNAME"
  ttl     = var.route53_failover_ttl

  set_identifier  = "primary-${var.aws_region}"
  health_check_id = aws_route53_health_check.primary[0].id

  failover_routing_policy {
    type = "PRIMARY"
  }

  records = [var.primary_ingress_dns]
}

resource "aws_route53_record" "app_secondary" {
  count = local.dns_failover ? 1 : 0

  zone_id = aws_route53_zone.main[0].zone_id
  name    = "app.${var.domain_name}"
  type    = "CNAME"
  ttl     = var.route53_failover_ttl

  set_identifier  = "secondary-${var.secondary_region}"
  health_check_id = aws_route53_health_check.secondary[0].id

  failover_routing_policy {
    type = "SECONDARY"
  }

  records = [var.secondary_ingress_dns]
}
