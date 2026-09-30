import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, EmptyState, LoadingBlock } from '../ui/Feedback'
import { LiveDot, StatusBadge } from '../ui/Badge'
import { Icon } from '../ui/Icon'
import { MetricCard, MetricStrip } from '../components/MetricCard'
import { Dropdown } from '../ui/Dropdown'
import { useAgentRuns, useAgentStats, isRunLive } from '../lib/queries'
import { formatDuration, formatPercent, timeAgo, elapsedSince, shortId } from '../lib/format'
import { stageLabel, stageProgress } from '../lib/labels'
import { zhRunStatus, zhCategory } from '../i18n'
import type { AgentRunStatus } from '../types'

type RunFilter = AgentRunStatus | 'all'

/**
 * Every status the wire actually defines — `_STATUS_MAP` server-side.
 *
 * `recovering` used to be in this list. It is not a run status: recovery has
 * its own plan status and is visible through `current_node`, so filtering on
 * it would have matched zero rows forever while looking like a real option.
 */
const FILTERS: RunFilter[] = [
  'all',
  'pending',
  'investigating',
  'awaiting_approval',
  'completed',
  'failed',
]

/**
 * AgentsPage — the agent operations roster.
 *
 * Deliberately about the *agent* rather than the incident: run throughput,
 * success rate, recovery rate, and per-run latency. This is the page that shows
 * the orchestration layer working as a system, not one investigation at a time.
 */
export function AgentsPage() {
  const statsQ = useAgentStats()
  const runsQ = useAgentRuns()
  const [filter, setFilter] = useState<RunFilter>('all')

  const runs = useMemo(() => {
    const items = runsQ.data ?? []
    return filter === 'all' ? items : items.filter((r) => r.status === filter)
  }, [runsQ.data, filter])

  const stats = statsQ.data
  const active = (runsQ.data ?? []).filter((r) => isRunLive(r.status)).length

  return (
    <>
      <PageHeader
        title="Agent 运行"
        description="编排层的运行视图：吞吐、成功率、恢复验证率与逐次运行的状态。"
        actions={
          <>
            <LiveDot state={active > 0 ? 'live' : 'closed'} label={active > 0 ? `${active} 个运行中` : '空闲'} />
            <Button
              icon={<Icon name="refresh" size={14} />}
              loading={runsQ.isFetching || statsQ.isFetching}
              onClick={() => {
                runsQ.refetch()
                statsQ.refetch()
              }}
            >
              刷新
            </Button>
          </>
        }
      />

      {statsQ.error && (
        <Alert tone="critical" title="加载统计失败">
          {statsQ.error instanceof Error ? statsQ.error.message : String(statsQ.error)}
        </Alert>
      )}

      <div className="metric-grid">
        <MetricCard
          label="累计运行"
          value={stats?.total_runs ?? 0}
          tone="agent"
          icon={<Icon name="cpu" size={13} />}
        />
        <MetricCard
          label="进行中"
          value={stats?.in_progress ?? 0}
          tone={active > 0 ? 'primary' : 'default'}
          sub={`${stats?.awaiting_approval ?? 0} 个等待审批`}
        />
        <MetricCard
          label="成功率"
          value={stats?.has_data ? formatPercent(stats.success_rate, 0) : '—'}
          tone="success"
          sub={`${stats?.completed ?? 0} 成功 / ${stats?.failed ?? 0} 失败`}
        />
        <MetricCard
          label="恢复验证率"
          value={stats?.recovery_verified ? formatPercent(stats.recovery_rate, 0) : '—'}
          tone="primary"
          sub={`${stats?.recovery_verified ?? 0} 次通过验证`}
        />
      </div>

      <Panel
        flush
        title="运行记录"
        subtitle={`${runs.length} 条`}
        actions={
          <Dropdown
            trigger={({ toggle, open }) => (
              <button className="filter-chip active" onClick={toggle} data-open={open}>
                {filter === 'all' ? '全部状态' : zhRunStatus(filter)}
                <Icon name="chevron-down" size={12} />
              </button>
            )}
          >
            {({ close }) => (
              <>
                {FILTERS.map((f) => (
                  <button
                    key={f}
                    className={`dropdown-item${filter === f ? ' dropdown-item-active' : ''}`}
                    onClick={() => {
                      setFilter(f)
                      close()
                    }}
                  >
                    {f === 'all' ? '全部状态' : zhRunStatus(f)}
                  </button>
                ))}
              </>
            )}
          </Dropdown>
        }
      >
        {runsQ.isLoading ? (
          <LoadingBlock label="加载运行记录…" />
        ) : runs.length === 0 ? (
          <EmptyState
            icon={<Icon name="cpu" size={20} />}
            title={filter === 'all' ? '暂无 Agent 运行' : '没有匹配的运行'}
            hint="在故障详情页点击「AI 调查」即可创建一次运行。"
            action={
              <Link to="/incidents">
                <Button size="sm" variant="agent">前往故障列表</Button>
              </Link>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th style={{ width: 96 }}>运行</th>
                  <th style={{ width: 100 }}>故障</th>
                  <th style={{ width: 150 }}>状态</th>
                  <th>根因</th>
                  <th style={{ width: 88 }}>置信度</th>
                  <th style={{ width: 96 }}>耗时</th>
                  <th style={{ width: 92 }}>更新</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const live = isRunLive(run.status)
                  // Prefer the measured wall clock the run itself recorded;
                  // fall back to time-since-start only while it is still going,
                  // and to "—" once it is over and the measurement is missing.
                  const duration = run.usage?.duration_ms ?? null
                  const durationLabel =
                    duration != null
                      ? formatDuration(duration / 1000)
                      : live
                        ? elapsedSince(run.started_at ?? run.created_at)
                        : '—'
                  return (
                    <tr key={run.id}>
                      <td className="col-mono">
                        <Link to={`/incidents/${run.incident_id}`} className="link">
                          {shortId(run.id, 8)}
                        </Link>
                      </td>
                      <td className="col-mono muted">{shortId(run.incident_id, 8)}</td>
                      <td>
                        <StatusBadge status={run.status} label={zhRunStatus(run.status)} size="sm" />
                        {live && run.current_node && (
                          <div className="muted" style={{ fontSize: 'var(--text-xs)', marginTop: 2 }}>
                            {stageLabel(run.current_node)}
                            {stageProgress(run.current_node) && (
                              <span className="mono"> · {stageProgress(run.current_node)}</span>
                            )}
                          </div>
                        )}
                      </td>
                      <td className="truncate" style={{ maxWidth: 280 }}>
                        {run.root_cause?.root_cause ? (
                          <>
                            {run.root_cause.root_cause}
                            <span className="muted" style={{ marginLeft: 6, fontSize: 'var(--text-xs)' }}>
                              {zhCategory(run.root_cause.category)}
                            </span>
                          </>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td className="col-mono">
                        {run.confidence ? formatPercent(run.confidence, 0) : '—'}
                      </td>
                      <td className="col-mono muted">{durationLabel}</td>
                      <td className="muted" style={{ fontSize: 'var(--text-sm)' }}>
                        {timeAgo(run.updated_at)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {stats && (
        <Panel title="恢复漏斗" subtitle="一次运行从诊断到验证通过的收敛情况">
          <MetricStrip
            items={[
              { label: '尝试恢复', value: stats.recovery_attempted },
              { label: '通过验证', value: stats.recovery_verified, tone: 'success' },
              { label: '等待审批', value: stats.awaiting_approval, tone: 'warning' },
              { label: '失败', value: stats.failed, tone: 'critical' },
            ]}
          />
        </Panel>
      )}
    </>
  )
}
