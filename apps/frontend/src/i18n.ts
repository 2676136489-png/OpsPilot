/** 前端中文化工具：把后端返回的英文枚举值转成中文展示。 */

export const severityZh: Record<string, string> = {
  critical: '紧急',
  high: '高危',
  medium: '中等',
  low: '低危',
}

export const statusZh: Record<string, string> = {
  // Incident status — the wire uses the *domain* vocabulary, which is
  // upper-case. The lookup lower-cases its input, so these keys are lower-case
  // and cover both the upper-case domain values and the lower-case run values
  // below. Before this, `CREATED` fell through and the badge literally read
  // "CREATED".
  created: '已创建',
  triaging: '分诊中',
  investigating: '调查中',
  diagnosing: '诊断中',
  waiting_approval: '等待审批',
  awaiting_approval: '等待审批',
  recovering: '恢复中',
  rolling_back: '回滚中',
  verifying: '验证中',
  escalated: '已升级',
  // API-level incident buckets
  open: '待处理',
  mitigated: '已缓解',
  resolved: '已解决',
  closed: '已关闭',
  // AgentRunStatus — mirrors the backend `_STATUS_MAP`. There is no
  // "recovering" state on the wire; recovery is reported by the plan's own
  // status and by `current_node`.
  pending: '排队中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  // Verification / step status
  passed: '通过',
  // RecoveryStep status
  active: '执行中',
  done: '已完成',
  ineffective: '调用成功但未改变环境',
  skipped: '已跳过',
  // RecoveryPlan status
  draft: '草拟',
  pending_approval: '待审批',
  executing: '执行中',
  executed: '已执行',
  rolled_back: '已回滚',
  // ToolCall status
  succeeded: '成功',
  timeout: '超时',
  blocked: '被预算拦截',
  refused: '被拒绝执行',
  // Approval status
  approved: '已通过',
  rejected: '已拒绝',
  not_required: '无需审批',
  expired: '已过期',
  // Hypothesis status
  proposed: '待验证',
  testing: '验证中',
  confirmed: '已证实',
}

/**
 * Risk levels and approval tiers.
 *
 * Both are wire vocabulary (`RiskLevel`, the backend's `TIER_*` constants) and
 * both were previously rendered as raw English identifiers next to Chinese
 * labels — "风险 HIGH，审批级别 approval_and_reverify".
 */
export const riskZh: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
  critical: '极高',
}

export const approvalTierZh: Record<string, string> = {
  auto: '可自动执行',
  approval: '需人工审批',
  approval_and_reverify: '需人工审批并二次验证',
  manual_only: '仅可人工执行',
}

export const approvalDecisionZh: Record<string, string> = {
  approve: '通过',
  approved: '已通过',
  reject: '拒绝',
  rejected: '已拒绝',
}

export const permissionZh: Record<string, string> = {
  read_only: '只读',
  write_external: '写外部系统',
  mutate_infra: '变更基础设施',
  destructive: '破坏性操作',
}

export const relevanceZh: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
  none: '无',
}

export const runStatusZh: Record<string, string> = {
  pending: '排队中',
  investigating: '调查中',
  awaiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
}

export const healthZh: Record<string, string> = {
  healthy: '正常',
  degraded: '降级',
  down: '故障',
  unknown: '未知',
}

export const healthDotZh: Record<string, string> = {
  healthy: '🟢',
  degraded: '🟡',
  down: '🔴',
  unknown: '⚪',
}

/**
 * Root cause categories.
 *
 * The vocabulary is not declared as an enum anywhere: a category is a plain
 * string on the simulator's `Scenario.root_cause_category`, and the diagnostic
 * nodes carry it through as `hyp.category`. Two places actually enumerate the
 * values — `FAULT_DOMAINS` in `agent/investigation.py` (which is what a
 * diagnosis can emit) and `CATEGORY_PLANS` in `agent/recovery.py` (which is
 * what the planner knows how to fix). This table has to cover both, because
 * `zhCategory` falls back to the raw value and a missing key is therefore
 * invisible until an operator reads `dependency` mid-sentence.
 *
 * `repositories/agent_run.py` additionally writes `unknown` when a diagnosis
 * arrives without a category.
 */
export const categoryZh: Record<string, string> = {
  deployment: '部署问题',
  database: '数据库',
  slow_database: '数据库慢查询',
  memory: '内存',
  redis: '缓存/Redis',
  network: '网络',
  third_party: '第三方依赖',
  capacity: '容量不足',
  cascading: '级联故障',
  dependency: '上游依赖',
  unknown: '未知',
}

export function zhSeverity(s?: string) {
  return severityZh[(s ?? '').toLowerCase()] ?? s ?? ''
}
export function zhStatus(s?: string) {
  return statusZh[(s ?? '').toLowerCase()] ?? s ?? ''
}
export function zhRunStatus(s?: string) {
  return runStatusZh[(s ?? '').toLowerCase()] ?? s ?? ''
}
export function zhHealth(h?: string) {
  return healthZh[(h ?? '').toLowerCase()] ?? h ?? ''
}
export function zhCategory(c?: string) {
  return categoryZh[(c ?? '').toLowerCase()] ?? c ?? ''
}
export function zhRisk(r?: string) {
  return riskZh[(r ?? '').toLowerCase()] ?? r ?? ''
}
export function zhApprovalTier(t?: string) {
  return approvalTierZh[(t ?? '').toLowerCase()] ?? t ?? ''
}
export function zhDecision(d?: string) {
  return approvalDecisionZh[(d ?? '').toLowerCase()] ?? d ?? ''
}
export function zhPermission(p?: string) {
  return permissionZh[(p ?? '').toLowerCase()] ?? p ?? ''
}
export function zhRelevance(r?: string) {
  return relevanceZh[(r ?? '').toLowerCase()] ?? r ?? ''
}

/** 把时间戳变成相对时间中文描述 */
export function timeAgoZh(iso: string): string {
  try {
    const now = Date.now()
    const then = new Date(iso).getTime()
    const diffSec = Math.floor((now - then) / 1000)
    if (diffSec < 60) return `${diffSec} 秒前`
    const diffMin = Math.floor(diffSec / 60)
    if (diffMin < 60) return `${diffMin} 分钟前`
    const diffHr = Math.floor(diffMin / 60)
    if (diffHr < 24) return `${diffHr} 小时前`
    return `${Math.floor(diffHr / 24)} 天前`
  } catch {
    return iso
  }
}
