# GitOps and canary delivery (Argo CD + Argo Rollouts)

This directory is the reference delivery layer on top of the Helm chart.

## Ownership split

- The chart renders the shared platform: ConfigMap, Secret reference, Service, Ingress,
  NetworkPolicy, PodDisruptionBudget, ServiceMonitor, the worker Deployment and the
  `alembic upgrade head` migration Job. The API Deployment is disabled in
  `argocd-values.yaml` (`api.enabled: false`): the Rollout below owns the API pods.
- `application-chart.yaml` is the Argo CD `Application` that syncs the chart with
  automated prune/self-heal.
- `api-rollout.yaml`, `canary-service.yaml` and `analysis-template.yaml` are applied as a
  second Argo CD Application (or with `kubectl apply`) into the same namespace.

## Canary flow

1. Argo CD syncs the chart (new image/config). The Rollout detects the template change.
2. 10% of traffic goes to the canary via the nginx ingress (header `X-Canary: true` always
   reaches it). The `blackbox-slo-analysis` AnalysisTemplate runs for three minutes:
   5xx error ratio, p95 latency and LLM error ratio must stay below the thresholds.
3. On success the weight goes to 50%, then 100%; each step re-runs the analysis.
4. On failure Argo Rollouts aborts and keeps the stable pods serving the previous revision.

```bash
kubectl argo rollouts get rollout blackbox -n blackbox --watch
kubectl argo rollouts promote blackbox -n blackbox
kubectl argo rollouts abort blackbox -n blackbox
```

## Blue-green

The same template can be switched to `strategy.blueGreen` with `activeService`/`previewService`
and `autoPromotionEnabled: false` if you prefer an all-or-nothing promotion. For a
competing-consumer worker that pulls from Redis the chart's rolling Deployment is the right
shape: a blue-green worker would have two active consumers draining the same queue.

## Migration ordering

The chart's migration Job is a Helm pre-install/pre-upgrade hook. Argo CD runs Helm hooks
in the sync phase, so a failed migration aborts the sync and the Rollout is never started.
Do not remove the hook in `argocd-values.yaml`.

## Rollback

Argo CD self-heal restores the previous Git revision; Argo Rollouts keeps the previous
pod template in `revisionHistoryLimit`. Either revert the commit or
`kubectl argo rollouts undo blackbox -n blackbox`.

## Notes

- `api.autoscaling.enabled` is false in `argocd-values.yaml` because the HPA would target
  the (now absent) Deployment. Use Argo Rollouts' own autoscaling or scale the Rollout with
  an HPA whose `scaleTargetRef.kind` is `Rollout`.
- The Rollout pod template is a copy of the chart's Deployment. Keep them in sync when you
  change probes, resources or the image pull policy; a drift check is on the Phase 4 backlog.