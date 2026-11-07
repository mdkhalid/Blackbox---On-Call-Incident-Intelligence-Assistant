---
id: alert-005
title: 'Alert: auth-service ERR_CONN_REFUSED'
doc_type: alert
services:
- auth-service
severity: SEV2
date: '2024-01-01'
error_codes:
- ERR_CONN_REFUSED
---

## Alert Definition
Alert name: auth-service_ERR_CONN_REFUSED_high
Condition: error rate > 5% for 5 minutes
Threshold: 5%
Linked runbook: RB-007
