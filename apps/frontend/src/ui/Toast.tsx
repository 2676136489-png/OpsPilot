import clsx from 'clsx'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { portalRoot } from '../lib/portal'

export type ToastTone = 'info' | 'success' | 'warning' | 'critical' | 'agent'

export interface Toast {
  id: string
  tone: ToastTone
  title: string
  message?: string
  duration?: number
}

interface ToastContextValue {
  push: (toast: Omit<Toast, 'id'>) => void
  success: (title: string, message?: string) => void
  error: (title: string, message?: string) => void
  info: (title: string, message?: string) => void
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (toast: Omit<Toast, 'id'>) => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
      const duration = toast.duration ?? 5000
      setToasts((prev) => [...prev.slice(-3), { ...toast, id }])
      if (duration > 0) {
        window.setTimeout(() => dismiss(id), duration)
      }
    },
    [dismiss],
  )

  const value = useMemo<ToastContextValue>(
    () => ({
      push,
      dismiss,
      success: (title, message) => push({ tone: 'success', title, message }),
      error: (title, message) => push({ tone: 'critical', title, message, duration: 8000 }),
      info: (title, message) => push({ tone: 'info', title, message }),
    }),
    [push, dismiss],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastHost toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  )
}

function ToastHost({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: string) => void }) {
  const reduced = useReducedMotion()
  return createPortal(
    <div className="toast-host" aria-live="polite" aria-atomic="false">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            className={clsx('toast', `toast-${t.tone}`)}
            // No `layout` prop: toasts only ever append, so there is nothing to
            // animate by position — and layout projection measures and reorders
            // real DOM nodes, which is the one motion feature that can fight
            // React's own commit and surface as an insertBefore crash.
            initial={{ opacity: 0, x: reduced ? 0 : 24, scale: reduced ? 1 : 0.98 }}
            animate={{ opacity: 1, x: 0, scale: 1 }}
            exit={{ opacity: 0, x: reduced ? 0 : 24, scale: reduced ? 1 : 0.98 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="toast-content">
              <div className="toast-title">{t.title}</div>
              {t.message != null && <div className="toast-message">{t.message}</div>}
            </div>
            <button className="toast-close" onClick={() => onDismiss(t.id)} aria-label="关闭通知">
              ✕
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>,
    portalRoot(),
  )
}
