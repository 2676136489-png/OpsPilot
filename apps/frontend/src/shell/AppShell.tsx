import { useCallback, useEffect, useState } from 'react'
import { useLocation, useOutlet } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { CommandPalette } from './CommandPalette'

const COLLAPSE_KEY = 'opspilot.sidebar.collapsed'

export function AppShell() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem(COLLAPSE_KEY) === '1'
    } catch {
      return false
    }
  })
  const [paletteOpen, setPaletteOpen] = useState(false)
  const location = useLocation()
  const outlet = useOutlet()

  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev
      try {
        window.localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0')
      } catch {
        /* storage unavailable — collapse still works for this session */
      }
      return next
    })
  }, [])

  // ⌘K / Ctrl+K opens the palette from anywhere
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  return (
    <div className="app-shell" data-collapsed={collapsed}>
      <Sidebar collapsed={collapsed} onToggle={toggle} />

      <div className="main-column">
        <TopBar onOpenPalette={() => setPaletteOpen(true)} />

        <main className="content">
          <div className="content-inner">
            {/*
              Route transitions are a CSS enter animation on a keyed wrapper.

              This used to be <AnimatePresence mode="wait">. That does not work
              here: during the exit phase the router has ALREADY switched to the
              new location, so the still-mounted old subtree re-renders with new
              params/context and mutates its own DOM while motion is trying to
              remove it. React then commits against stale sibling references and
              throws "insertBefore: the node is not a child". Snapshotting the
              outlet does not help — the snapshot still subscribes to router
              context.

              A keyed div has no exit phase: React unmounts the old subtree and
              mounts the new one in a single commit, so there is never a moment
              where two owners are mutating the same nodes.
            */}
            <div key={location.pathname} className="route-enter">
              {outlet}
            </div>
          </div>
        </main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  )
}
