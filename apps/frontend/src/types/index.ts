/**
 * TypeScript interfaces mirroring the backend response contracts.
 *
 * These are hand-written because the backend does not publish an OpenAPI
 * schema to the build. The rule that keeps them honest is that every field
 * here must exist in a backend response, and every field the UI renders must
 * exist here — a field that is only in this file is a field the UI invents.
 * The `AgentRun` block below was rewritten against
 * `services/agent_run.py::AgentRunQueryService._assemble`, which is the single
 * definition of that response.
 */

// ===== Services =====

export type ServiceHealth = 'healthy' | 'degraded' | 'down' | 'unknown'

export interface Service {
  id: string
  name: string
  /**
   * Nullable because the datastore-backed rows carry no health verdict of
   * their own. Declaring it non-null made callers multiply `null` by 100 and
   * render a healthy-looking 0.00% error rate for a service nobody measured.
   */
  health: ServiceHealth | null
  error_rate: number | null
  latency_p95: number | null
  latency_p99: number | null
  cpu_usage?: number | null
  memory_usage?: number | null
  version?: string | null
  description?: string | null
}

// ===== Incidents =====

export type IncidentSeverity = 'critical' | 'high' | 'medium' | 'low'
export type IncidentStatus =
  | 'open'
  | 'investigating'
  | 'mitigated'
  | 'resolved'
  | 'closed'

export interface Incident {
  id: string
  title: string
  description: string | null
  severity: IncidentSeverity
  status: IncidentStatus
  /** FK to the affected service. Always present. */
  service_id: string
  /**
   * Denormalised service name for display. Present for simulator-sourced
   * incidents and DB rows whose service relationship is loaded; fall back to
   * `service_id` when it is absent.
   */
  service_name?: string | null
  /** Simulator scenario that produced this incident (if any). */
  scenario?: string | null
  created_at: string
  updated_at: string
  resolved_at?: string | null
  root_cause?: string | null
  root_cause_category?: string | null
  /** One of the four `DiagnosisOutcome` values, when a diagnosis has run. */
  diagnosis_outcome?: string | null
  diagnosis_summary?: string | null
  confidence?: number | null
}

/** One row of the incident's business timeline (`GET /incidents/{id}/events`). */
export interface IncidentTimelineEvent {
  id: string
  event_type: string
  summary: string
  actor: string
  actor_type: string
  from_status: string | null
  to_status: string | null
  data: Record<string, unknown>
  created_at: string
}

export interface IncidentTimeline {
  incident_id: string
  count: number
  events: IncidentTimelineEvent[]
}

// ===== Deployments =====

export type DeploymentStatus =
  | 'PENDING'
  | 'DEPLOYING'
  | 'SUCCESSFUL'
  | 'FAILED'
  | 'ROLLED_BACK'

export interface Deployment {
  id: string
  service_id: string
  version: string
  previous_version?: string | null
  status: DeploymentStatus
  change_summary?: string | null
  deployer?: string | null
  commit_sha?: string | null
  created_at: string
}

// ===== Evidence / Hypothesis / RootCause =====

export type EvidenceType =
  | 'metric'
  | 'log'
  | 'deployment'
  | 'commit'
  | 'runbook'
  | 'observation'
  | 'dependency'

export type EvidenceSeverity = 'low' | 'medium' | 'high' | 'critical'

export type RootCauseCategory =
  | 'deployment'
  | 'database'
  | 'memory'
  | 'redis'
  | 'network'
  | 'third_party'
  | 'capacity'
  | 'cascading'
  | 'unknown'

export interface Evidence {
  id: string
  type: EvidenceType
  source: string
  service: string
  description: string
  timestamp: string
  value?: Record<string, unknown> | null
  severity: EvidenceSeverity
}

export type HypothesisStatus = 'proposed' | 'testing' | 'confirmed' | 'rejected'

export interface Hypothesis {
  id: string
  description: string
  confidence: number
  evidence_ids: string[]
  reasoning: string
}

export interface RootCause {
  root_cause: string
  service: string
  confidence: number
  /** The `E###` refs the diagnosis rests on. Empty only when it cited none. */
  evidence_ids: string[]
  /** How the verdict was reached, as recorded at diagnosis time. */
  reasoning_summary: string
  category: RootCauseCategory
  /** One of the four `DiagnosisOutcome` values. */
  outcome?: string | null
  resolved_at?: string | null
}

// ===== Recovery =====

/**
 * How a single recovery action ended.
 *
 * `ineffective` is the important one: the call returned success and the
 * environment did not change. Folding it into `done` reports a recovery that
 * never happened.
 */
