---
id: alert-002
title: 'Alert: order-service ERR_AUTH_FAILED'
doc_type: alert
services:
- order-service
severity: SEV2
date: '2024-01-01'
error_codes:
- ERR_AUTH_FAILED
---

## Alert Definition
Alert name: order-service_ERR_AUTH_FAILED_high
Condition: error rate > 5% for 5 minutes
Threshold: 5%
Linked runbook: RB-007
