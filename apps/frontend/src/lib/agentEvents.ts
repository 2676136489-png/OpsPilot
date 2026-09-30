/**
 * Client-side registry for agent events: label, tone, and one-line description.
 *
 * `EVENT_META` is a total `Record<AgentEventType, …>`, so adding a member to the
 * `AgentEventType` union is a type error until it is described here. That
 * mirrors the backend's `EVENT_FIELDS` projection table on purpose: the two
 * sides of the wire are then forced to agree about what exists, rather than
 * one silently ignoring what the other sends.
 *
 * The previous version of this module carried a *different* event vocabulary
 * (snake_case: `evidence_added`) from the backend's dot-namespaced `EventType`
 * (`evidence.created`), along with a reducer that patched a local `AgentRun`
 * from those events. Since the real stream never used those names and the run
 * is served by REST anyway, the reducer is gone and this file is now purely
 * presentational.
 */

import type { AgentEvent, AgentEventType } from '../types'

/**
 * Tone vocabulary, deliberately identical to the badge/marker CSS vocabulary
 * (`badge-success`, `marker-warning`, …).
 *
 * It used to be `ok | warn | crit`, which produced `badge-ok` / `marker-crit`
 * — class names no stylesheet defines. The rows still rendered, so nothing
 * looked broken; they just silently lost their colour. One vocabulary, two
 * consumers, no aliases.
 */
export type EventTone = 'neutral' | 'info' | 'success' | 'warning' | 'critical' | 'agent'

export interface EventMeta {
  /** Chip text in the timeline. */
  label: string
  tone: EventTone
}

export const EVENT_META: Record<AgentEventType, EventMeta> = {
  'agent.started': { label: '开始', tone: 'info' },
  'agent.step.started': { label: '节点开始', tone: 'neutral' },
  'agent.step.completed': { label: '节点完成', tone: 'neutral' },
  'agent.completed': { label: '完成', tone: 'success' },
  'agent.failed': { label: '失败', tone: 'critical' },
  'agent.budget.exhausted': { label: '预算耗尽', tone: 'warning' },
  'agent.escalated': { label: '已升级', tone: 'warning' },

  'investigation.started': { label: '分诊', tone: 'info' },
  'investigation.plan_created': { label: '调查计划', tone: 'neutral' },

  'tool.started': { label: '调用工具', tone: 'neutral' },
  'tool.completed': { label: '工具返回', tone: 'neutral' },
  'tool.failed': { label: '工具失败', tone: 'warning' },

  'evidence.created': { label: '证据', tone: 'info' },
  'hypothesis.created': { label: '提出假设', tone: 'info' },
  'hypothesis.updated': { label: '更新假设', tone: 'neutral' },
  'hypothesis.rejected': { label: '否决假设', tone: 'warning' },
  'diagnosis.completed': { label: '诊断', tone: 'success' },

  'risk.assessed': { label: '风险评估', tone: 'warning' },
  'approval.required': { label: '请求审批', tone: 'warning' },
  'approval.decided': { label: '审批结果', tone: 'info' },

  'recovery.plan.created': { label: '恢复方案', tone: 'info' },
  'recovery.started': { label: '开始恢复', tone: 'warning' },
  'recovery.action.completed': { label: '执行动作', tone: 'neutral' },
  'recovery.completed': { label: '恢复完成', tone: 'success' },
  'recovery.failed': { label: '恢复失败', tone: 'critical' },
  'recovery.rollback.started': { label: '开始回滚', tone: 'warning' },
  'recovery.rollback.completed': { label: '回滚完成', tone: 'info' },

  'verification.started': { label: '开始验证', tone: 'neutral' },
  'verification.completed': { label: '验证结果', tone: 'success' },

  'postmortem.created': { label: '复盘', tone: 'info' },

  'heartbeat': { label: '心跳', tone: 'neutral' },
  'state.sync': { label: '状态同步', tone: 'neutral' },

  'stream.opened': { label: '已连接', tone: 'neutral' },
  'stream.closed': { label: '连接结束', tone: 'neutral' },
}

/** Every event name the client listens for, derived from the registry. */
export const AGENT_EVENT_TYPES = Object.keys(EVENT_META) as AgentEventType[]

export function toneOf(type: string): EventTone {
  return EVENT_META[type as AgentEventType]?.tone ?? 'neutral'
}

export function labelOf(type: string): string {
  return EVENT_META[type as AgentEventType]?.label ?? type
}

const str = (v: unknown): string => (v === null || v === undefined ? '' : String(v))
const pct = (v: unknown): string => `${(Number(v ?? 0) * 100).toFixed(0)}%`

