---
id: arch-006
title: 'Architecture: notification-service'
doc_type: arch
services:
- notification-service
- user-service
severity: SEV3
date: '2024-01-01'
error_codes: []
---

## Service Description
notification-service handles core business logic and depends on upstream services.

## Dependencies
notification-service depends on: user-service.

## Data Flows
Requests flow from API gateway to notification-service, then to user-service.

## Known Limits
Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
