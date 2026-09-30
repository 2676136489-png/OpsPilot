# Third-Party Payment Timeout

**Service**: checkout, payment-service  
**Category**: third_party  
**Severity**: high  
**Source**: SRE Runbook — v1.8  

---

## Symptoms

How to recognize a payment provider outage:

- Logs show `httpx.ReadTimeout: connect timeout (30s) calling POST https://api.acme-pay.com/v2/charge`
- HTTP 504 Gateway Timeout from payment provider visible in service logs
- `payment_timeout_rate` metric climbs above 20% (normal <2%)
- Payment success rate drops from baseline (~97%) to below 50%
- P99 latency spikes above 7000ms (requests wait for timeout + retries)
- Circuit breaker transitions to OPEN state after consecutive failures
- Webhook delivery delays increase (upstream queue backed up)

## Root Cause Pattern

External payment provider incidents:

1. **Provider regional outage** — AWS region failure, CDN issue, or provider datacenter downtime
2. **Provider rate limiting** — Our request volume exceeds provider's per-second quota; returns 429
3. **Network connectivity** — BGP route leak or DDoS targeting the provider's edge network
4. **Provider API version deprecation** — Old SDK calls deprecated endpoint that's being rate-limited
5. **TLS/certificate issues** — Provider rotates certificates; client handshake fails

## Resolution Steps

### Step 1 — Confirm it's not our code

```bash
# Check deployment history — if no recent deploys, almost certainly upstream
kubectl rollout history deployment/payment-service

# Check for 429 vs 504 vs connection errors in logs
kubectl logs -l app=payment-service --since=10m | grep -E "504|429|timeout" | head -20
```

### Step 2 — Retry with exponential backoff (client-side)

If retry storm is amplifying the issue:
```python
# Current retry policy — may be too aggressive
retry 5 times, no backoff → retry 2 times with 1s, 2s backoff

# Add jitter to prevent thundering herd
import random
delay = min(2**attempt, 10) + random.uniform(0, 0.5)
```

### Step 3 — Circuit breaker

Ensure CB is configured correctly:
```yaml
# Resilience4j or similar
failureRateThreshold: 50  # open at 50% failure
waitDurationInOpenState: 30s  # try again after 30s
slidingWindowSize: 20  # last 20 calls
```

Circuit breaker should transition: CLOSED → OPEN → HALF_OPEN → CLOSED when provider recovers.

### Step 4 — Switch to backup provider

```python
# Fallback to secondary provider
primary = AcmePayClient()
secondary = StripeClient()

try:
    result = primary.charge(request)
except TimeoutError:
    result = secondary.charge(request)
```

Verify secondary provider credentials are pre-configured and tested.

### Step 5 — Notify provider

- Check provider status page (status.acme-pay.com)
- Open priority support ticket with incident ID
- Share error rate and latency metrics as evidence

## Verification

- `payment_timeout_rate` drops below 5% within 10 minutes of intervention
- Payment success rate returns to >95%
- Circuit breaker returns to CLOSED state
- P99 latency decreases below 2000ms
- Both primary and secondary provider dashboards accessible and healthy
- No new 504 errors in the last 5 minutes

## Preventive Actions

- Set up provider health check (poll `/health` endpoint every 30s)
- Configure alert on `payment_timeout_rate > 10%`
- Pre-provision at least one backup payment provider
- Add bulkhead pattern — isolate payment service thread pool from rest of application
