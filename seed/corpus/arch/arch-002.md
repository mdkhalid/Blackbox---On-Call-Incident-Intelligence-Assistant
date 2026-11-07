---
id: arch-002
title: 'Architecture: payment-gateway'
doc_type: arch
services:
- payment-gateway
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
payment-gateway handles core business logic and depends on upstream services.

## Dependencies
payment-gateway depends on: none.

## Data Flows
Requests flow from API gateway to payment-gateway, then to data store.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
