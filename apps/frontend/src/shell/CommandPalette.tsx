import clsx from 'clsx'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { Icon, type IconName } from '../ui/Icon'
import { serviceLabel } from '../lib/labels'
import { portalRoot } from '../lib/portal'

interface Command {
  id: string
  group: string
  label: string
  hint?: string
  icon: IconName
  run: () => void
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const reduced = useReducedMotion()
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const listRef = useRef<HTMLDivElement>(null)

  const { data: incidents } = useQuery({
    queryKey: ['incidents', 'palette'],
    queryFn: () => api.incidents.list({ limit: 25 }),
    enabled: open,
    retry: false,
  })

  const commands = useMemo<Command[]>(() => {
    const nav: Command[] = [
      { id: 'nav-home', group: '导航', label: '指挥中心', icon: 'grid', run: () => navigate('/') },
      { id: 'nav-incidents', group: '导航', label: '故障', icon: 'alert', run: () => navigate('/incidents') },
      { id: 'nav-topology', group: '导航', label: '服务拓扑', icon: 'topology', run: () => navigate('/topology') },
      { id: 'nav-agents', group: '导航', label: 'Agent 运行', icon: 'cpu', run: () => navigate('/agents') },
      { id: 'nav-approvals', group: '导航', label: '审批队列', icon: 'check', run: () => navigate('/approvals') },
      { id: 'nav-runbooks', group: '导航', label: '运维手册', icon: 'book', run: () => navigate('/runbooks') },
      { id: 'nav-evals', group: '导航', label: '评估', icon: 'flask', run: () => navigate('/evaluations') },
      { id: 'nav-obs', group: '导航', label: '可观测性', icon: 'activity', run: () => navigate('/observability') },
    ]

    const incidentCmds: Command[] = (incidents?.items ?? []).map((inc) => ({
      id: `inc-${inc.id}`,
      group: '故障',
      label: inc.title,
      hint: serviceLabel(inc),
      icon: 'alert' as IconName,
      run: () => navigate(`/incidents/${inc.id}`),
    }))

    return [...nav, ...incidentCmds]
  }, [incidents, navigate])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commands
    return commands.filter(
      (c) => c.label.toLowerCase().includes(q) || c.hint?.toLowerCase().includes(q),
    )
  }, [commands, query])

  const grouped = useMemo(() => {
    const map = new Map<string, Command[]>()
    for (const c of filtered) {
      const list = map.get(c.group) ?? []
      list.push(c)
      map.set(c.group, list)
    }
    return [...map.entries()]
  }, [filtered])

  // Reset state each time the palette opens
  useEffect(() => {
    if (open) {
      setQuery('')
      setActiveIndex(0)
    }
  }, [open])

  useEffect(() => {
    setActiveIndex(0)
  }, [query])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
      } else if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActiveIndex((i) => Math.min(i + 1, filtered.length - 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActiveIndex((i) => Math.max(i - 1, 0))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        const cmd = filtered[activeIndex]
        if (cmd) {
          cmd.run()
          onClose()
        }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, filtered, activeIndex, onClose])

  // Keep the active row in view during keyboard navigation
  useEffect(() => {
    const el = listRef.current?.querySelector('[data-active="true"]')
    el?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  let flatIndex = -1

  return createPortal(
    <AnimatePresence>
      {open && (
        <Fragment key="command-palette">
          <motion.div
            className="scrim scrim-modal palette-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={onClose}
          />
          <motion.div
            className="palette"
            role="dialog"
            aria-modal="true"
            aria-label="命令面板"
            initial={{ opacity: 0, y: reduced ? 0 : -8, scale: reduced ? 1 : 0.99 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: reduced ? 0 : -8, scale: reduced ? 1 : 0.99 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
          >
            <input
              className="palette-input"
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索故障、页面或操作…"
              aria-label="搜索"
            />
            <div className="palette-list" ref={listRef}>
              {grouped.length === 0 && <div className="palette-empty">没有匹配的结果</div>}
              {grouped.map(([group, items]) => (
                <div key={group}>
                  <div className="palette-group">{group}</div>
                  {items.map((cmd) => {
                    flatIndex += 1
                    const isActive = flatIndex === activeIndex
                    return (
                      <button
                        key={cmd.id}
                        data-active={isActive}
                        className={clsx('palette-item', isActive && 'active')}
                        onMouseEnter={() => setActiveIndex(flatIndex)}
                        onClick={() => {
                          cmd.run()
                          onClose()
                        }}
                      >
                        <Icon name={cmd.icon} size={15} />
                        <span className="truncate">{cmd.label}</span>
                        {cmd.hint != null && <span className="palette-item-hint">{cmd.hint}</span>}
                      </button>
                    )
                  })}
                </div>
              ))}
            </div>
          </motion.div>
        </Fragment>
      )}
    </AnimatePresence>,
    portalRoot(),
  )
}
