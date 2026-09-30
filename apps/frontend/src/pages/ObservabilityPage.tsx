import { useQuery } from '@tanstack/react-query'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, EmptyState, LoadingBlock } from '../ui/Feedback'
import { LiveDot, HealthDot } from '../ui/Badge'
import { Icon } from '../ui/Icon'
import { MetricCard } from '../components/MetricCard'
import { useHealth, queryKeys, useServices, useAgentStats } from '../lib/queries'
import { API_BASE } from '../api/client'
import { formatPercent } from '../lib/format'
import { zhHealth } from '../i18n'

/**
 * ObservabilityPage — the "is the platform healthy" view.
 *
 * Sources are all real: backend health probe, service health snapshot, agent
 * counters, and the raw Prometheus exposition text. Nothing here is mocked —
 * the page is the operator's answer to "is OpsPilot itself OK?".
 */
export function ObservabilityPage() {
  const healthQ = useHealth()
  const servicesQ = useServices()
  const statsQ = useAgentStats()

  const metricsQ = useQuery({
    queryKey: queryKeys.metrics,
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/metrics`)
      if (!res.ok) throw new Error(`metrics ${res.status}`)
      return res.text()
    },
    refetchInterval: 15_000,
    retry: false,
  })

  const parsed = parsePrometheus(metricsQ.data ?? '')
  // Counting `!== 'healthy'` treated "no verdict recorded" as an outage. Only
  // services that actually reported a bad state are unhealthy.
  const unhealthy = (servicesQ.data ?? []).filter(
    (s) => s.health === 'down' || s.health === 'degraded',
  )

  return (
    <>
      <PageHeader
        title="可观测性"
        description="OpsPilot 自身与所管理服务的健康状态，以及 Prometheus 格式的原始指标。"
        actions={
          <>
            <LiveDot
              state={healthQ.data?.status === 'ok' ? 'live' : healthQ.isError ? 'error' : 'connecting'}
              label={healthQ.data?.status === 'ok' ? '后端在线' : healthQ.isError ? '后端离线' : '探测中'}
            />
            <Button
              icon={<Icon name="refresh" size={14} />}
              loading={metricsQ.isFetching}
              onClick={() => {
                healthQ.refetch()
                servicesQ.refetch()
                statsQ.refetch()
                metricsQ.refetch()
              }}
            >
              刷新
            </Button>
          </>
        }
      />

      {healthQ.isError && (
        <Alert tone="critical" title="无法连接后端">
          健康检查失败。确认后端服务已启动并监听在预期的端口上。
        </Alert>
      )}

      <div className="metric-grid">
        <MetricCard
          label="后端状态"
          value={healthQ.data?.status ?? '—'}
          tone={healthQ.data?.status === 'ok' ? 'success' : 'critical'}
          sub={healthQ.data?.version ? `版本 ${healthQ.data.version}` : undefined}
        />
        <MetricCard
          label="服务总数"
          value={servicesQ.data?.length ?? 0}
          sub={unhealthy.length > 0 ? `${unhealthy.length} 个异常` : '全部正常'}
          tone={unhealthy.length > 0 ? 'warning' : 'success'}
        />
        <MetricCard label="Agent 运行" value={statsQ.data?.total_runs ?? 0} tone="agent" />
        <MetricCard
          label="恢复验证率"
          value={
            statsQ.data?.recovery_verified ? formatPercent(statsQ.data.recovery_rate, 0) : '—'
          }
          tone="primary"
        />
      </div>

      <div className="eval-grid">
        <Panel title="服务健康快照" flush>
          {(servicesQ.data ?? []).length === 0 ? (
            <EmptyState compact title="暂无服务数据" />
          ) : (
            <div className="health-list">
              {servicesQ.data!.map((svc) => {
                const health = svc.health ?? 'unknown'
                return (
                  <div key={svc.name} className="health-row">
                    <HealthDot health={health} label={zhHealth(health)} />
                    <span className="mono health-name">{svc.name}</span>
                    <span className="mono muted">{formatPercent(svc.error_rate, 2)}</span>
                    <span className="mono muted">
                      {svc.latency_p95 != null ? `${svc.latency_p95}ms` : '—'}
                    </span>
                    <span className="health-state">{zhHealth(health)}</span>
                  </div>
                )
              })}
            </div>
          )}
        </Panel>

        <Panel
          title="Prometheus 指标"
          subtitle={parsed.length > 0 ? `${parsed.length} 个指标` : undefined}
          flush
        >
          {metricsQ.isLoading ? (
            <LoadingBlock label="拉取 /metrics…" />
          ) : metricsQ.error ? (
            <div style={{ padding: 'var(--space-4)' }}>
              <Alert tone="warning" title="指标端点不可用">
                {metricsQ.error instanceof Error ? metricsQ.error.message : String(metricsQ.error)}
              </Alert>
            </div>
          ) : parsed.length === 0 ? (
            <EmptyState compact title="暂无指标" hint="指标端点已响应，但未暴露任何样本。" />
          ) : (
            <div className="metrics-list">
              {parsed.map(([name, value]) => (
                <div key={name} className="metrics-row">
                  <span className="mono metrics-name">{name}</span>
                  <span className="mono metrics-value">{value}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </>
  )
}

/**
 * Minimal Prometheus text parser.
 *
 * Deliberately not a full exposition-format implementation: we only need the
 * simple `name value` samples for display, so we skip `#` comment lines and
 * drop label sets rather than pretending to model them.
 */
function parsePrometheus(text: string): [string, string][] {
  const out: [string, string][] = []
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const idx = line.lastIndexOf(' ')
    if (idx <= 0) continue
    const name = line.slice(0, idx).trim()
    const value = line.slice(idx + 1).trim()
    if (name && value) out.push([name, value])
  }
  return out
}