/** Render one frame's `data` as a single readable line. */
export function describeEvent(evt: AgentEvent): string {
  const d = evt.data
  switch (evt.event_type) {
    case 'agent.started':
      return '调查已启动'
    case 'agent.step.started':
      return `进入节点 ${evt.stage ?? str(d.stage)}${
        Number(d.attempt ?? 1) > 1 ? `（第 ${str(d.attempt)} 次尝试）` : ''
      }`
    case 'agent.step.completed':
      return `节点 ${evt.stage ?? str(d.stage)} 完成${
        d.duration_ms ? `，耗时 ${str(d.duration_ms)}ms` : ''
      }`
    case 'agent.completed':
      return '调查结束'
    case 'agent.failed':
      return `调查失败：${str(d.error || d.errors) || '未知错误'}`
    case 'agent.budget.exhausted':
      return `预算耗尽，未能执行 ${str(d.tool)}（${str(d.reason)}）`
    case 'agent.escalated':
      return `已移交人工：${str(d.reason)}${d.outcome ? `（${str(d.outcome)}）` : ''}`

    case 'investigation.started':
      return `分诊为 ${str(d.severity)}，服务 ${str(d.service)}`
    case 'investigation.plan_created': {
      const tools = Array.isArray(d.tools) ? d.tools.map(str).join('、') : ''
      const head =
        d.decision === 'replan'
          ? `重新规划（第 ${str(d.iteration)} 轮）`
          : `制定调查计划（第 ${str(d.iteration)} 轮）`
      const why = d.reason ? ` — ${str(d.reason)}` : ''
      const conf = d.confidence !== undefined ? `，置信度 ${pct(d.confidence)}` : ''
      return `${head}：${tools}${conf}${why}`
    }

    case 'tool.started': {
      const args = Object.entries(d.arguments ?? {})
        .map(([k, v]) => `${k}=${str(v)}`)
        .join(', ')
      return `调用 ${str(d.tool_name)}${args ? `(${args})` : ''}`
    }
    case 'tool.completed':
      return `${str(d.tool_name)} 返回成功${
        d.duration_ms ? `，耗时 ${str(d.duration_ms)}ms` : ''
      }`
    case 'tool.failed':
      return `${str(d.tool_name)} 失败：${str(d.error_message || d.error_code)}`

    case 'evidence.created':
      return `[${str(d.severity)}] ${str(d.ref)} ${str(d.title)}${
        d.relevance !== undefined ? `（相关性 ${pct(d.relevance)}）` : ''
      }`
    case 'hypothesis.created': {
      if (d.count !== undefined) return `无法提出假设：${str(d.reason)}`
      return `${str(d.ref)} ${str(d.statement)} — 域 ${str(
        d.domain || d.category,
      )}，置信度 ${pct(d.confidence)}`
    }
    case 'hypothesis.updated':
      return `${str(d.ref)} ${str(d.status)} — 置信度 ${pct(d.confidence)}`
    case 'hypothesis.rejected':
      return `${str(d.ref)} 被否决 — ${str(d.statement)}`
    case 'diagnosis.completed':
      return `${str(d.outcome)}：${str(d.root_cause)}（分类 ${str(
        d.category,
      )}，置信度 ${pct(d.confidence)}）`

    case 'risk.assessed':
      return `风险 ${str(d.risk_level)}，审批级别 ${str(d.approval_tier)}`
    case 'approval.required':
      return `需要审批：风险 ${str(d.risk_level)}／级别 ${str(d.approval_tier)}`
    case 'approval.decided':
      return `审批 ${str(d.decision)}（${str(d.decided_by)}）`

    case 'recovery.plan.created': {
      const n = Array.isArray(d.actions) ? d.actions.length : 0
      return `生成恢复方案：${n} 个动作，风险 ${str(d.risk_level)}`
    }
    case 'recovery.started':
      return `开始执行恢复方案 ${str(d.plan_id).slice(0, 8)}`
    case 'recovery.action.completed': {
      const eff =
        d.effective === false ? '（未产生实际变化）' : d.effective ? '（已改变环境）' : ''
      return `${str(d.ref)} ${str(d.tool)} → ${str(d.status)}${eff}`
    }
    case 'recovery.completed':
      return `恢复完成，生效动作 ${str(d.effective_ref)}`
    case 'recovery.failed':
      return `恢复失败：${str(d.reason)}`
    case 'recovery.rollback.started':
      return '开始回滚'
    case 'recovery.rollback.completed':
      return `${str(d.ref)} 回滚 → ${str(d.status)}`

    case 'verification.started':
      return '开始验证环境'
    case 'verification.completed':
      return `验证 ${str(d.status)}：${str(d.passed_checks)}/${str(d.total_checks)} 项通过`

    case 'postmortem.created':
      return `已生成复盘：${str(d.summary)}`

    case 'stream.opened':
      return '已连接事件流'
    case 'stream.closed':
      return d.reason === 'terminal' ? '调查已结束' : '连接超时，已断开'
    case 'heartbeat':
    case 'state.sync':
      return ''
    default:
      return str(evt.event_type)
  }
}
