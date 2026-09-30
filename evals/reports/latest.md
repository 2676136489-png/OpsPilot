# OpsPilot agent evaluation

- run at: `2026-09-30T10:22:37+00:00`
- scenarios: **12** across the simulator catalogue
- simulator: in-process (ASGI), eval mode on (ground truth reachable)
- wall clock: 15.1s

## Scorecard

| metric | value |
| --- | --- |
| Root cause accuracy | **100.0%** |
| Evidence accuracy (recall) | 70.1% (utilised 29.1%, traceable 33.2%) |
| Tool selection accuracy | 100.0% |
| Investigation steps | avg 18.9 steps, 14.0 distinct stages, replanned in 12/12 runs (4.9 extra node runs on average), 0.0 hypotheses rejected |
| Recovery success rate | 100.0% (environment actually fixed 100.0%) |
| Verification accuracy | 100.0% over 12 scored runs |
| Reported fixed while broken | 0 |
| False diagnosis rate | 0.0% (12 answers, 0 abstentions) |
| Escalation rate | 0.0%  |
| Tool calls | 162 total, avg 13.5/run, 0.0% failed |
| Token usage | 0 total, avg 0.0/run `{'deterministic': 12}` |
| Latency | avg 1237ms, p50 1193ms, p95 1787ms, max 2099ms |
| Trace integrity | 12/12 single-root, 0 dangling, avg 126 spans |

## Per scenario

| scenario | expected | diagnosed | outcome | tools | recall | env | verify | latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-cpu-saturation | capacity | capacity OK | ROOT_CAUSE_PROBABLE | 18 | 0.33 | fixed | passed | 2099ms |
| checkout-db-pool-exhaustion | database | database OK | ROOT_CAUSE_CONFIRMED | 10 | 0.75 | fixed | passed | 1170ms |
| checkout-deployment-cascade | deployment | deployment OK | ROOT_CAUSE_CONFIRMED | 19 | 1.00 | fixed | passed | 1402ms |
| checkout-memory-leak | memory | memory OK | ROOT_CAUSE_CONFIRMED | 10 | 0.75 | fixed | passed | 902ms |
| gateway-dependency-cascade | cascading | dependency OK | ROOT_CAUSE_CONFIRMED | 18 | 0.75 | fixed | passed | 1335ms |
| inventory-cpu-saturation | capacity | capacity OK | ROOT_CAUSE_PROBABLE | 14 | 0.33 | fixed | passed | 1532ms |
| payment-bad-deployment | deployment | deployment OK | ROOT_CAUSE_CONFIRMED | 9 | 1.00 | fixed | passed | 885ms |
| payment-high-error-rate | deployment | deployment OK | ROOT_CAUSE_CONFIRMED | 9 | 0.75 | fixed | passed | 838ms |
| payment-provider-outage | third_party | third_party OK | ROOT_CAUSE_CONFIRMED | 10 | 0.50 | fixed | passed | 1037ms |
| payment-third-party-timeout | third_party | third_party OK | ROOT_CAUSE_CONFIRMED | 10 | 0.50 | fixed | passed | 1067ms |
| postgres-slow-queries | database | database OK | ROOT_CAUSE_CONFIRMED | 19 | 0.75 | fixed | passed | 1358ms |
| redis-failure | redis | redis OK | ROOT_CAUSE_CONFIRMED | 16 | 1.00 | fixed | passed | 1216ms |

## Node path actually taken

Repeats are the point: a re-planned investigation runs `investigation_planner` → `parallel_investigation` again, which a fixed pipeline never does.

- **checkout-cpu-saturation** (14 extra node runs): ctx → triage → plan → collect → assess → hypotheses → test → plan → collect → assess → hypotheses → test → plan → collect → assess → hypotheses → test → plan → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **checkout-db-pool-exhaustion** (1 extra node run): ctx → triage → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **checkout-deployment-cascade** (7 extra node runs): ctx → triage → plan → collect → assess → plan → collect → assess → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **checkout-memory-leak** (1 extra node run): ctx → triage → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **gateway-dependency-cascade** (7 extra node runs): ctx → triage → plan → collect → assess → plan → collect → assess → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **inventory-cpu-saturation** (12 extra node runs): ctx → triage → plan → collect → assess → hypotheses → test → plan → collect → assess → hypotheses → test → plan → hypotheses → test → plan → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **payment-bad-deployment** (1 extra node run): ctx → triage → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **payment-high-error-rate** (1 extra node run): ctx → triage → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **payment-provider-outage** (1 extra node run): ctx → triage → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **payment-third-party-timeout** (1 extra node run): ctx → triage → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **postgres-slow-queries** (7 extra node runs): ctx → triage → plan → collect → assess → plan → collect → assess → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem
- **redis-failure** (6 extra node runs): ctx → triage → plan → collect → assess → hypotheses → test → plan → collect → assess → hypotheses → test → diagnose → recovery-plan → risk → approval → approval → execute → verify → postmortem

## Hypotheses raised

### checkout-cpu-saturation

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | capacity | capacity | 0.65 | testing |
| H002 | capacity | capacity | 0.65 | testing |

### checkout-db-pool-exhaustion

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | database | database | 0.97 | confirmed |
| H002 | deployment | deployment | 0.56 | testing |

### checkout-deployment-cascade

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | deployment | deployment | 0.97 | confirmed |
| H002 | cascading | dependency | 0.62 | testing |

### checkout-memory-leak

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | memory | memory | 0.94 | confirmed |
| H002 | deployment | deployment | 0.56 | testing |

### gateway-dependency-cascade

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | cascading | dependency | 0.82 | confirmed |

### inventory-cpu-saturation

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | capacity | capacity | 0.65 | testing |
| H002 | capacity | capacity | 0.65 | testing |

### payment-bad-deployment

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | deployment | deployment | 0.91 | confirmed |

### payment-high-error-rate

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | deployment | deployment | 0.96 | confirmed |

### payment-provider-outage

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | third_party | third_party | 0.92 | confirmed |

### payment-third-party-timeout

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | third_party | third_party | 0.92 | confirmed |

### postgres-slow-queries

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | slow_database | database | 0.97 | confirmed |
| H002 | cascading | dependency | 0.57 | testing |

### redis-failure

| ref | domain | category | confidence | status |
| --- | --- | --- | --- | --- |
| H001 | redis | redis | 0.70 | testing |
| H002 | redis | redis | 0.97 | confirmed |
| H003 | cascading | dependency | 0.57 | testing |

## By root-cause category

- capacity: 100.0%
- cascading: 100.0%
- database: 100.0%
- deployment: 100.0%
- memory: 100.0%
- redis: 100.0%
- third_party: 100.0%

## By difficulty

- easy: 100.0%
- hard: 100.0%
- medium: 100.0%

## How the numbers are defined

- **root_cause_accuracy** — diagnosed category == scenario root_cause_category (domain aliases accepted, see CATEGORY_ALIASES)
- **evidence_recall** — share of the scenario's expected_evidence keywords present in collected evidence
- **evidence_utilisation** — share of collected evidence the final diagnosis actually cites
- **tool_selection_accuracy** — every tool family the fault requires was actually called
- **recovery_success_rate** — a recovery action succeeded AND the live environment passes the scenario's own criteria
- **verification_accuracy** — the Agent's own verdict agrees with the environment's ground-truth evaluation
- **false_diagnosis_rate** — a CONFIRMED/PROBABLE diagnosis that was wrong (abstentions are not counted)
- **escalation_rate** — run handed over to a human instead of resolving
