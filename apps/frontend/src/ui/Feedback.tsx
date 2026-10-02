import clsx from 'clsx'
import type { ReactNode } from 'react'

export function Skeleton({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return <div className={clsx('skeleton', className)} style={style} aria-hidden />
}

export function SkeletonText({ lines = 3 }: { lines?: number }) {
  return (
    <div>
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className="skeleton skeleton-text"
          style={{ width: i === lines - 1 ? '62%' : '100%' }}
        />
      ))}
    </div>
  )
}

export function Spinner({ size = 'md' }: { size?: 'md' | 'lg' }) {
  return <div className={clsx('spinner', size === 'lg' && 'spinner-lg')} role="status" aria-label="加载中" />
}

export function LoadingBlock({ label = '加载中…' }: { label?: string }) {
  return (
    <div className="loading-block">
      <Spinner />
      <span>{label}</span>
    </div>
  )
}

export function EmptyState({
  icon,
  title,
  hint,
  action,
  compact,
  tone,
}: {
  icon?: ReactNode
  title: ReactNode
  hint?: ReactNode
  action?: ReactNode
  compact?: boolean
  tone?: 'critical'
}) {
  return (
    <div className={clsx('empty-state', compact && 'empty-state-compact', tone && `empty-state-${tone}`)}>
      {icon != null && <div className="empty-state-icon">{icon}</div>}
      <div className="empty-state-title">{title}</div>
      {hint != null && <div className="empty-state-hint">{hint}</div>}
      {action != null && <div style={{ marginTop: 8 }}>{action}</div>}
    </div>
  )
}

export type AlertTone = 'info' | 'success' | 'warning' | 'critical'

export function Alert({
  tone = 'info',
  title,
  children,
  action,
}: {
  tone?: AlertTone
  title?: ReactNode
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className={clsx('alert', `alert-${tone}`)} role={tone === 'critical' ? 'alert' : 'status'}>
      <div style={{ flex: 1, minWidth: 0 }}>
        {title != null && <div className="alert-title">{title}</div>}
        {children != null && <div className="alert-body">{children}</div>}
      </div>
      {action}
    </div>
  )
}

export function ConfidenceMeter({
  value,
  label = '置信度',
  tone = 'primary',
  hint,
}: {
  value: number
  label?: string
  tone?: 'primary' | 'agent' | 'success' | 'warning' | 'critical'
  hint?: string
}) {
  const pct = Math.max(0, Math.min(1, value)) * 100
  return (
    <div className="confidence-meter">
      <div className="meter-bar">
        <div
          className={clsx('meter-fill', tone !== 'primary' && `meter-fill-${tone}`)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="meter-label">
        <span>{label}</span>
        <span className="meter-value">{pct.toFixed(1)}%</span>
      </div>
      {/* A bare percentage is not interpretable on its own — 97% of what?
          Opt-in so the approval queue, which reuses this component for a
          narrower decision, does not inherit a paragraph it does not need. */}
      {hint != null && <div className="meter-hint">{hint}</div>}
    </div>
  )
}

export function Progress({ value, tone }: { value: number; tone?: 'success' | 'critical' }) {
  const pct = Math.max(0, Math.min(1, value)) * 100
  return (
    <div className="progress" role="progressbar" aria-valuenow={Math.round(pct)}>
      <div
        className="progress-fill"
        style={{
          width: `${pct}%`,
          background: tone === 'success' ? 'var(--success)' : tone === 'critical' ? 'var(--critical)' : undefined,
        }}
      />
    </div>
  )
}
