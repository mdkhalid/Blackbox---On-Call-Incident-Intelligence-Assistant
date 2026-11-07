---
id: alert-001
title: 'Alert: search-service ERR_RATE_LIMIT'
doc_type: alert
services:
- search-service
severity: SEV2
date: '2024-01-01'
error_codes:
- ERR_RATE_LIMIT
---

## Alert Definition
Alert name: search-service_ERR_RATE_LIMIT_high
Condition: error rate > 5% for 5 minutes
Threshold: 5%
Linked runbook: RB-008
