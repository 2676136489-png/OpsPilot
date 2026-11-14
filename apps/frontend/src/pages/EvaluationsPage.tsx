import { useMemo, useState, type ReactNode } from 'react'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, EmptyState, Progress } from '../ui/Feedback'
import { Icon } from '../ui/Icon'
import { MetricCard } from '../components/MetricCard'
import { useAgentStats } from '../lib/queries'
import { formatNumber, formatPercent } from '../lib/format'
import { outcomeLabel, outcomeTone } from '../lib/labels'
import { zhCategory } from '../i18n'
import report from '../lib/eval_report.json'

/**
 * The offline benchmark report, as produced by `evals/cli.py`.
 *
 * The page is a view of a generated artefact, not a hand-maintained snapshot.
 * Offline scores and live counters are kept in separate tabs because they
 * answer different questions and must never be mixed into one number.
 */
interface EvalReport {
  generated_at: string
  report: {
    total: number
    errors: number
    root_cause_accuracy: number
    evidence_accuracy: { recall: number; utilisation: number; traceability: number }
    tool_selection_accuracy: number
    investigation: {
      avg_steps: number
      avg_distinct_stages: number
      avg_replan_rounds: number
      runs_that_replanned: number
      avg_rejected_hypotheses: number
    }
    recovery: {
      success_rate: number
      environment_fixed_rate: number
      effective_action_rate: number
      rollback_rate: number
    }
    verification: {
      accuracy: number
      scored_runs: number
      reported_success_env_broken: number
    }
    diagnosis_honesty: { false_diagnosis_rate: number; answered: number; abstained: number }
    escalation: { rate: number; reasons: Record<string, number> }
    cost: {
      total_tool_calls: number
      avg_tool_calls: number
      failed_tool_calls: number
      failed_tool_call_rate: number
      total_tokens: number
      avg_tokens: number
      reasoning_modes: Record<string, number>
      budget_exhausted_runs: number
    }
    latency_ms: { avg: number; p50: number; p95: number; max: number; avg_workflow: number }
    trace_integrity: {
      single_root: number
      with_dangling_parents: number
      avg_spans: number
      avg_depth: number
    }
    breakdowns: {
      category: Record<string, number>
      scenario: Record<string, ScenarioScore>
      difficulty: Record<string, number>
      severity: Record<string, number>
    }
    notes: { suite: string; version: string; scoring: Record<string, string> }
  }
}

interface ScenarioScore {
  correct: boolean
  diagnosed: string
  expected: string
  outcome: string
  tools: number
  recall: number
  env_fixed: boolean
  escalated: boolean
  latency_ms: number
}

const REPORT = (report as EvalReport).report
const GENERATED_AT = (report as EvalReport).generated_at

