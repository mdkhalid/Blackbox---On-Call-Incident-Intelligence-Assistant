---
id: arch-003
title: 'Architecture: inventory-service'
doc_type: arch
services:
- inventory-service
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
inventory-service handles core business logic and depends on upstream services.

## Dependencies
inventory-service depends on: none.

## Data Flows
Requests flow from API gateway to inventory-service, then to data store.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