export type RecoveryStepStatus =
  | 'pending'
  | 'active'
  | 'done'
  | 'ineffective'
  | 'failed'

export type ApprovalTier =
  | 'auto'
  | 'approval'
  | 'approval_and_reverify'
  | 'manual_only'

export interface RecoveryStep {
  id: string
  order: number
  description: string
  action: string
  status: RecoveryStepStatus
  target: string
  parameters: Record<string, unknown>
  risk_level: string
  expected_impact: string
  approval_tier: ApprovalTier | string
  approval_status: string
  required_permission: string
  verification_strategy: string
  /** Present when the action is reversible, and says how. */
  rollback_tool?: string | null
  /** `null` until the action has run; `false` means it ran and changed nothing. */
  effective?: boolean | null
  error?: string | null
  executed_by?: string | null
}

export type RecoveryPlanStatus =
  | 'draft'
  | 'pending_approval'
  | 'approved'
  | 'rejected'
  | 'executing'
  | 'executed'
  | 'failed'
  | 'rolled_back'

export interface RecoveryPlan {
  id: string
  ref: string
  status: RecoveryPlanStatus
  risk_level: string
  steps: RecoveryStep[]
  summary: string
  expected_impact: string
  verification_criteria: string[]
  created_at?: string
  executed_at?: string | null
}

export type VerificationStatus = 'pending' | 'running' | 'passed' | 'failed'

export interface VerificationCheck {
  service?: string
  metric?: string
  passed?: boolean
  value?: unknown
  threshold?: unknown
  reason?: string
}

export interface VerificationResult {
  id: string
  status: VerificationStatus
  description: string
  checks: VerificationCheck[]
  passed_checks: number
  total_checks: number
  /** How sure the verification itself is. A 0.4 pass is not a 0.99 pass. */
  confidence: number
  plan_id?: string | null
  timestamp?: string
}

// ===== Agent Run =====

/** The run vocabulary the API speaks. Mirrors `_STATUS_MAP` server-side. */
export type AgentRunStatus =
  | 'pending'
  | 'investigating'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'

/** The four honest endings of an investigation. */
export type DiagnosisOutcome =
  | 'ROOT_CAUSE_CONFIRMED'
  | 'ROOT_CAUSE_PROBABLE'
  | 'INSUFFICIENT_EVIDENCE'
  | 'INVESTIGATION_FAILED'

export type EscalationReason =
  | 'BUDGET_EXHAUSTED'
  | 'TOOL_FAILURES'
  | 'NO_HYPOTHESIS_CONFIRMED'
  | 'RECOVERY_FAILED'
  | 'VERIFICATION_FAILED'
  | 'MANUAL_ONLY'

export interface RunUsage {
  tool_calls: number
  tokens: number
  retries: number
  seconds: number
  /** Measured wall clock, or the elapsed time so far for a live run. */
  duration_ms: number | null
}

export interface RunBudget {
  tool_calls: number
  tokens: number
  seconds: number
  max_retries: number
  max_parallel_tools: number
  /** True when a limit, not the evidence, is what stopped the investigation. */
  exhausted: boolean
}

/** Present only once the run is terminal — `null` while it is still working. */
export interface RunFinalResult {
  status: AgentRunStatus
  outcome: string | null
  root_cause: string
  category: string
  recovery_status: RecoveryPlanStatus | null
  verification_status: string | null
  escalation_reason: string | null
  error: string | null
}

export interface AgentRun {
  id: string
  incident_id: string
  status: AgentRunStatus
  /** The graph node the run is on (or parked at), e.g. `human_approval`. */
  current_node?: string | null
  interrupted_at_node?: string | null
  confidence: number
  evidence: Evidence[]
  hypotheses: Hypothesis[]
  root_cause: RootCause | null
  recovery_plan: RecoveryPlan | null
  verification: VerificationResult | null
  approval_required?: Approval | null
  error?: string | null
  outcome?: DiagnosisOutcome | string | null
  reasoning_mode?: string
  escalation_reason?: EscalationReason | string | null
  trace_id?: string
  request_id?: string
  attempt?: number
  max_attempts?: number
  usage?: RunUsage
  budget?: RunBudget
  final_result?: RunFinalResult | null
  created_at: string
  started_at?: string
  ended_at?: string
  updated_at: string
}

// ===== Agent timeline =====

export type StepStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'timeout'

export type ToolCallStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'blocked'

