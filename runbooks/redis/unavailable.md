# Redis Unavailable

**Service**: api-gateway, gateway-service  
**Category**: redis  
**Severity**: critical  
**Source**: SRE Runbook — v2.0  

---

## Symptoms

Clear indicators that Redis is down or unreachable:

- Logs contain `redis.exceptions.ConnectionError: Error 111 connecting to redis.internal:6379. Connection refused.`
- Logs show repeated `Connection refused after N retries` messages
- Prometheus `redis_connected` metric drops from 1 to 0
- Cache hit rate plummets from normal range (80-95%) to near-zero
- Application falls through to database, causing secondary load spikes
- P95 latency increases 5-10x due to DB fallback path
- Circuit breaker may trip on Redis client side

## Root Cause Pattern

Redis outages typically stem from:

1. **Pod crash / OOM kill** — Redis memory limit too low, eviction policy fails, kernel OOM killer terminates process
2. **Network partition** — K8s node-level network policy change or DNS failure makes Redis service unreachable
3. **Accidental bad deployment** — Config change (e.g., reduced maxmemory from 4gb to 256mb) causes instability
4. **Failover in progress** — Redis Sentinel or Cluster is promoting a replica; brief window of unavailability
5. **Disk full** — AOF/RDB persistence fails when the data directory runs out of disk

## Resolution Steps

### Step 1 — Quick connectivity check

```bash
redis-cli -h redis.internal -p 6379 ping
# Expected: PONG
```

If `Connection refused`: Redis process is down. If timeout: network issue.

### Step 2 — Check Redis pod health

```bash
kubectl get pods -l app=redis -n infra
kubectl describe pod <redis-pod> -n infra
```

Look for OOMKilled, CrashLoopBackOff, or scheduling issues.

### Step 3 — Restart or scale Redis

```bash
# For simple single-instance Redis
kubectl rollout restart deployment/redis -n infra

# For Sentinel/Cluster — promote replica manually if needed
redis-cli -h redis-sentinel sentinel failover mymaster
```

### Step 4 — Verify recovery

```bash
redis-cli -h redis.internal info server | grep redis_version
redis-cli -h redis.internal info memory | grep used_memory_human
redis-cli -h redis.internal CONFIG GET maxmemory
```

Check `redis_connected` metric returns to 1 and cache hit rate recovers.

### Step 5 — Post-incident hardening

- Set proper `maxmemory-policy allkeys-lru` to prevent OOM
- Configure health checks: `kubectl edit deployment redis` — add readiness probe
- Set up Redis Sentinel with 3 replicas for automatic failover
- Monitor disk usage on Redis data directory with alerting at 80%

## Verification

- `redis-cli ping` returns PONG
- `redis_connected` metric stabilizes at 1.0
- Cache hit rate returns to baseline (80-95%)
- Error rate drops below 1% after Redis recovery
- P95 latency returns to pre-incident baseline
- No OOM events in Redis pod logs for 1 hour post-recovery
