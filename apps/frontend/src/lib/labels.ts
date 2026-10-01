/**
 * Small label helpers that don't fit the enum-translation table in `i18n.ts`.
 *
 * The enum tables answer "what is this value called"; these answer "what do we
 * call this concept on screen". Keeping them separate stops `i18n.ts` from
 * accumulating one-off strings.
 */

export function zhConfidenceLabel(): string {
  return '置信度'
}

/**
 * The workflow's real nodes, in execution order — mirrors the backend's
 * `AgentStage` enum, which is what `step.stage` and `run.current_node`
 * actually contain.
 *
 * This used to hold a four-item list of *run statuses* (`investigating`,
 * `awaiting_approval`, …) presented as pipeline stages. They are different
 * vocabularies: a status says how the run is doing, a stage says which node it
 * is in. Conflating them meant no stage label ever matched a real node name.
 */
export const AGENT_STAGE_NAMES = [
  'load_context',
  'triage',
  'investigation_planner',
  'parallel_investigation',
  'evidence_aggregation',
  'hypothesis_generation',
  'hypothesis_verification',
  'root_cause_diagnosis',
  'recovery_planner',
  'risk_assessment',
  'human_approval',
  'recovery_executor',
  'rollback',
  'verification',
  'postmortem',
] as const

const STAGE_ZH: Record<string, string> = {
  load_context: '载入上下文',
  triage: '分诊定级',
  investigation_planner: '制定调查计划',
  parallel_investigation: '并行取证',
  evidence_aggregation: '汇总证据',
  hypothesis_generation: '生成假设',
  hypothesis_verification: '验证假设',
  root_cause_diagnosis: '根因诊断',
  recovery_planner: '制定恢复方案',
  risk_assessment: '风险评估',
  human_approval: '人工审批',
  recovery_executor: '执行恢复',
  rollback: '回滚补偿',
  verification: '验证恢复',
  postmortem: '生成复盘',
}

/** Display name for a workflow node. Unknown names fall through verbatim. */
export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return '—'
  return STAGE_ZH[stage] ?? stage
}

/**
 * How far along the workflow a node is, e.g. `12 / 15`.
 *
 * Returns `null` for a node name outside the enum rather than inventing a
 * position — a run parked on an unknown node is a fact worth showing as
 * "unknown", not as "step 1".
 */
export function stageProgress(stage: string | null | undefined): string | null {
  if (!stage) return null
  const index = (AGENT_STAGE_NAMES as readonly string[]).indexOf(stage)
  if (index < 0) return null
  return `${index + 1} / ${AGENT_STAGE_NAMES.length}`
}

/**
 * Display name for the service an incident affects.
 *
 * The backend always returns `service_id` but only denormalises the *name* when
 * it is known, so every place that renders a service must fall back rather than
 * showing an empty cell.
 */
export function serviceLabel(incident: { service_name?: string | null; service_id?: string }): string {
  if (incident.service_name) return incident.service_name
  if (incident.service_id) return incident.service_id.slice(0, 8)
  return '未知服务'
}

/* ---------------------------------------------------------------------------
 * The four outcomes and the escalation reasons.
 *
 * Both are wire vocabularies (`DiagnosisOutcome`, `EscalationReason`) that the
 * incident page, the agent roster and the evaluation report all render, so
 * they live here rather than being re-declared per page — two copies of
 * "ROOT_CAUSE_CONFIRMED → 根因已确认" is two chances to disagree.
 * ------------------------------------------------------------------------- */

const OUTCOME_ZH: Record<string, string> = {
  ROOT_CAUSE_CONFIRMED: '根因已确认',
  ROOT_CAUSE_PROBABLE: '根因很可能成立',
  INSUFFICIENT_EVIDENCE: '证据不足',
  INVESTIGATION_FAILED: '调查失败',
  // Not a member of the backend's `DiagnosisOutcome`: a run that never got far
  // enough to produce a verdict stores no outcome, and some read paths
  // denormalise that as `UNKNOWN`. It has to read as an honest Chinese
  // sentence rather than as the bare token.
  UNKNOWN: '未得出结论',
}

const OUTCOME_TONE: Record<string, string> = {
  ROOT_CAUSE_CONFIRMED: 'success',
  ROOT_CAUSE_PROBABLE: 'primary',
  INSUFFICIENT_EVIDENCE: 'warning',
  INVESTIGATION_FAILED: 'critical',
}

const ESCALATION_ZH: Record<string, string> = {
  BUDGET_EXHAUSTED: '调查预算耗尽',
  TOOL_FAILURES: '工具持续失败',
  NO_HYPOTHESIS_CONFIRMED: '没有假设能被证实',
  RECOVERY_FAILED: '恢复动作均未生效',
  VERIFICATION_FAILED: '恢复后验证未通过',
  CRITICAL_RISK_UNACTIONABLE: '风险过高，无法自动处置',
  APPROVAL_REJECTED: '人工审批被拒绝',
  NODE_ERROR: '工作流节点内部报错',
}

/** An unknown value falls through verbatim rather than becoming "unknown". */
export function outcomeLabel(outcome: string): string {
  return OUTCOME_ZH[outcome] ?? outcome
}

/** Badge tone for an outcome; unknown values stay neutral. */
export function outcomeTone(outcome: string): string {
  return OUTCOME_TONE[outcome] ?? 'neutral'
}

export function escalationLabel(reason: string): string {
  return ESCALATION_ZH[reason] ?? reason
}
