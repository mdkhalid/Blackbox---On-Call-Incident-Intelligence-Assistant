---
id: arch-009
title: 'Architecture: checkout-service'
doc_type: arch
services:
- checkout-service
- payment-gateway
- inventory-service
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
checkout-service handles core business logic and depends on upstream services.

## Dependencies
checkout-service depends on: payment-gateway, inventory-service.

## Data Flows
Requests flow from API gateway to checkout-service, then to payment-gateway, inventory-service.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
