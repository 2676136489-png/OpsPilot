# Memory Leak / OOM Kill

**Service**: api-gateway, user-service  
**Category**: memory  
**Severity**: critical  
**Source**: SRE Runbook — v2.2  

---

## Symptoms of a Memory Leak

Progressive memory consumption leading to inevitable failure:

- `process_memory_mb` metric grows monotonically over time (e.g., +120 MB/minute)
- `memory_percent` reaches and stays above 90%
- Logs contain `OOM killer: process killed by memory pressure (exit code 137)`
- Process restart count increases: `restart_count_1h >= 2`
- GC collection time increases significantly: `gc_collection_time` p99 above 300ms (normal <50ms)
- Logs: `GC unable to reclaim memory — retained heap growth observed`
- No corresponding traffic increase to justify memory growth
- Heap profile shows "unreleased references" in a specific data structure

## Root Cause Pattern

Memory leaks are almost always code-level issues introduced by recent changes:

1. **Unbounded cache growth** — In-memory cache without TTL or size limit accumulates entries indefinitely
2. **Retained references** — A list/dict that is never cleared keeps old objects alive (e.g., request history, event log)
3. **Closure captures** — Function closures hold references to large objects they should release
4. **Thread-local storage** — Per-thread caches grow with thread count
5. **Connection/stream leaks** — Opened files, sockets, or streams not released on error paths
6. **Buggy caching layer** — New feature adds request caching without TTL: "add per-request context caching for 10min TTL" — but TTL forgotten in implementation

## Resolution Steps

### Immediate mitigation

1. **Restart the service** — Quickest way to reclaim memory:
   ```bash
   kubectl rollout restart deployment/api-gateway
   ```
   This buys time for investigation but the leak will recur.

2. **Scale out** (temporary) — Add more replicas so each processes fewer requests:
   ```bash
   kubectl scale deployment/api-gateway --replicas=6  # was 3
   ```

3. **Increase memory limit** (short-term) — Only if restart alone doesn't give enough time:
   ```yaml
   resources:
     limits:
       memory: 4Gi  # was 2Gi
   ```
   This treats symptom, not cause. Remove once fixed.

### Root cause profiling

4. **Capture heap dump before restart** (if possible):
   ```bash
   # For Java/JVM services
   jmap -dump:format=b,file=heap.hprof <pid>
   
   # For Python
   import tracemalloc
   tracemalloc.start()
   # ... let it run ...
   snapshot = tracemalloc.take_snapshot()
   ```

5. **Profile with production traffic replay**:
   ```bash
   # In staging, replay production requests at 1x speed
   # Monitor memory growth — if it reproduces, leak is deterministic
   ```

6. **Bisect recent commits**:
   ```bash
   git log --oneline -20
   # v2.4.0 introduced RequestContext caching — suspect commit
   # Deploy v2.3.5, check if memory stays flat
   ```

7. **Fix the bug**:
   ```python
   # BEFORE (leak)
   _request_cache: dict[str, RequestContext] = {}
   
   def handle_request(request):
       ctx = RequestContext(request)
       _request_cache[request.id] = ctx  # never cleaned up!
   
   # AFTER (fixed) — use TTL cache
   from cachetools import TTLCache
   _request_cache: TTLCache[str, RequestContext] = TTLCache(maxsize=1000, ttl=600)
   ```

## Verification

After deploying the fix:

- `process_memory_mb` stays stable or shows normal GC cycles
- `restart_count_1h` returns to 0
- No OOM kills in pod logs for 24 hours
- GC collection time returns to baseline (<50ms p99)
- Memory profile in staging shows flat heap growth over 1 hour of load test
- Code review confirms TTL or bounded cache in place