export interface ToolCallRecord {
  id: string
  tool_name: string
  status: ToolCallStatus
  arguments: Record<string, unknown>
  result?: Record<string, unknown> | null
  risk_level?: string | null
  permission_level?: string | null
  error_code?: string | null
  error_message?: string | null
  attempt: number
  duration_ms?: number | null
  transport: string
  span_id: string
  created_at: string
}

export interface AgentStep {
  id: string
  sequence: number
  stage: string
  status: StepStatus
  attempt: number
  duration_ms?: number | null
  error?: string | null
  started_at: string
  ended_at: string
  trace_id: string
  span_id: string
  tool_calls: ToolCallRecord[]
}

export interface RunTimeline {
  run_id: string
  count: number
  tool_call_count: number
  steps: AgentStep[]
  /** Calls whose step row is gone. Normally empty; never silently dropped. */
  orphaned_tool_calls: ToolCallRecord[]
}

// ===== Trace =====

export interface TraceSpan {
  span_id: string
  parent_span_id: string
  trace_id: string
  request_id: string
  name: string
  kind: string
  status: string
  error?: string | null
  duration_ms: number
  attributes: Record<string, unknown>
  started_at: string | null
  children?: TraceSpan[]
}

export interface RunTrace {
  run_id: string
  incident_id: string
  trace_id: string
  request_id: string
  status: string
  span_count: number
  by_kind: Record<string, number>
  duration_ms: number
  spans: TraceSpan[]
  tree: TraceSpan[]
}

// ===== Approval =====

export interface Approval {
  id: string
  agent_run_id: string
  type: 'recovery'
  description: string
  status: 'pending' | 'approved' | 'rejected'
  requested_at: string
  responded_at?: string | null
  reason?: string | null
}

// ===== Agent aggregate statistics =====

export interface AgentStats {
  total_runs: number
  completed: number
  failed: number
  in_progress: number
  awaiting_approval: number
  /** null when no run has reached a terminal state yet — render as "—". */
  success_rate: number | null
  recovery_attempted: number
  recovery_verified: number
  /** null when no recovery has been verified yet — render as "—". */
  recovery_rate: number | null
  has_data: boolean
}

// ===== Service health card (simulator) =====

export interface ServiceCard extends Service {
  description?: string | null
}

// ===== Agent events =====
//
// The vocabulary is the backend's `EventType` enum, verbatim: the wire format
// is dot-namespaced (`tool.started`, `evidence.created`). It used to be a
// separate snake_case list that overlapped the server's in zero places, so
// even a correct stream would have matched no listener.

export type AgentEventType =
  | 'agent.started'
  | 'agent.step.started'
  | 'agent.step.completed'
  | 'agent.failed'
  | 'agent.completed'
  | 'agent.budget.exhausted'
  | 'agent.escalated'
  | 'investigation.started'
  | 'investigation.plan_created'
  | 'tool.started'
  | 'tool.completed'
  | 'tool.failed'
  | 'evidence.created'
  | 'hypothesis.created'
  | 'hypothesis.updated'
  | 'hypothesis.rejected'
  | 'diagnosis.completed'
  | 'risk.assessed'
  | 'approval.required'
  | 'approval.decided'
  | 'recovery.plan.created'
  | 'recovery.started'
  | 'recovery.action.completed'
  | 'recovery.completed'
  | 'recovery.failed'
  | 'recovery.rollback.started'
  | 'recovery.rollback.completed'
  | 'verification.started'
  | 'verification.completed'
  | 'postmortem.created'
  | 'heartbeat'
  | 'state.sync'
  // Transport control frames, emitted by the stream itself rather than
  // persisted. `stream.closed` is how the client learns why it stopped
  // reading without having to interpret a bare socket close.
  | 'stream.opened'
  | 'stream.closed'

/**
 * One frame from `GET /agent/runs/{id}/stream` (or `/events`).
 *
 * `data` holds only the fields the backend declared for that event type, so
 * the shape varies by `event_type`. Unknown keys are stripped server-side
 * rather than trusted here.
 */
export interface AgentEvent {
  seq: number
  event_id: string
  run_id: string
  incident_id: string
  event_type: AgentEventType
  stage: string | null
  data: Record<string, unknown>
  created_at: string | null
}

/** Payload of `stream.closed` — why the stream stopped. */
export interface StreamClosedData {
  run_id: string
  status: string
  reason: 'terminal' | 'idle_timeout'
}

// ===== API Response =====

export interface ListResponse<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

// ===== Simulator =====

export interface Scenario {
  name: string
  description: string
  service: string
  severity: IncidentSeverity
  symptoms: string[]
}
