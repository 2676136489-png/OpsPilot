import clsx from 'clsx'
import { NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { HealthDot } from '../ui/Badge'
import { Icon, type IconName } from '../ui/Icon'

interface NavEntry {
  to: string
  label: string
  icon: IconName
  end?: boolean
  badge?: number
}

interface NavGroup {
  label: string
  items: NavEntry[]
}

export function Sidebar({
  collapsed,
  onToggle,
}: {
  collapsed: boolean
  onToggle: () => void
}) {
  // The sidebar is the one place that may safely poll — a supervisor glancing
  // at the rail must see pending approvals without opening the page.
  const { data: stats } = useQuery({
    queryKey: ['agent', 'stats'],
    queryFn: () => api.agent.stats(),
    refetchInterval: 15_000,
    retry: false,
  })

  const pending = stats?.awaiting_approval ?? 0

  const groups: NavGroup[] = [
    {
      label: '指挥',
      items: [
        { to: '/', label: '指挥中心', icon: 'grid', end: true },
        { to: '/incidents', label: '故障', icon: 'alert' },
        { to: '/topology', label: '服务拓扑', icon: 'topology' },
      ],
    },
    {
      label: 'AI 运维',
      items: [
        { to: '/agents', label: 'Agent 运行', icon: 'cpu' },
        { to: '/approvals', label: '审批队列', icon: 'check', badge: pending || undefined },
        { to: '/runbooks', label: '运维手册', icon: 'book' },
      ],
    },
    {
      label: '洞察',
      items: [
        { to: '/evaluations', label: '评估', icon: 'flask' },
        { to: '/observability', label: '可观测性', icon: 'activity' },
      ],
    },
  ]

  return (
    <aside className="sidebar" aria-label="主导航">
      <div className="sidebar-brand">
        <div className="brand-mark">OP</div>
        <div className="brand-text">
          <span className="brand-name">OpsPilot</span>
          <span className="brand-tag">故障响应指挥台</span>
        </div>
      </div>

      <nav className="sidebar-nav">
        {groups.map((group) => (
          <div key={group.label}>
            <div className="sidebar-group-label">{group.label}</div>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => clsx('nav-item', isActive && 'active')}
                title={collapsed ? item.label : undefined}
              >
                <Icon name={item.icon} className="nav-item-icon" />
                <span className="nav-item-label">{item.label}</span>
                {item.badge != null && <span className="nav-item-badge">{item.badge}</span>}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        <BackendStatus collapsed={collapsed} />
        <button
          className="conn-status"
          onClick={onToggle}
          style={{ cursor: 'pointer', border: 0, background: 'transparent', width: '100%' }}
          aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
        >
          <Icon name={collapsed ? 'chevron-right' : 'chevron-left'} className="nav-item-icon" />
          <span>{collapsed ? '展开' : '收起侧边栏'}</span>
        </button>
      </div>
    </aside>
  )
}

function BackendStatus({ collapsed }: { collapsed: boolean }) {
  const { data, isError } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.health.check(),
    refetchInterval: 20_000,
    retry: false,
  })

  const connected = !isError && data != null

  return (
    <div className="conn-status" title={connected ? '后端已连接' : '后端离线'}>
      <HealthDot health={connected ? 'healthy' : 'down'} size="sm" label={connected ? '已连接' : '离线'} />
      {!collapsed && <span>{connected ? '后端已连接' : '后端离线'}</span>}
    </div>
  )
}
