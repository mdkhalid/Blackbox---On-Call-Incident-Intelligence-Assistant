---
id: arch-004
title: 'Architecture: order-service'
doc_type: arch
services:
- order-service
- inventory-service
- payment-gateway
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
order-service handles core business logic and depends on upstream services.

## Dependencies
order-service depends on: inventory-service, payment-gateway.

## Data Flows
Requests flow from API gateway to order-service, then to inventory-service, payment-gateway.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
