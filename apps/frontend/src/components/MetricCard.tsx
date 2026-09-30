import clsx from 'clsx'
import type { ReactNode } from 'react'
import { motion, useReducedMotion } from 'motion/react'

export type MetricTone = 'default' | 'primary' | 'agent' | 'success' | 'warning' | 'critical'

/**
 * MetricCard — a single headline number with its context line.
 *
 * The number animates on change so live-updating values read as movement
 * rather than a silent swap.
 */
export function MetricCard({
  label,
  value,
  unit,
  sub,
  tone = 'default',
  icon,
  onClick,
}: {
  label: string
  value: ReactNode
  unit?: string
  sub?: ReactNode
  tone?: MetricTone
  icon?: ReactNode
  onClick?: () => void
}) {
  const reduced = useReducedMotion()

  return (
    <motion.div
      className={clsx('metric-card', tone !== 'default' && `metric-card-${tone}`)}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      style={onClick ? { cursor: 'pointer' } : undefined}
      initial={{ opacity: 0, y: reduced ? 0 : 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="metric-label">
        {icon}
        {label}
      </div>
      <AnimatedNumber value={value} unit={unit} />
      {sub != null && <div className="metric-sub">{sub}</div>}
    </motion.div>
  )
}

/**
 * Only numeric values spring-animate; anything else (an em dash, a string)
 * renders directly so we never animate a non-number.
 */
function AnimatedNumber({ value, unit }: { value: ReactNode; unit?: string }) {
  const reduced = useReducedMotion()

  if (typeof value !== 'number') {
    return (
      <div className="metric-value metric-value-sm">
        {value}
        {unit != null && <span className="metric-unit">{unit}</span>}
      </div>
    )
  }

  return (
    <motion.div
      className="metric-value"
      key={reduced ? 'static' : value}
      initial={reduced ? false : { opacity: 0.4, y: -3 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
    >
      {value}
      {unit != null && <span className="metric-unit">{unit}</span>}
    </motion.div>
  )
}

/** MetricStrip — compact horizontal band of small metrics. */
export function MetricStrip({
  items,
}: {
  items: { label: string; value: ReactNode; tone?: 'critical' | 'warning' | 'success' }[]
}) {
  return (
    <div className="metric-strip">
      {items.map((item) => (
        <div key={item.label} className="metric-strip-item">
          <div className="metric-strip-label">{item.label}</div>
          <div
            className="metric-strip-value"
            style={
              item.tone === 'critical'
                ? { color: 'var(--critical)' }
                : item.tone === 'warning'
                  ? { color: 'var(--warning)' }
                  : item.tone === 'success'
                    ? { color: 'var(--success)' }
                    : undefined
            }
          >
            {item.value}
          </div>
        </div>
      ))}
    </div>
  )
}
