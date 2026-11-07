---
id: arch-005
title: 'Architecture: user-service'
doc_type: arch
services:
- user-service
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
user-service handles core business logic and depends on upstream services.

## Dependencies
user-service depends on: none.

## Data Flows
Requests flow from API gateway to user-service, then to data store.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
