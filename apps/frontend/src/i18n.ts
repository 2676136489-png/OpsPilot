/** 前端中文化工具：把后端返回的英文枚举值转成中文展示。 */

export const severityZh: Record<string, string> = {
  critical: '紧急',
  high: '高危',
  medium: '中等',
  low: '低危',
}

export const statusZh: Record<string, string> = {
  // Incident status
  open: '待处理',
  investigating: '调查中',
  mitigated: '已缓解',
  resolved: '已解决',
  closed: '已关闭',
  // AgentRunStatus — mirrors the backend `_STATUS_MAP`. There is no
  // "recovering" state on the wire; recovery is reported by the plan's own
  // status and by `current_node`.
  pending: '排队中',
  awaiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
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
  running: '执行中',
  succeeded: '成功',
  timeout: '超时',
  blocked: '被预算拦截',
  // Approval status
  approved: '已通过',
  rejected: '已拒绝',
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
 * Keys must stay in sync with the backend `RootCauseCategory` enum in
 * `domain/enums.py` — a missing key makes the UI silently fall back to
 * showing the raw English identifier.
 */
export const categoryZh: Record<string, string> = {
  deployment: '部署问题',
  database: '数据库',
  memory: '内存',
  redis: '缓存/Redis',
  network: '网络',
  third_party: '第三方依赖',
  capacity: '容量不足',
  cascading: '级联故障',
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
