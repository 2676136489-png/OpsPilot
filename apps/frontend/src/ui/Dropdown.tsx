import clsx from 'clsx'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Button, type ButtonProps } from './Button'

/**
 * Dropdown — trigger + floating menu with outside-click and Escape dismissal.
 * Owns its own open state so callers never wire document listeners themselves.
 */
export function Dropdown({
  trigger,
  children,
  align = 'right',
  menuWidth,
}: {
  trigger: (props: { open: boolean; toggle: () => void }) => ReactNode
  children: (props: { close: () => void }) => ReactNode
  align?: 'left' | 'right'
  menuWidth?: number
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const reduced = useReducedMotion()

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="dropdown" ref={ref}>
      {trigger({ open, toggle: () => setOpen((v) => !v) })}
      <AnimatePresence>
        {open && (
          <motion.div
            className={clsx('dropdown-menu', align === 'left' && 'dropdown-menu-left')}
            style={menuWidth ? { minWidth: menuWidth } : undefined}
            role="menu"
            initial={{ opacity: 0, y: reduced ? 0 : -4, scale: reduced ? 1 : 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: reduced ? 0 : -4, scale: reduced ? 1 : 0.98 }}
            transition={{ duration: 0.14, ease: [0.16, 1, 0.3, 1] }}
          >
            {children({ close: () => setOpen(false) })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/** Convenience trigger button matching the Dropdown contract. */
export function DropdownButton({
  open,
  toggle,
  children,
  ...rest
}: { open: boolean; toggle: () => void; children: ReactNode } & Omit<ButtonProps, 'onClick'>) {
  return (
    <Button onClick={toggle} data-open={open} {...rest}>
      {children}
    </Button>
  )
}
