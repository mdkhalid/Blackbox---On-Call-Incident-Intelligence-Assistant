---
id: alert-003
title: 'Alert: checkout-service ERR_TIMEOUT'
doc_type: alert
services:
- checkout-service
severity: SEV2
date: '2024-01-01'
error_codes:
- ERR_TIMEOUT
---

## Alert Definition
Alert name: checkout-service_ERR_TIMEOUT_high
Condition: error rate > 5% for 5 minutes
Threshold: 5%
Linked runbook: RB-007
