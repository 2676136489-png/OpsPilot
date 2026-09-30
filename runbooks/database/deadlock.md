# Database Deadlock

**Service**: payment-service, order-service  
**Category**: database  
**Severity**: medium  
**Source**: SRE Runbook — v2.1  

---

## Symptoms

Deadlocks manifest differently from pool exhaustion:

- Application logs contain PostgreSQL deadlock detection messages: `ERROR: deadlock detected`
- Specific transaction IDs show rollback: `DETAIL: Process X waits for ShareLock on transaction Y`
- Error rate spikes but latency does NOT spike (unlike pool exhaustion)
- Retry storms — clients retry after deadlock rollback, creating pressure
- `pg_stat_activity` shows multiple sessions waiting on `Lock` state simultaneously

## Root Cause Pattern

A deadlock forms when two or more transactions acquire locks in contradictory order:

```
Transaction A: UPDATE orders SET ... WHERE id=1;   -- holds lock on order 1
Transaction B: UPDATE orders SET ... WHERE id=2;   -- holds lock on order 2
Transaction A: UPDATE orders SET ... WHERE id=2;   -- waits for B
Transaction B: UPDATE orders SET ... WHERE id=1;   -- waits for A → deadlock
```

Common causes:

1. **Inconsistent lock ordering** across code paths (e.g., API route 1 updates orders→payments, API route 2 updates payments→orders)
2. **Range locks with Gaps** — concurrent INSERTs into indexed ranges
3. **Foreign key checks** — locking parent rows while child rows are being locked elsewhere
4. **Long-running transactions** — held locks increase deadlock window

## Resolution Steps

### Detection

1. Query active locks:
   ```sql
   SELECT blocked.pid AS blocked_pid,
          blocked.query AS blocked_query,
          blocker.pid AS blocker_pid,
          blocker.query AS blocker_query
   FROM pg_locks blocked
   JOIN pg_locks blocker ON blocked.locktype = blocker.locktype
   WHERE NOT blocked.granted AND blocker.granted;
   ```

2. Enable deadlock logging: `ALTER SYSTEM SET log_lock_waits = on; ALTER SYSTEM SET deadlock_timeout = '1s';`

### Immediate fix

3. **PostgreSQL victim selection** — PostgreSQL automatically kills the youngest transaction in a deadlock cycle. If this is insufficient due to cascading retries:
   - Kill the blocker manually: `SELECT pg_terminate_backend(<pid>);`
   - Scale up read replicas to offload read traffic

### Long-term prevention

4. **Standardize lock ordering** — Review all UPDATE/DELETE paths and ensure they always touch tables in the same order (e.g., always orders → payments, never payments → orders).

5. **Keep transactions short** — Move non-critical work (email sending, analytics) outside the transaction boundary.

6. **Use SELECT ... FOR UPDATE SKIP LOCKED** for queue-style processing to avoid waiting on locked rows.

7. **Add retry with exponential backoff** on the client side:
   ```
   retry 3 times, delays: 100ms → 200ms → 400ms
   ```

## Verification

- Deadlock rate drops to zero in the next 24 hours
- `pg_stat_statements` shows no `deadlock` errors
- Update latency percentiles stable after fix
- All retry paths validated in staging
