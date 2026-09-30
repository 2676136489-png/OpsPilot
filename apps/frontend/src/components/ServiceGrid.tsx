import type { Service } from '../types'
import { HealthDot } from '../ui/Badge'
import { zhHealth } from '../i18n'
import { formatPercent } from '../lib/format'

export function ServiceGrid({ services }: { services: Service[] }) {
  return (
    <div className="service-grid">
      {services.map((svc) => (
        <ServiceCard key={svc.name} service={svc} />
      ))}
    </div>
  )
}

export function ServiceCard({ service }: { service: Service }) {
  // `health` is nullable: rows sourced from the datastore carry no verdict of
  // their own. Rendering them as `healthy`/0.00% would invent a measurement,
  // so an absent verdict is shown as "未知" and an absent rate as "—".
  const health = service.health ?? 'unknown'
  const errorRate = service.error_rate

  return (
    <div className="service-card">
      <div className="service-card-head">
        <span className="service-name" title={service.name}>
          {service.name}
        </span>
        <HealthDot health={health} label={zhHealth(health)} />
      </div>
      <div className="service-metrics">
        <Metric
          label="错误率"
          value={formatPercent(errorRate, 2)}
          critical={errorRate != null && errorRate > 0.05}
        />
        <Metric label="P95" value={service.latency_p95 != null ? `${service.latency_p95}ms` : '—'} />
        <Metric label="CPU" value={service.cpu_usage != null ? `${service.cpu_usage.toFixed(0)}%` : '—'} />
        <Metric label="内存" value={service.memory_usage != null ? `${service.memory_usage.toFixed(0)}%` : '—'} />
      </div>
    </div>
  )
}

function Metric({ label, value, critical }: { label: string; value: string; critical?: boolean }) {
  return (
    <div className="service-metric">
      <span className="service-metric-label">{label}</span>
      <span
        className="service-metric-value"
        style={critical ? { color: 'var(--critical)' } : undefined}
      >
        {value}
      </span>
    </div>
  )
}
