import { useCallback, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, ConfidenceMeter, EmptyState, LoadingBlock } from '../ui/Feedback'
import { SeverityBadge, StatusBadge, HealthDot } from '../ui/Badge'
import { Icon } from '../ui/Icon'
import { AgentStreamTimeline } from '../components/AgentStreamTimeline'
import { HypothesisPanel } from '../components/HypothesisPanel'
import {
  isRunLive,
  queryKeys,
  useIncident,
  useIncidentRun,
  useIncidentTimeline,
  useRunEvents,
  useRunTimeline,
  useServices,
} from '../lib/queries'
import { api } from '../api/client'
import { toMessage } from '../utils/errors'
import { formatTime } from '../lib/format'
import { escalationLabel, outcomeLabel, serviceLabel, stageLabel, stageProgress } from '../lib/labels'
import {
  zhCategory,
  zhHealth,
  zhRisk,
  zhSeverity,
  zhStatus,
} from '../i18n'
import type { AgentRun, IncidentTimelineEvent, RootCause } from '../types'

/**
 * IncidentDetailPage — three-column incident command view.
 *
 *   left   facts & timeline   (what happened)
 *   centre agent trace        (what the AI did)
 *   right  diagnosis / plan / approval (what we will do)
 *
 * Every column reads persisted backend state. The page no longer starts an
 * investigation on mount: it reads the incident's existing run via
 * `GET /agent/incidents/{id}/run` and offers to start one only when the
 * incident has never been investigated. Loading the page is therefore
 * idempotent — refreshing, opening a second tab, or arriving from a bookmark
 * all show the same run rather than racing to create a new one.
 */
