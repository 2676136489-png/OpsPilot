import clsx from 'clsx'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { Fragment, useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Button } from './Button'
import { portalRoot } from '../lib/portal'

const scrimVariants = { hidden: { opacity: 0 }, visible: { opacity: 1 } }

function useEscape(active: boolean, onClose: () => void) {
  useEffect(() => {
    if (!active) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [active, onClose])
}

function useLockScroll(active: boolean) {
  useEffect(() => {
    if (!active) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [active])
}

/* -----------------------------------------------------------------------------
   Drawer — slides in from the right (or left). Used for Agent Execution and
   Service Inspector.
   -------------------------------------------------------------------------- */
export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  footer,
  side = 'right',
  width,
  children,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  footer?: ReactNode
  side?: 'right' | 'left'
  width?: number
  children: ReactNode
}) {
  useEscape(open, onClose)
  useLockScroll(open)
  const reduced = useReducedMotion()

  const offset = reduced ? 0 : side === 'right' ? 32 : -32

  return createPortal(
    <AnimatePresence>
      {open && (
        // AnimatePresence identifies children by key. A bare <></> leaves the
        // key to React.Children's positional fallback, which shifts when the
        // sibling set changes — give it a stable explicit key instead.
        <Fragment key="drawer">
          <motion.div
            className="scrim"
            variants={scrimVariants}
            initial="hidden"
            animate="visible"
            exit="hidden"
            transition={{ duration: 0.18 }}
            onClick={onClose}
          />
          <motion.aside
            className={clsx('drawer', side === 'left' && 'drawer-left')}
            style={width ? { width: `min(${width}px, 94vw)` } : undefined}
            role="dialog"
            aria-modal="true"
            initial={{ x: offset, opacity: reduced ? 0 : 1 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: offset, opacity: reduced ? 0 : 1 }}
            transition={{ duration: 0.26, ease: [0.16, 1, 0.3, 1] }}
          >
            <header className="drawer-header">
              <div>
                <div className="drawer-title">{title}</div>
                {subtitle != null && (
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--muted)', marginTop: 2 }}>
                    {subtitle}
                  </div>
                )}
              </div>
              <Button variant="ghost" size="sm" onClick={onClose} aria-label="关闭">
                ✕
              </Button>
            </header>
            <div className="drawer-body">{children}</div>
            {footer != null && <footer className="drawer-footer">{footer}</footer>}
          </motion.aside>
        </Fragment>
      )}
    </AnimatePresence>,
    portalRoot(),
  )
}

/* -----------------------------------------------------------------------------
   Modal — centered dialog for confirmations and short forms.
   -------------------------------------------------------------------------- */
export function Modal({
  open,
  onClose,
  title,
  footer,
  wide,
  children,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  footer?: ReactNode
  wide?: boolean
  children: ReactNode
}) {
  useEscape(open, onClose)
  useLockScroll(open)
  const reduced = useReducedMotion()

  return createPortal(
    <AnimatePresence>
      {open && (
        <Fragment key="modal">
          <motion.div
            className="scrim scrim-modal"
            variants={scrimVariants}
            initial="hidden"
            animate="visible"
            exit="hidden"
            transition={{ duration: 0.18 }}
            onClick={onClose}
          />
          <motion.div
            className={clsx('modal', wide && 'modal-wide')}
            role="dialog"
            aria-modal="true"
            initial={{ opacity: 0, scale: reduced ? 1 : 0.97, y: reduced ? 0 : 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: reduced ? 1 : 0.98, y: reduced ? 0 : 4 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          >
            <header className="modal-header">
              <div className="drawer-title">{title}</div>
              <Button variant="ghost" size="sm" onClick={onClose} aria-label="关闭">
                ✕
              </Button>
            </header>
            <div className="modal-body">{children}</div>
            {footer != null && <footer className="modal-footer">{footer}</footer>}
          </motion.div>
        </Fragment>
      )}
    </AnimatePresence>,
    portalRoot(),
  )
}

/* -----------------------------------------------------------------------------
   ConfirmDialog — the common "are you sure" shape, so no page hand-rolls it.
   -------------------------------------------------------------------------- */
export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = '确认',
  cancelLabel = '取消',
  tone = 'primary',
  busy,
}: {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: ReactNode
  message: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  tone?: 'primary' | 'danger' | 'success'
  busy?: boolean
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button variant={tone === 'danger' ? 'danger' : tone === 'success' ? 'success' : 'primary'} onClick={onConfirm} loading={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div style={{ fontSize: 'var(--text-base)', color: 'var(--foreground-secondary)', lineHeight: 1.6 }}>
        {message}
      </div>
    </Modal>
  )
}