export function EvaluationsPage() {
  const statsQ = useAgentStats()
  const [tab, setTab] = useState<'offline' | 'live'>('offline')

  const scenarios = useMemo(
    () => Object.entries(REPORT.breakdowns.scenario).sort(([a], [b]) => a.localeCompare(b)),
    [],
  )
  const categories = useMemo(() => Object.entries(REPORT.breakdowns.category), [])
  const difficulties = useMemo(() => Object.entries(REPORT.breakdowns.difficulty), [])
  const severities = useMemo(() => Object.entries(REPORT.breakdowns.severity), [])
  const scoringDefs = useMemo(() => Object.entries(REPORT.notes.scoring), [])

  const pass = REPORT.root_cause_accuracy >= 0.8

  return (
    <>
      <PageHeader
        title="评估报告"
        description="离线基准与线上运行指标的对照，用于校验诊断准确性、恢复有效性与执行成本。"
        actions={
          <div className="tabs">
            <button
              className={`tab${tab === 'offline' ? ' tab-active' : ''}`}
              onClick={() => setTab('offline')}
            >
              离线基准
            </button>
            <button
              className={`tab${tab === 'live' ? ' tab-active' : ''}`}
              onClick={() => setTab('live')}
            >
              线上指标
            </button>
          </div>
        }
      />

      {tab === 'offline' ? (
        <>
          <Alert tone={pass ? 'success' : 'warning'} title={`离线基准 · ${REPORT.total} 个标准场景`}>
            <span className="mono">
              确定性执行，不调用模型，不访问外部网络。报告生成于 {GENERATED_AT}，由{' '}
              <code>python evals/cli.py</code> 产出。
              {REPORT.errors > 0 && ` 本次有 ${REPORT.errors} 个场景执行失败。`}
            </span>
          </Alert>

          <div className="metric-grid">
            <MetricCard
              label="根因准确率"
              value={formatPercent(REPORT.root_cause_accuracy, 0)}
              tone={pass ? 'success' : 'warning'}
              sub={`${REPORT.total} 个标准场景`}
            />
            <MetricCard
              label="工具覆盖"
              value={formatPercent(REPORT.tool_selection_accuracy, 0)}
              tone="agent"
              sub="所需工具类型全部调用"
            />
            <MetricCard
              label="证据召回率"
              value={formatPercent(REPORT.evidence_accuracy.recall, 0)}
              tone="primary"
              sub={`证据引用率 ${formatPercent(REPORT.evidence_accuracy.utilisation, 0)}`}
            />
            <MetricCard
              label="平均耗时"
              value={formatNumber(REPORT.latency_ms.avg, 0)}
              unit="ms"
              sub={`${REPORT.investigation.avg_steps.toFixed(1)} 步 / 次`}
            />
          </div>

          <Panel title="质量指标" subtitle="证据、调查、诊断、恢复、成本与追踪的基准结果" flush>
            <div className="eval-overview">
              <EvalBlock title="证据质量" description="采集覆盖、引用率与可追溯性">
                <BreakdownRow label="召回率" score={REPORT.evidence_accuracy.recall} />
                <BreakdownRow label="引用率" score={REPORT.evidence_accuracy.utilisation} />
                <BreakdownRow
                  label="可追溯至假设"
                  score={REPORT.evidence_accuracy.traceability}
                />
              </EvalBlock>

              <EvalBlock title="调查过程" description="调查步数、阶段覆盖与重规划次数">
                <StatRow label="平均调查步数" value={REPORT.investigation.avg_steps.toFixed(1)} />
                <StatRow
                  label="平均覆盖节点"
                  value={REPORT.investigation.avg_distinct_stages.toFixed(1)}
                />
                <StatRow
                  label="平均重规划轮次"
                  value={REPORT.investigation.avg_replan_rounds.toFixed(1)}
                />
                <StatRow
                  label="发生重规划的运行"
                  value={`${REPORT.investigation.runs_that_replanned} / ${REPORT.total}`}
                />
                <StatRow
                  label="平均被否决假设"
                  value={REPORT.investigation.avg_rejected_hypotheses.toFixed(1)}
                />
              </EvalBlock>

              <EvalBlock title="诊断可靠性" description="证据不足时的结论策略与升级情况">
                <BreakdownRow
                  label="结论准确率"
                  score={1 - REPORT.diagnosis_honesty.false_diagnosis_rate}
                />
                <StatRow label="给出结论" value={String(REPORT.diagnosis_honesty.answered)} />
                <StatRow label="未给出结论" value={String(REPORT.diagnosis_honesty.abstained)} />
                <StatRow
                  label="升级人工"
                  value={
                    REPORT.escalation.rate > 0
                      ? formatPercent(REPORT.escalation.rate, 0)
                      : '未触发'
                  }
                />
              </EvalBlock>

              <EvalBlock title="恢复与验证" description="恢复成功率、环境变更与验证一致性">
                <BreakdownRow label="恢复成功率" score={REPORT.recovery.success_rate} />
                <BreakdownRow label="环境实际修复率" score={REPORT.recovery.environment_fixed_rate} />
                <BreakdownRow label="动作产生环境变更" score={REPORT.recovery.effective_action_rate} />
                <BreakdownRow label="回滚率" score={REPORT.recovery.rollback_rate} />
                <StatRow
                  label="验证结论与环境一致"
                  value={`${formatPercent(REPORT.verification.accuracy, 0)} · ${REPORT.verification.scored_runs} 次`}
                />
                <StatRow
                  label="声明成功但环境未恢复"
                  value={`${REPORT.verification.reported_success_env_broken} 次`}
                />
              </EvalBlock>

              <EvalBlock title="执行成本" description="工具调用、Token 消耗与预算中断">
                <StatRow
                  label="工具调用总数"
                  value={`${REPORT.cost.total_tool_calls} 次 · 平均 ${REPORT.cost.avg_tool_calls.toFixed(1)} 次/运行`}
                />
                <StatRow
                  label="工具失败率"
                  value={`${formatPercent(REPORT.cost.failed_tool_call_rate, 1)} · ${REPORT.cost.failed_tool_calls} 次`}
                />
                <StatRow
                  label="Token 消耗"
                  value={
                    REPORT.cost.total_tokens > 0
                      ? `${REPORT.cost.total_tokens} · 平均 ${REPORT.cost.avg_tokens.toFixed(0)}/运行`
                      : '0（确定性执行）'
                  }
                />
                <StatRow
                  label="预算耗尽中断"
                  value={`${REPORT.cost.budget_exhausted_runs} 次`}
                />
              </EvalBlock>

              <EvalBlock title="追踪完整性" description="调用链结构与跨度完整性">
                <StatRow
                  label="单根调用链"
                  value={`${REPORT.trace_integrity.single_root} / ${REPORT.total}`}
                />
                <StatRow
                  label="悬空父跨度"
                  value={
                    REPORT.trace_integrity.with_dangling_parents === 0
                      ? '无'
                      : String(REPORT.trace_integrity.with_dangling_parents)
                  }
                />
                <StatRow
                  label="平均跨度数"
                  value={REPORT.trace_integrity.avg_spans.toFixed(1)}
                />
                <StatRow
                  label="平均调用深度"
                  value={REPORT.trace_integrity.avg_depth.toFixed(1)}
                />
              </EvalBlock>
            </div>
          </Panel>

          <Panel title="分类表现" subtitle="按根因类别、难度与严重度分组" flush>
            <div className="eval-breakdowns">
              <EvalBlock title="按根因类别" description="各类别的根因命中率">
                {categories.map(([name, score]) => (
                  <BreakdownRow key={name} label={zhCategory(name)} score={score} />
                ))}
              </EvalBlock>
              <EvalBlock title="按难度" description="不同难度场景的通过率">
                {difficulties.length === 0 ? (
                  <EmptyState compact title="暂无难度标注" />
                ) : (
                  difficulties.map(([name, score]) => (
                    <BreakdownRow key={name} label={difficultyLabel(name)} score={score} />
                  ))
                )}
              </EvalBlock>
              <EvalBlock title="按严重度" description="不同事件级别的通过率">
                {severities.length === 0 ? (
                  <EmptyState compact title="暂无严重度标注" />
                ) : (
                  severities.map(([name, score]) => (
                    <BreakdownRow key={name} label={name} score={score} />
                  ))
                )}
              </EvalBlock>
            </div>
          </Panel>

          <Panel
            title="场景明细"
            subtitle={`${scenarios.length} 个场景 · 与答案键逐项比对`}
            flush
          >
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>场景</th>
                    <th style={{ width: 118 }}>期望 / 实际</th>
                    <th style={{ width: 172 }}>结论</th>
                    <th style={{ width: 108 }}>证据召回</th>
                    <th style={{ width: 76 }}>工具</th>
                    <th style={{ width: 76 }}>环境</th>
                    <th style={{ width: 84 }}>耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {scenarios.map(([name, s]) => (
                    <tr key={name}>
                      <td className="col-mono">{name}</td>
                      <td className="col-mono">
                        <span className={s.correct ? '' : 'hl-crit'}>
                          {zhCategory(s.diagnosed)}
                        </span>
                        <span className="muted"> / {zhCategory(s.expected)}</span>
                      </td>
                      <td>
                        <span className={`badge badge-sm badge-${outcomeTone(s.outcome)}`}>
                          {outcomeLabel(s.outcome)}
                        </span>
                        {s.escalated && <span className="badge badge-sm badge-warning">已升级</span>}
                      </td>
                      <td className="col-mono">{formatPercent(s.recall, 0)}</td>
                      <td className="col-mono">{s.tools}</td>
                      <td className={s.env_fixed ? '' : 'hl-crit'}>
                        {s.env_fixed ? '已修复' : '未修复'}
                      </td>
                      <td className="col-mono muted">{formatNumber(s.latency_ms, 0)}ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel title="指标口径" subtitle="各指标的计算方式与取值范围">
            <div className="breakdown-list">
              {scoringDefs.map(([key, desc]) => (
                <div className="def-row" key={key}>
                  <span className="def-key mono">{key}</span>
                  <span className="def-desc">{desc}</span>
                </div>
              ))}
            </div>
          </Panel>
        </>
      ) : (
        <>
          <Alert tone="info" title="线上指标 · 来自 Agent 运行记录">
            数据源为后端 <code className="mono">/api/v1/agent/stats</code>，随运行状态更新。
          </Alert>

          {statsQ.error ? (
            <Alert tone="critical" title="加载线上指标失败">
              {statsQ.error instanceof Error ? statsQ.error.message : String(statsQ.error)}
            </Alert>
          ) : (
            <div className="metric-grid">
              <MetricCard label="累计运行" value={statsQ.data?.total_runs ?? 0} tone="agent" />
              <MetricCard
                label="已完成"
                value={statsQ.data?.completed ?? 0}
                tone="success"
                sub={`${statsQ.data?.failed ?? 0} 次失败`}
              />
              <MetricCard
                label="成功率"
                value={statsQ.data?.has_data ? formatPercent(statsQ.data.success_rate, 0) : '—'}
              />
              <MetricCard
                label="恢复验证率"
                value={
                  statsQ.data?.recovery_verified
                    ? formatPercent(statsQ.data.recovery_rate, 0)
                    : '—'
                }
                sub={`${statsQ.data?.recovery_attempted ?? 0} 次尝试`}
              />
            </div>
          )}

          <Panel title="运行分布" subtitle="当前运行状态占比">
            <div className="breakdown-list">
              <BreakdownRow
                label="调查中"
                score={
                  statsQ.data?.total_runs
                    ? (statsQ.data.in_progress ?? 0) / statsQ.data.total_runs
                    : 0
                }
              />
              <BreakdownRow
                label="等待审批"
                score={
                  statsQ.data?.total_runs
                    ? (statsQ.data.awaiting_approval ?? 0) / statsQ.data.total_runs
                    : 0
                }
              />
              <BreakdownRow
                label="已恢复并验证"
                score={
                  statsQ.data?.total_runs
                    ? (statsQ.data.recovery_verified ?? 0) / statsQ.data.total_runs
                    : 0
                }
              />
            </div>
          </Panel>

          <div className="row" style={{ marginTop: 'var(--space-4)' }}>
            <Button
              icon={<Icon name="refresh" size={14} />}
              loading={statsQ.isFetching}
              onClick={() => statsQ.refetch()}
            >
              刷新数据
            </Button>
          </div>
        </>
      )}
    </>
  )
}

/** A section inside the report panel. The divider, not a nested card, marks it. */
function EvalBlock({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: ReactNode
}) {
  return (
    <section className="eval-block">
      <header className="eval-block-head">
        <h3 className="eval-block-title">{title}</h3>
        <p className="eval-block-description">{description}</p>
      </header>
      <div className="breakdown-list">{children}</div>
    </section>
  )
}

function BreakdownRow({ label, score }: { label: ReactNode; score: number }) {
  return (
    <div className="breakdown-row">
      <span className="breakdown-label">{label}</span>
      <span className="breakdown-bar">
        <Progress value={score} tone={score >= 1 ? 'success' : undefined} />
      </span>
      <span className="breakdown-score mono">{formatPercent(score, 0)}</span>
    </div>
  )
}

/** A plain label/value line for counts and durations. */
function StatRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat-row">
      <span className="stat-label">{label}</span>
      <span className="stat-value mono">{value}</span>
    </div>
  )
}

function difficultyLabel(name: string): string {
  return name === 'easy' ? '简单' : name === 'medium' ? '中等' : name === 'hard' ? '困难' : name
}
