import clsx from 'clsx'
import type { HTMLAttributes, ReactNode } from 'react'

export interface PanelProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  footer?: ReactNode
  flush?: boolean
  children: ReactNode
}

/**
 * Panel — the workhorse surface. Header / body / footer structure keeps every
 * page visually consistent without each one re-inventing padding.
 */
export function Panel({
  title,
  subtitle,
  actions,
  footer,
  flush = false,
  className,
  children,
  ...rest
}: PanelProps) {
  const hasHeader = title != null || actions != null
  return (
    <div className={clsx('panel', className)} {...rest}>
      {hasHeader && (
        <div className="panel-header">
          <div className="panel-title">
            {title}
            {subtitle != null && <span className="panel-subtitle">{subtitle}</span>}
          </div>
          {actions != null && <div className="panel-actions">{actions}</div>}
        </div>
      )}
      <div className={clsx('panel-body', flush && 'panel-body-flush')}>{children}</div>
      {footer != null && <div className="panel-footer">{footer}</div>}
    </div>
  )
}

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  interactive?: boolean
  elevated?: boolean
  children: ReactNode
}

export function Card({ interactive, elevated, className, children, ...rest }: CardProps) {
  return (
    <div
      className={clsx(
        'card',
        elevated && 'card-elevated',
        interactive && 'card-interactive',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  )
}

export function Section({
  title,
  hint,
  actions,
  children,
  className,
}: {
  title: ReactNode
  hint?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={clsx('section', className)}>
      <div className="section-header">
        <div>
          <div className="section-title">{title}</div>
          {hint != null && <div className="section-hint">{hint}</div>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div className="page-title-block">
        <h1 className="page-title">{title}</h1>
        {description != null && <p className="page-description">{description}</p>}
      </div>
      {actions != null && <div className="page-actions">{actions}</div>}
    </header>
  )
}
