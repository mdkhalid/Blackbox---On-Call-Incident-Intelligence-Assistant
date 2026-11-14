"""Operational drills: failover, restore and tenant move.

These are harnesses, not production automation. Each function measures something the
runbooks claim (RTO, RPO, move completeness) against whatever database URLs are handed
to it, so the numbers in `docs/dr-drill-results.md` come from a run rather than an
estimate.
"""
