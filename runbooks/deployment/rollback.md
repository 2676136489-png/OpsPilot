# Bad Deployment Rollback

**Service**: any  
**Category**: deployment  
**Severity**: critical  
**Source**: SRE Runbook — v3.0  

---

## Symptoms of a Bad Deployment

How to detect that a recent deployment is the cause of an incident:

- **Immediate health degradation** right after a deployment timestamp in ArgoCD / GitHub Actions
- Error rate spikes from baseline (<1%) to >10% within 5 minutes of rollout start
- Logs contain import errors, startup failures, or runtime panics referencing new code
- Pod readiness probes fail: 0/N replicas ready after rolling update
- `service_replicas_ready` metric drops to 0 from normal count
- A breaking change in the commit message: "BREAKING CHANGE" or "bump version to 3.0"
- Circuit breaker trips on dependent services (e.g., api-gateway marks checkout as DOWN)

## Root Cause Pattern

Bad deployments fall into these categories:

1. **Breaking API change** — Internal import renamed or removed (e.g., `PriceCalculator` → `PricingEngine`)
2. **Database migration mismatch** — Code expects a new column that migration hasn't applied yet
3. **Config drift** — New config value (feature flag, env var) causes unexpected behavior
4. **Dependency bump** — Upstream library (HTTP client, ORM) has a behavior change in minor version
5. **Incomplete testing** — Happy-path tests pass but edge cases fail in production traffic

## Resolution Steps

### Phase 1 — Detection (automated preferred)

```bash
# Check recent deployments
argo list apps --status degraded

# Check which version is currently failing
kubectl rollout history deployment/<service-name>

# Compare error rate before vs after deployment timestamp
# Using Prometheus range query
```

### Phase 2 — Rollback procedure

```bash
# Option A: Roll back via kubectl to previous revision
kubectl rollout undo deployment/<service-name> --to-revision=N

# Option B: Roll back via ArgoCD
argo rollback <app-name> --to-sync-wave=0

# Option C: Traffic split — route 100% to old version (if using Istio/Linkerd)
kubectl apply -f traffic-split-old-v100.yaml
```

Rollback should complete in 30-90 seconds. Verify new pods are running old image.

### Phase 3 — Freeze and investigate

- Pause the CI/CD pipeline for this service: `argo pause <app-name>`
- Notify on-call dev of the broken commit SHA
- Collect the broken deployment logs before cleanup
- Tag the bad version: `git tag bad-vX.Y.Z <sha>`

### Phase 4 — Fix forward (not rollback)

Sometimes rollback is not possible (schema migration already applied, data already written). In that case:
- Deploy a fix commit on top of the broken one
- Canary deploy the fix to 10% of traffic first
- Monitor for 15 minutes before full rollout

## Verification After Rollback

- All replicas ready: `kubectl get pods -l app=<service> | grep Running | wc -l` matches expected count
- `service_replicas_ready` metric returns to baseline count
- Error rate drops from incident level to pre-deployment baseline (<1%)
- P95 latency returns to normal range
- Health endpoint `/health` returns 200 OK from all instances
- Smoke test: run synthetic transaction (e.g., create order → pay → confirm)
- Dependent services' circuit breakers close automatically
- ArgoCD app syncs to "Synced" and "Healthy"

## Post-Incident

- Create hotfix branch off last-good revision
- Add integration test that would have caught this regression
- Review deployment checklist — was breaking change communicated?
- Consider progressive delivery (canary → blue/green) for this service