export function IncidentDetailPage() {
  const { incidentId } = useParams<{ incidentId: string }>()
  const incidentQ = useIncident(incidentId)
  const servicesQ = useServices()
  const runQ = useIncidentRun(incidentId)
  const incidentTimelineQ = useIncidentTimeline(incidentId)
  const queryClient = useQueryClient()

  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [approvalBusy, setApprovalBusy] = useState(false)
  const [approvalError, setApprovalError] = useState<string | null>(null)

  const incident = incidentQ.data
  const run = runQ.data ?? null
  const live = isRunLive(run?.status)
  const timelineQ = useRunTimeline(run?.id, live)
  // The hypothesis lifecycle lives only in the event log, not in the timeline
  // endpoint, so the rejected candidates need their own fetch.
  const eventsQ = useRunEvents(run?.id, live)

  const start = useCallback(async () => {
    if (!incident || starting) return
    setStartError(null)
    setStarting(true)
    try {
      await api.agent.startInvestigation(incident.id, incident.scenario ?? undefined)
      await queryClient.invalidateQueries({
        queryKey: queryKeys.incidentRun(incident.id),
      })
      queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns })
      queryClient.invalidateQueries({ queryKey: queryKeys.agentStats })
    } catch (e) {
      setStartError(toMessage(e))
    } finally {
      setStarting(false)
    }
  }, [incident, starting, queryClient])

  const respond = useCallback(
    async (action: 'approve' | 'reject') => {
      const approval = run?.approval_required
      if (!approval || approvalBusy || !incident) return
      setApprovalBusy(true)
      setApprovalError(null)
      try {
        if (action === 'approve') await api.agent.approveRecovery(approval.id)
        else await api.agent.rejectRecovery(approval.id, '操作员已拒绝')
        // Re-read rather than trusting the POST body: the resumed graph keeps
        // writing after it returns, so the response is a snapshot, not the
        // final word.
        await queryClient.invalidateQueries({
          queryKey: queryKeys.incidentRun(incident.id),
        })
        queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns })
        queryClient.invalidateQueries({ queryKey: queryKeys.agentStats })
      } catch (e) {
        setApprovalError(toMessage(e))
      } finally {
        setApprovalBusy(false)
      }
    },
    [run, approvalBusy, incident, queryClient],
  )

  const serviceName = incident ? serviceLabel(incident) : undefined
  const service = useMemo(
    () => (servicesQ.data ?? []).find((s) => s.name === serviceName),
    [servicesQ.data, serviceName],
  )

  if (incidentQ.isLoading) return <LoadingBlock label="加载故障详情…" />

  if (incidentQ.error || !incident) {
    return (
      <>
        <PageHeader title="故障详情" />
        <Alert tone="critical" title="无法加载该故障">
          {incidentQ.error ? toMessage(incidentQ.error) : '故障不存在'}
        </Alert>
        <div style={{ marginTop: 'var(--space-4)' }}>
          <Link to="/incidents">
            <Button icon={<Icon name="chevron-left" size={14} />}>返回故障列表</Button>
          </Link>
        </div>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title={incident.title}
        description={
          <span className="mono">
            #{incident.id.slice(0, 8)} · {serviceName}
          </span>
        }
        actions={
          <div className="row" style={{ gap: 'var(--space-2)' }}>
            <SeverityBadge severity={incident.severity} label={zhSeverity(incident.severity)} />
            <StatusBadge status={incident.status} label={zhStatus(incident.status)} />
            {run ? (
              <Button
                variant="ghost"
                size="sm"
                icon={<Icon name="refresh" size={14} />}
                loading={starting}
                onClick={start}
              >
                重新调查
              </Button>
            ) : null}
          </div>
        }
      />

      <div className="incident-grid">
        {/* ── LEFT: facts ─────────────────────────────────────────────── */}
        <div className="incident-col">
          <Panel title="故障信息">
            <InfoRow label="服务" value={serviceName ?? '—'} />
            {incident.scenario && <InfoRow label="场景" value={incident.scenario} />}
            <InfoRow label="创建" value={formatTime(incident.created_at)} />
            <InfoRow label="更新" value={formatTime(incident.updated_at)} />
            {incident.description && <p className="incident-desc">{incident.description}</p>}
          </Panel>

          <Panel title="服务状态">
            {service ? (
              <div className="service-status-row">
                <span className="mono">{service.name}</span>
                <span className="row" style={{ gap: 8, alignItems: 'center' }}>
                  <HealthDot health={service.health ?? 'unknown'} label={zhHealth(service.health ?? 'unknown')} />
                  {zhHealth(service.health ?? 'unknown')}
                </span>
              </div>
            ) : (
              <EmptyState compact title="暂无状态数据" />
            )}
          </Panel>

          <Panel title="时间线">
            <IncidentTimelineList
              events={incidentTimelineQ.data?.events}
              loading={incidentTimelineQ.isLoading}
            />
          </Panel>
        </div>

        {/* ── CENTRE: agent trace ─────────────────────────────────────── */}
        <div className="incident-col incident-col-center">
          <Panel flush>
            {runQ.isLoading ? (
              <LoadingBlock label="加载调查记录…" />
            ) : startError ? (
              <div style={{ padding: 'var(--space-4)' }}>
                <Alert
                  tone="critical"
                  title="启动调查失败"
                  action={
                    <Button size="sm" onClick={start}>
                      重试
                    </Button>
                  }
                >
                  {startError}
                </Alert>
              </div>
            ) : !run ? (
              <div style={{ padding: 'var(--space-4)' }}>
                <EmptyState
                  title="该故障尚未调查"
                  hint="启动后 Agent 会自主取证、提出并否证假设，再给出四态结论之一。"
                  action={
                    <Button onClick={start} loading={starting} icon={<Icon name="lightning" size={14} />}>
                      开始调查
                    </Button>
                  }
                />
              </div>
            ) : (
              <>
                <RunTelemetry run={run} />
                <AgentStreamTimeline
                  key={run.id}
                  run={run}
                  timeline={timelineQ.data}
                  live={live}
                  timelineLoading={timelineQ.isLoading}
                />
              </>
            )}
          </Panel>
        </div>

        {/* ── RIGHT: decisions ────────────────────────────────────────── */}
        <div className="incident-col">
          <Panel title="根因分析">
            {run?.root_cause ? (
              <RootCauseBlock rootCause={run.root_cause} evidenceIds={
                new Set(run.evidence.map((e) => e.id))
              } />
            ) : (
              <EmptyForStatus run={run} />
            )}
          </Panel>

          <Panel
            title="恢复方案"
            subtitle="按顺序执行；高风险步骤会先挂起等人工批准"
          >
            {run?.recovery_plan?.steps?.length ? (
              <>
                <div className="plan-meta">
                  <span className={`badge badge-sm badge-${riskTone(run.recovery_plan.risk_level)}`}>
                    风险 {zhRisk(run.recovery_plan.risk_level)}
                  </span>
                  <span className="plan-status mono">
                    {zhStatus(run.recovery_plan.status)}
                  </span>
                </div>
                <ol className="step-list">
                  {run.recovery_plan.steps.map((s, i) => (
                    <li
                      key={s.id ?? i}
                      className={`step-item${s.status === 'done' ? ' step-item-done' : s.status === 'active' ? ' step-item-active' : ''}`}
                    >
                      <span className="step-index">{s.order ?? i + 1}</span>
                      <span className="step-title">
                        {s.description}
                        {s.target && <span className="step-target mono"> → {s.target}</span>}
                      </span>
                      <span className="step-status">{zhStatus(s.status)}</span>
                      {s.rollback_tool && (
                        <span className="step-rollback mono" title="可回滚">
                          可回滚 · {s.rollback_tool}
                        </span>
                      )}
                    </li>
                  ))}
                </ol>
              </>
            ) : (
              <EmptyState compact title="暂无恢复方案" />
            )}
          </Panel>

          <Panel title="操作审批">
            {run?.approval_required && run.approval_required.status === 'pending' ? (
              <div className="approval-gate">
                <div className="approval-gate-icon">
                  <Icon name="shield" size={16} />
                </div>
                <div className="approval-gate-body">
                  <div className="approval-gate-title">需要人工审批</div>
                  <div className="approval-gate-desc">{run.approval_required.description}</div>
                  <div className="row" style={{ gap: 'var(--space-2)' }}>
                    <Button
                      variant="success"
                      size="sm"
                      loading={approvalBusy}
                      onClick={() => respond('approve')}
                      icon={<Icon name="check" size={14} />}
                    >
                      通过并执行
                    </Button>
                    <Button
                      variant="danger-ghost"
                      size="sm"
                      disabled={approvalBusy}
                      onClick={() => respond('reject')}
                      icon={<Icon name="x" size={14} />}
                    >
                      拒绝
                    </Button>
                  </div>
                  {approvalError && (
                    <div className="hl-crit" style={{ marginTop: 'var(--space-2)' }}>
                      {approvalError}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <EmptyState
                compact
                title={
                  run?.approval_required
                    ? `审批状态：${zhStatus(run.approval_required.status)}`
                    : '无需审批'
                }
              />
            )}
          </Panel>

          <HypothesisPanel events={eventsQ.data?.events} />

          {run?.verification && (
            <Alert
              tone={
                run.verification.status === 'passed'
                  ? 'success'
                  : run.verification.status === 'failed'
                    ? 'critical'
                    : 'info'
              }
              title={
                run.verification.status === 'passed'
                  ? '✓ 验证通过'
                  : run.verification.status === 'failed'
                    ? '✕ 验证失败'
                    : '⏳ 验证中'
              }
            >
              <div>{run.verification.description}</div>
              <div className="mono" style={{ fontSize: 'var(--text-2xs)', marginTop: 4 }}>
                置信度 {(run.verification.confidence * 100).toFixed(0)}%
                {run.verification.checks.length > 0 &&
                  ` · ${run.verification.passed_checks}/${run.verification.total_checks} 项`}
              </div>
              {run.verification.checks.some((c) => c.passed === false) && (
                <ul className="check-list">
                  {run.verification.checks
                    .filter((c) => c.passed === false)
                    .map((c, i) => (
                      <li key={i} className="hl-crit">
                        {c.service ?? ''} {c.metric ?? ''} 未达标
                        {c.reason ? ` — ${c.reason}` : ''}
                      </li>
                    ))}
                </ul>
              )}
            </Alert>
          )}
        </div>
      </div>
    </>
  )
}

/**
 * Run cost and status, read from the run row.
 *
 * Token/tool/retry/time budget lives on `agent_runs`; it was simply never
 * returned. Without it the page could say "investigating" but nothing about
 * how far along, how long, or whether the budget — rather than the evidence —
 * is what will end it.
 */
function RunTelemetry({ run }: { run: AgentRun }) {
  const usage = run.usage
  const budget = run.budget
  const chips: Array<{ label: string; value: string; tone?: 'warn' | 'crit' }> = []

  if (run.current_node) {
    const progress = stageProgress(run.current_node)
    chips.push({
      label: '当前节点',
      value: progress ? `${stageLabel(run.current_node)} · ${progress}` : stageLabel(run.current_node),
    })
  }
  if (usage) {
    chips.push({
      label: '工具调用',
      value: budget ? `${usage.tool_calls}/${budget.tool_calls}` : String(usage.tool_calls),
      tone: budget && usage.tool_calls >= budget.tool_calls ? 'warn' : undefined,
    })
    if (usage.duration_ms != null) {
      chips.push({ label: '耗时', value: formatDuration(usage.duration_ms) })
    }
    if (usage.retries > 0) chips.push({ label: '重试', value: String(usage.retries) })
    if (usage.tokens > 0) chips.push({ label: 'Token', value: String(usage.tokens) })
  }
  if (budget?.exhausted) chips.push({ label: '预算', value: '已耗尽', tone: 'crit' })
  if (run.attempt != null && run.max_attempts != null && run.attempt > 1) {
    chips.push({ label: '尝试', value: `${run.attempt}/${run.max_attempts}` })
  }
  if (run.escalation_reason) {
    chips.push({ label: '升级原因', value: escalationLabel(run.escalation_reason), tone: 'warn' })
  }
  if (run.outcome) chips.push({ label: '结论', value: outcomeLabel(run.outcome) })
  if (run.trace_id) chips.push({ label: 'Trace', value: run.trace_id.slice(0, 12) })

  if (chips.length === 0) return null

  return (
    <div className="run-telemetry">
      {chips.map((chip) => (
        <span
          key={chip.label}
          className={`telemetry-chip${chip.tone ? ` telemetry-chip-${chip.tone}` : ''}`}
        >
          <span className="telemetry-label">{chip.label}</span>
          <span className={chip.tone === 'crit' ? 'hl-crit' : 'telemetry-value'}>{chip.value}</span>
        </span>
      ))}
    </div>
  )
}

function RootCauseBlock({
  rootCause,
  evidenceIds,
}: {
  rootCause: RootCause
  evidenceIds: Set<string>
}) {
  const cited = rootCause.evidence_ids.filter((id) => evidenceIds.has(id))
  return (
    <>
      <div className="rc-title">{rootCause.root_cause}</div>
      <div className="caps-label">
        分类 · {zhCategory(rootCause.category)}
        {rootCause.outcome ? ` · ${outcomeLabel(rootCause.outcome)}` : ''}
      </div>
      <ConfidenceMeter
        value={rootCause.confidence}
        hint="证据支持度，不是正确率。100% 表示本轮采到的证据全部指向该根因，与是否查错无关。"
      />
      {rootCause.reasoning_summary && (
        <p className="rc-reasoning">{rootCause.reasoning_summary}</p>
      )}
      {cited.length > 0 && (
        <div className="rc-evidence">
          <span className="caps-label">依据证据</span>
          <div className="rc-evidence-refs">
            {cited.map((ref) => (
              <span key={ref} className="ref-chip mono">
                {ref}
              </span>
            ))}
          </div>
        </div>
      )}
    </>
  )
}

function IncidentTimelineList({
  events,
  loading,
}: {
  events?: IncidentTimelineEvent[]
  loading: boolean
}) {
  if (loading) return <LoadingBlock label="载入时间线…" />
  if (!events || events.length === 0) {
    return <EmptyState compact title="暂无记录" />
  }
  return (
    <ul className="timeline">
      {events.map((evt) => (
        <TimelineItem
          key={evt.id}
          time={evt.created_at}
          text={evt.summary || evt.event_type}
          actor={evt.actor_type === 'agent' ? 'Agent' : evt.actor}
          tone={toneForIncidentEvent(evt.event_type)}
        />
      ))}
    </ul>
  )
}

function TimelineItem({
  time,
  text,
  tone,
  actor,
}: {
  time: string
  text: string
  tone?: 'critical' | 'success'
  actor?: string
}) {
  return (
    <li className={`timeline-item${tone ? ` timeline-item-${tone}` : ''}`}>
      <span className="timeline-dot" />
      <div className="timeline-content">
        <div className="timeline-msg">
          {text}
          {actor && <span className="timeline-actor">{actor}</span>}
        </div>
        <div className="timeline-time mono">{formatTime(time)}</div>
      </div>
    </li>
  )
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="info-row">
      <span className="info-label">{label}</span>
      <span className="info-value mono">{value}</span>
    </div>
  )
}

function EmptyForStatus({ run }: { run: AgentRun | null }) {
  if (!run) return <EmptyState compact title="尚未开始调查" />
  if (run.status === 'failed') {
    return <EmptyState compact tone="critical" title="调查失败" hint={run.error ?? undefined} />
  }
  // A completed run with no cause is a real, honest outcome — say which one.
  if (run.status === 'completed') {
    if (run.outcome === 'INSUFFICIENT_EVIDENCE') {
      return (
        <EmptyState
          compact
          title="证据不足，未给出根因"
          hint="Agent 主动放弃下结论，而不是给出一个它无法证明的答案。"
        />
      )
    }
    if (run.outcome === 'INVESTIGATION_FAILED') {
      return (
        <EmptyState
          compact
          tone="critical"
          title="调查未能完成"
          hint={run.escalation_reason ? escalationLabel(run.escalation_reason) : undefined}
        />
      )
    }
    return <EmptyState compact title="本次调查未得出结论" />
  }
  return <EmptyState compact title="等待诊断结果…" />
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
}

function riskTone(risk: string | undefined): string {
  switch ((risk ?? '').toUpperCase()) {
    case 'CRITICAL':
      return 'critical'
    case 'HIGH':
      return 'critical'
    case 'MEDIUM':
      return 'warning'
    default:
      return 'neutral'
  }
}

function toneForIncidentEvent(eventType: string): 'critical' | 'success' | undefined {
  if (eventType.includes('rejected') || eventType.includes('failed')) return 'critical'
  if (eventType === 'incident.resolved') return 'success'
  return undefined
}
