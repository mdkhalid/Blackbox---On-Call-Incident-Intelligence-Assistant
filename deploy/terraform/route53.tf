# Optional Route 53 hosted zone. Disabled by default because the domain name is
# account- and environment-specific. Enable with `enable_route53 = true` and
# `domain_name = "blackbox.example.com"`.

resource "aws_route53_zone" "main" {
  count = var.enable_route53 && var.domain_name != "" ? 1 : 0

  name = var.domain_name

  tags = local.tags
}
