import clsx from 'clsx'
import type { ReactNode } from 'react'
import type { IncidentSeverity, IncidentStatus, ServiceHealth } from '../types'

export type BadgeTone =
  | 'neutral'
  | 'success'
  | 'warning'
  | 'critical'
  | 'info'
  | 'agent'

export interface BadgeProps {
  tone?: BadgeTone
  size?: 'sm' | 'md' | 'lg'
  className?: string
  children: ReactNode
  title?: string
}

export function Badge({ tone = 'neutral', size = 'md', className, children, title }: BadgeProps) {
  return (
    <span
      className={clsx('badge', `badge-${tone}`, size !== 'md' && `badge-${size}`, className)}
      title={title}
    >
      {children}
    </span>
  )
}

/** Severity is the only place the red / orange ramps may be used. */
export function SeverityBadge({
  severity,
  label,
  size,
}: {
  severity: IncidentSeverity | string
  label: string
  size?: 'sm' | 'md' | 'lg'
}) {
  const key = String(severity).toLowerCase().replace(/^sev-?/, '')
  const tone =
    key === '1' || key === 'critical'
      ? 'critical'
      : key === '2' || key === 'high'
        ? 'high'
        : key === '3' || key === 'medium'
          ? 'medium'
          : 'low'
  return (
    <span className={clsx('badge', `badge-sev-${tone}`, size && `badge-${size}`)}>{label}</span>
  )
}

export function StatusBadge({
  status,
  label,
  size,
}: {
  status: IncidentStatus | string
  label: string
  size?: 'sm' | 'md' | 'lg'
}) {
  const slug = String(status).toLowerCase().replace(/_/g, '-')
  return (
    <span className={clsx('badge', `badge-status-${slug}`, size && `badge-${size}`)}>{label}</span>
  )
}

export function HealthDot({
  health,
  size = 'md',
  label,
}: {
  health: ServiceHealth | string
  size?: 'sm' | 'md' | 'lg'
  label?: string
}) {
  const slug = String(health).toLowerCase()
  const pulse = slug === 'critical' || slug === 'down'
  return (
    <span
      className={clsx(
        'health-dot',
        `health-${slug}`,
        size !== 'md' && `health-dot-${size}`,
        pulse && 'health-dot-pulse',
      )}
      style={pulse ? { color: `var(--health-${slug})` } : undefined}
      title={label}
      role="img"
      aria-label={label ?? slug}
    />
  )
}

export function LiveDot({
  state,
  label,
}: {
  state: 'live' | 'connecting' | 'error' | 'closed'
  label: string
}) {
  return <span className={clsx('live-dot', `live-${state}`)}>{label}</span>
}
