import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, EmptyState, LoadingBlock } from '../ui/Feedback'
import { Icon } from '../ui/Icon'
import { IncidentTable } from '../components/IncidentTable'
import { useIncidents } from '../lib/queries'
import { severityZh, statusZh } from '../i18n'
import type { Incident, IncidentSeverity, IncidentStatus } from '../types'

type StatusFilter = IncidentStatus | 'all'
type SeverityFilter = IncidentSeverity | 'all'

const STATUSES: StatusFilter[] = ['all', 'open', 'investigating', 'mitigated', 'resolved', 'closed']
const SEVERITIES: SeverityFilter[] = ['all', 'critical', 'high', 'medium', 'low']

/**
 * IncidentsPage — the operational queue.
 *
 * Filters live in component state and are applied client-side on the fetched
 * page. When the incident volume outgrows a single page this swaps to
 * server-side query params without touching the table component.
 */
export function IncidentsPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<StatusFilter>('all')
  const [severity, setSeverity] = useState<SeverityFilter>('all')

  const query = useIncidents({ status: status === 'all' ? undefined : status, limit: 100 })

  const incidents = useMemo(() => {
    const items = query.data?.items ?? []
    return severity === 'all' ? items : items.filter((i) => i.severity === severity)
  }, [query.data, severity])

  return (
    <>
      <PageHeader
        title="故障"
        description="全部故障事件。点击任意一行查看详情，或直接对单条故障启动 AI 调查。"
        actions={
          <Button
            icon={<Icon name="refresh" size={14} />}
            loading={query.isFetching}
            onClick={() => query.refetch()}
          >
            刷新
          </Button>
        }
      />

      <Panel
        flush
        title="故障列表"
        subtitle={`${incidents.length} 条`}
        actions={
          <div className="filter-row">
            <div className="filter-group">
              <span className="filter-group-label">状态</span>
              {STATUSES.map((s) => (
                <button
                  key={s}
                  className={`filter-chip${status === s ? ' active' : ''}`}
                  onClick={() => setStatus(s)}
                >
                  {s === 'all' ? '全部' : statusZh[s]}
                </button>
              ))}
            </div>
            <div className="filter-group">
              <span className="filter-group-label">级别</span>
              {SEVERITIES.map((s) => (
                <button
                  key={s}
                  className={`filter-chip${severity === s ? ' active' : ''}`}
                  onClick={() => setSeverity(s)}
                >
                  {s === 'all' ? '全部' : severityZh[s]}
                </button>
              ))}
            </div>
          </div>
        }
      >
        {query.isLoading ? (
          <LoadingBlock label="加载故障…" />
        ) : query.error ? (
          <div style={{ padding: 'var(--space-4)' }}>
            <Alert
              tone="critical"
              title="加载失败"
              action={
                <Button size="sm" onClick={() => query.refetch()}>
                  重试
                </Button>
              }
            >
              {query.error instanceof Error ? query.error.message : String(query.error)}
            </Alert>
          </div>
        ) : incidents.length === 0 ? (
          <EmptyState
            icon={<Icon name="check" size={20} />}
            title="没有匹配的故障"
            hint="调整筛选条件，或在指挥中心注入一个模拟故障来生成事件。"
          />
        ) : (
          <IncidentTable
            incidents={incidents}
            onSelect={(inc: Incident) => navigate(`/incidents/${inc.id}`)}
          />
        )}
      </Panel>
    </>
  )
}








