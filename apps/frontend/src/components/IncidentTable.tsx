import { useNavigate } from 'react-router-dom'
import type { Incident } from '../types'
import { Icon } from '../ui/Icon'
import { SeverityBadge, StatusBadge } from '../ui/Badge'
import { zhSeverity, zhStatus } from '../i18n'
import { timeAgo } from '../lib/format'
import { serviceLabel } from '../lib/labels'
import { api } from '../api/client'
import { useToast } from '../ui/Toast'

export function IncidentTable({
  incidents,
  onSelect,
  showService = true,
}: {
  incidents: Incident[]
  onSelect: (incident: Incident) => void
  showService?: boolean
}) {
  const navigate = useNavigate()
  const toast = useToast()

  const investigate = async (incident: Incident, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      const run = await api.agent.startInvestigation(incident.id, incident.scenario ?? undefined)
      toast.success('已启动 AI 调查', `运行 ${run.id.slice(0, 8)}`)
      navigate(`/incidents/${incident.id}`)
    } catch (err) {
      toast.error('启动调查失败', err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th style={{ width: 72 }}>ID</th>
            <th style={{ width: 84 }}>级别</th>
            {showService && <th style={{ width: 120 }}>服务</th>}
            <th>标题</th>
            <th style={{ width: 108 }}>状态</th>
            <th style={{ width: 96 }}>发生时间</th>
            <th style={{ width: 72 }} />
          </tr>
        </thead>
        <tbody>
          {incidents.map((inc) => (
            <tr key={inc.id} className="row-clickable" onClick={() => onSelect(inc)}>
              <td className="col-mono" style={{ color: 'var(--muted-foreground)' }}>
                {inc.id.slice(0, 6)}
              </td>
              <td>
                <SeverityBadge severity={inc.severity} label={zhSeverity(inc.severity)} size="sm" />
              </td>
              {showService && (
                <td className="col-mono" style={{ color: 'var(--foreground-secondary)' }}>
                  {serviceLabel(inc)}
                </td>
              )}
              <td style={{ color: 'var(--foreground)' }}>{inc.title}</td>
              <td>
                <StatusBadge status={inc.status} label={zhStatus(inc.status)} size="sm" />
              </td>
              <td style={{ color: 'var(--muted)', fontSize: 'var(--text-sm)' }}>
                {timeAgo(inc.created_at)}
              </td>
              <td className="col-actions">
                <button
                  className="btn btn-sm btn-agent"
                  onClick={(e) => investigate(inc, e)}
                  title="启动 AI 调查"
                  style={{ padding: '0 8px' }}
                >
                  <Icon name="cpu" size={13} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
