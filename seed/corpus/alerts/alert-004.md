---
id: alert-004
title: 'Alert: checkout-service ERR_RATE_LIMIT'
doc_type: alert
services:
- checkout-service
severity: SEV2
date: '2024-01-01'
error_codes:
- ERR_RATE_LIMIT
---

## Alert Definition
Alert name: checkout-service_ERR_RATE_LIMIT_high
Condition: error rate > 5% for 5 minutes
Threshold: 5%
Linked runbook: RB-005
