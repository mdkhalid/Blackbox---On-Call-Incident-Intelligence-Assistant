---
id: arch-008
title: 'Architecture: auth-service'
doc_type: arch
services:
- auth-service
- user-service
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
auth-service handles core business logic and depends on upstream services.

## Dependencies
auth-service depends on: user-service.

## Data Flows
Requests flow from API gateway to auth-service, then to user-service.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
