import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { Panel, PageHeader } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, EmptyState, LoadingBlock, SkeletonText } from '../ui/Feedback'
import { Icon } from '../ui/Icon'
import { Dropdown } from '../ui/Dropdown'
import { useAgentStats, useHealth, useIncidents, useScenarios, useServices } from '../lib/queries'
import { formatPercent } from '../lib/format'
import { useToast } from '../ui/Toast'
import { MetricCard } from '../components/MetricCard'
import { ServiceGrid } from '../components/ServiceGrid'
import { IncidentTable } from '../components/IncidentTable'
import { ActivityFeed } from '../components/ActivityFeed'

export function CommandCenterPage() {
  const navigate = useNavigate()
  const toast = useToast()

  const health = useHealth()
  const services = useServices()
  const incidents = useIncidents({ limit: 50 })
  const stats = useAgentStats()
  const scenarios = useScenarios()

  const serviceList = services.data ?? []
  const incidentList = incidents.data?.items ?? []

  // Derive directly rather than through `useMemo`: `?? []` allocates a new
  // array on every render, so a memo keyed on it would recompute every time and
  // only add bookkeeping. The filters below are cheap and the lists are short.
  const activeIncidents = incidentList.filter((i) =>
    ['open', 'investigating'].includes(i.status),
  )
  const criticalCount = activeIncidents.filter((i) => i.severity === 'critical').length
  const unhealthy = serviceList.filter((s) => s.health === 'degraded' || s.health === 'down')

  const inject = async (name: string) => {
    try {
      await api.simulator.injectScenario(name)
      toast.success('故障已注入', `${name} 场景已激活`)
      await Promise.all([incidents.refetch(), services.refetch()])
    } catch (e) {
      toast.error('注入失败', e instanceof Error ? e.message : String(e))
    }
  }

  const connected = !health.isError && health.data != null

  return (
    <div className="stack" style={{ gap: 'var(--space-5)' }}>
      <PageHeader
        title="指挥中心"
        description="实时掌握生产环境状态、活动故障与 AI Agent 调查进展。"
        actions={
          <>
            <Button
              variant="default"
              icon={<Icon name="refresh" size={14} />}
              onClick={() => {
                void services.refetch()
                void incidents.refetch()
                void stats.refetch()
              }}
            >
              刷新
            </Button>

            <Dropdown
              menuWidth={300}
              trigger={({ toggle }) => (
                <Button variant="primary" icon={<Icon name="lightning" size={14} />} onClick={toggle}>
                  注入故障
                </Button>
              )}
            >
              {({ close }) => (
                <>
                  <div className="dropdown-label">模拟场景</div>
                  {(scenarios.data ?? []).map((s) => (
                    <button
                      key={s.name}
                      className="dropdown-item"
                      onClick={() => {
                        close()
                        void inject(s.name)
                      }}
                    >
                      <span className="dropdown-item-title mono" style={{ fontSize: 'var(--text-sm)' }}>
                        {s.name}
                      </span>
                      <span className="dropdown-item-hint">{s.description}</span>
                    </button>
                  ))}
                  {scenarios.data?.length === 0 && (
                    <div className="dropdown-item-hint" style={{ padding: 'var(--space-2) var(--space-3)' }}>
                      模拟器未返回场景列表
                    </div>
                  )}
                </>
              )}
            </Dropdown>
          </>
        }
      />

      {!connected && (
        <Alert tone="critical" title="后端未连接">
          无法访问 API。请确认后端服务运行在 8000 端口。
        </Alert>
      )}

      <div className="metric-grid">
        <MetricCard
          label="活动故障"
          value={activeIncidents.length}
          tone={activeIncidents.length > 0 ? 'critical' : 'success'}
          sub={criticalCount > 0 ? `${criticalCount} 个紧急` : '无紧急故障'}
        />
        <MetricCard
          label="异常服务"
          value={unhealthy.length}
          tone={unhealthy.length > 0 ? 'warning' : 'success'}
          sub={`共 ${serviceList.length} 个服务`}
        />
        <MetricCard
          label="Agent 成功率"
          value={formatPercent(stats.data?.success_rate)}
          tone="agent"
          sub={
            stats.data && stats.data.completed + stats.data.failed > 0
              ? `${stats.data.completed}/${stats.data.completed + stats.data.failed} 已得出结论`
              : '暂无已完成调查'
          }
        />
        <MetricCard
          label="待审批"
          value={stats.data?.awaiting_approval ?? 0}
          tone={(stats.data?.awaiting_approval ?? 0) > 0 ? 'warning' : 'success'}
          sub={(stats.data?.awaiting_approval ?? 0) > 0 ? '需要人工确认' : '审批队列为空'}
        />
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1.35fr) minmax(0, 1fr)',
          gap: 'var(--space-4)',
          alignItems: 'start',
        }}
      >
        <Panel
          title="活动故障"
          subtitle={activeIncidents.length > 0 ? `${activeIncidents.length} 个未关闭` : undefined}
          actions={
            <Button size="sm" variant="ghost" onClick={() => navigate('/incidents')}>
              查看全部 <Icon name="arrow-right" size={13} />
            </Button>
          }
          flush
        >
          {incidents.isLoading && incidentList.length === 0 ? (
            <div style={{ padding: 'var(--space-4)' }}>
              <SkeletonText lines={4} />
            </div>
          ) : incidents.isError ? (
            <EmptyState
              tone="critical"
              title="加载故障失败"
              hint={incidents.error instanceof Error ? incidents.error.message : '未知错误'}
              compact
            />
          ) : activeIncidents.length === 0 ? (
            <EmptyState
              icon={<Icon name="check" size={18} />}
              title="暂无活动故障"
              hint="所有服务运行正常。可注入一个模拟场景来体验完整的 AI 调查流程。"
              compact
            />
          ) : (
            <IncidentTable
              incidents={activeIncidents.slice(0, 8)}
              onSelect={(inc) => navigate(`/incidents/${inc.id}`)}
            />
          )}
        </Panel>

        <Panel
          title="服务健康"
          subtitle={`${serviceList.length} 个服务`}
          actions={
            <Button size="sm" variant="ghost" onClick={() => navigate('/topology')}>
              拓扑 <Icon name="arrow-right" size={13} />
            </Button>
          }
        >
          {services.isLoading && serviceList.length === 0 ? (
            <LoadingBlock label="加载服务中…" />
          ) : services.isError ? (
            <EmptyState tone="critical" title="加载服务失败" compact />
          ) : serviceList.length === 0 ? (
            <EmptyState title="暂无服务注册" hint="注入故障场景后服务会自动注册。" compact />
          ) : (
            <ServiceGrid services={serviceList.slice(0, 6)} />
          )}
        </Panel>
      </div>

      <ActivityFeed />
    </div>
  )
}
