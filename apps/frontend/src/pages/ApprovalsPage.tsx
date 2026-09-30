import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, ConfidenceMeter, EmptyState, LoadingBlock } from '../ui/Feedback'
import { Badge, LiveDot } from '../ui/Badge'
import { Icon } from '../ui/Icon'
import { useAgentRuns, queryKeys } from '../lib/queries'
import { api } from '../api/client'
import { toMessage } from '../utils/errors'
import { timeAgo, shortId } from '../lib/format'
import { zhConfidenceLabel } from '../lib/labels'
import type { AgentRun } from '../types'

type ActionState = 'approving' | 'rejecting'

/** Risk levels are `LOW | MEDIUM | HIGH | CRITICAL` on the wire. */
function riskTone(risk: string | null | undefined): string {
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

function riskLevelLabel(risk: string | null | undefined): string {
  switch ((risk ?? '').toUpperCase()) {
    case 'CRITICAL':
      return '极高'
    case 'HIGH':
      return '高'
    case 'MEDIUM':
      return '中'
    case 'LOW':
      return '低'
    default:
      return risk ?? '未评级'
  }
}

/**
 * ApprovalsPage — the human-in-the-loop gate.
 *
 * Every recovery step that mutates a production system must be signed off here.
 * The page is intentionally a queue, not a list: it only surfaces runs whose
 * approval is still `pending`, so the operator's whole job is the visible set.
 */
export function ApprovalsPage() {
  const runsQ = useAgentRuns()
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<Record<string, ActionState>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})

  const pending = useMemo(
    () => (runsQ.data ?? []).filter((r) => r.approval_required?.status === 'pending'),
    [runsQ.data],
  )

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns })
    queryClient.invalidateQueries({ queryKey: queryKeys.agentStats })
    queryClient.invalidateQueries({ queryKey: queryKeys.incidents() })
  }, [queryClient])

  const respond = useCallback(
    async (run: AgentRun, action: ActionState) => {
      const approval = run.approval_required
      if (!approval) return

      setBusy((b) => ({ ...b, [approval.id]: action }))
      setErrors((e) => {
        const next = { ...e }
        delete next[approval.id]
        return next
      })

      try {
        if (action === 'approving') {
          await api.agent.approveRecovery(approval.id)
        } else {
          await api.agent.rejectRecovery(approval.id, '操作员已拒绝')
        }
        invalidate()
      } catch (e) {
        // 409 means somebody else already decided — refresh so the stale row leaves.
        setErrors((prev) => ({ ...prev, [approval.id]: toMessage(e) }))
        invalidate()
      } finally {
        setBusy((b) => {
          const next = { ...b }
          delete next[approval.id]
          return next
        })
      }
    },
    [invalidate],
  )

  return (
    <>
      <PageHeader
        title="审批队列"
        description="所有会改动生产环境的恢复操作都必须在这里获得人工批准后才会执行。"
        actions={
          <>
            <LiveDot state={pending.length > 0 ? 'live' : 'closed'} label={`${pending.length} 待处理`} />
            <Button
              icon={<Icon name="refresh" size={14} />}
              loading={runsQ.isFetching}
              onClick={() => runsQ.refetch()}
            >
              刷新
            </Button>
          </>
        }
      />

      {runsQ.error && (
        <Alert tone="critical" title="加载审批队列失败">
          {toMessage(runsQ.error)}
        </Alert>
      )}

      {runsQ.isLoading ? (
        <LoadingBlock label="加载审批中…" />
      ) : pending.length === 0 ? (
        <Panel>
          <EmptyState
            icon={<Icon name="check" size={22} />}
            title="暂无待处理审批"
            hint="当 Agent 完成诊断并生成恢复方案后，需要签核的操作会出现在这里。"
            action={
              <Link to="/incidents">
                <Button size="sm">前往故障列表</Button>
              </Link>
            }
          />
        </Panel>
      ) : (
        <div className="approval-list">
          {pending.map((run) => {
            const approval = run.approval_required!
            const state = busy[approval.id]
            const error = errors[approval.id]
            const isBusy = Boolean(state)

            return (
              <Panel
                key={approval.id}
                title={
                  <span className="row" style={{ gap: 'var(--space-2)', alignItems: 'center' }}>
                    恢复操作
                    <span className="mono muted">· {run.root_cause?.service ?? '未知服务'}</span>
                  </span>
                }
                actions={<Badge tone="warning">{isBusy ? '处理中…' : '待处理'}</Badge>}
              >
                <div className="approval-meta mono">
                  故障 {shortId(run.incident_id)} · 运行 {shortId(run.id)} · {timeAgo(approval.requested_at)}
                </div>

                <p className="approval-desc">{approval.description}</p>

                {run.root_cause && (
                  <div className="inspection-block">
                    <div className="caps-label">根因</div>
                    <div style={{ marginBottom: 'var(--space-2)' }}>{run.root_cause.root_cause}</div>
                    <ConfidenceMeter value={run.root_cause.confidence} label={zhConfidenceLabel()} />
                  </div>
                )}

                {run.recovery_plan?.steps?.length ? (
                  <>
                    <div className="caps-label" style={{ marginTop: 'var(--space-3)' }}>
                      恢复方案 · {run.recovery_plan.steps.length} 步
                    </div>
                    {/*
                      The risk model is the whole reason this gate exists, so it
                      has to be on the card. Approving a description without
                      seeing the blast radius, the expected effect, and which
                      steps can be undone is a rubber stamp, not a decision.
                    */}
                    <div className="plan-meta">
                      <span className={`badge badge-sm badge-${riskTone(run.recovery_plan.risk_level)}`}>
                        风险 {riskLevelLabel(run.recovery_plan.risk_level)}
                      </span>
                      {run.recovery_plan.expected_impact && (
                        <span className="plan-status">预期影响：{run.recovery_plan.expected_impact}</span>
                      )}
                    </div>
                    <ol className="step-list step-list-sm">
                      {run.recovery_plan.steps.map((s, i) => (
                        <li key={s.id ?? i} className="step-item">
                          <span className="step-index">{s.order ?? i + 1}</span>
                          <span className="step-title">
                            {s.description}
                            {s.target && <span className="step-target mono"> → {s.target}</span>}
                          </span>
                          <span className={`badge badge-sm badge-${riskTone(s.risk_level)}`}>
                            {riskLevelLabel(s.risk_level)}
                          </span>
                          {s.rollback_tool ? (
                            <span className="step-rollback mono" title="该动作可通过工具回滚">
                              可回滚
                            </span>
                          ) : (
                            <span className="step-rollback mono hl-warn" title="该动作不可逆">
                              不可逆
                            </span>
                          )}
                        </li>
                      ))}
                    </ol>
                    {run.recovery_plan.verification_criteria?.length ? (
                      <div className="inspection-block">
                        <div className="caps-label">通过标准 · 执行后必须逐条满足</div>
                        <ul className="check-list">
                          {run.recovery_plan.verification_criteria.map((c, i) => (
                            <li key={i} className="muted">
                              {c}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </>
                ) : null}

                {error && (
                  <Alert tone="critical" title="操作失败">
                    {error}
                  </Alert>
                )}

                <div className="row" style={{ gap: 'var(--space-2)', marginTop: 'var(--space-4)' }}>
                  <Button
                    variant="success"
                    loading={state === 'approving'}
                    disabled={isBusy}
                    onClick={() => respond(run, 'approving')}
                    icon={<Icon name="check" size={14} />}
                  >
                    通过并执行
                  </Button>
                  <Button
                    variant="danger-ghost"
                    disabled={isBusy}
                    onClick={() => respond(run, 'rejecting')}
                    icon={<Icon name="x" size={14} />}
                  >
                    {state === 'rejecting' ? '拒绝中…' : '拒绝'}
                  </Button>
                  <Link to={`/incidents/${run.incident_id}`} style={{ marginLeft: 'auto' }}>
                    <Button variant="ghost" size="sm" iconRight={<Icon name="arrow-right" size={13} />}>
                      查看详情
                    </Button>
                  </Link>
                </div>
              </Panel>
            )
          })}
        </div>
      )}
    </>
  )
}
