---
id: arch-007
title: 'Architecture: search-service'
doc_type: arch
services:
- search-service
- inventory-service
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
search-service handles core business logic and depends on upstream services.

## Dependencies
search-service depends on: inventory-service.

## Data Flows
Requests flow from API gateway to search-service, then to inventory-service.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
