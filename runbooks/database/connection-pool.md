# Database Connection Pool Exhaustion

**Service**: api-gateway, payment-service, user-service  
**Category**: database  
**Severity**: high  
**Source**: SRE Runbook — v2.3  

---

## Symptoms

The following symptoms indicate a database connection pool exhaustion incident:

- HTTP 503 / 504 errors with messages containing "connection timeout" or "pool limit reached"
- Application logs show `sqlalchemy.exc.TimeoutError: QueuePool limit of size N overflow reached`
- Prometheus metric `db_connections_active` equals `db_connections_max` (saturation ratio 100%)
- P95 latency spikes above 2000ms for all database-dependent endpoints
- Request queuing observed in application logs: "queuing requests"
- CPU usage on database server may be elevated due to connection churn

## Root Cause Pattern

Connection pool exhaustion occurs when:

1. **Slow queries hold connections** — a SELECT that takes 10+ seconds keeps a connection busy, preventing others from using it. Autovacuum operations, missing indexes, or lock waits are common triggers.
2. **Pool size too small** — max_connections configured below peak concurrent demand (e.g., 100 connections for 500 concurrent requests).
3. **Connection leak** — a code path opens a connection without closing it, gradually consuming the pool over minutes or hours.
4. **Database-side block** — PostgreSQL reaches its own max_connections limit, so new connections from the application pool are rejected.

## Resolution Steps

### Immediate mitigation (first 5 minutes)

1. **Restart the affected service pods** — This releases all held connections. Use the orchestrator to restart:
   ```
   kubectl rollout restart deployment/api-gateway
   ```
   Expect ~30 seconds of downtime while pods restart.

2. **Increase pool size temporarily** — If restart alone is insufficient, bump SQLAlchemy `pool_size` + `max_overflow`:
   ```
   pool_size=100  # was 50
   max_overflow=20  # was 10
   pool_pre_ping=True
   ```
   Rolling restart required to pick up the new config.

### Root cause remediation (after service is healthy)

3. **Identify and optimize slow queries**:
   ```sql
   SELECT query, calls, total_time, mean_time
   FROM pg_stat_statements
   ORDER BY total_time DESC
   LIMIT 20;
   ```
   Add missing indexes or rewrite queries that hold connections too long.

4. **Check for connection leaks** — Review recent PRs that touch database context managers. Ensure every `get_session()` is wrapped in a `try/finally` or uses async context manager pattern.

5. **Tune PostgreSQL `max_connections`** if the database-side limit is the bottleneck.

## Verification

- After restart: check `db_connections_active` drops below 80% of max
- After pool increase: confirm error_rate returns to baseline (< 1%)
- Query `pg_stat_activity` shows no sessions waiting on lock
- P95 latency returns to normal range (< 500ms)
- Run synthetic health checks against all database endpoints
