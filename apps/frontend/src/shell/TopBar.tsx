import { useCallback, useEffect, useState } from 'react'
import { useMatches } from 'react-router-dom'
import { Icon } from '../ui/Icon'
import { currentTheme, setTheme, type Theme } from '../lib/theme'

interface RouteHandle {
  crumb?: string
  parent?: string
}

export function TopBar({ onOpenPalette }: { onOpenPalette: () => void }) {
  const matches = useMatches()

  const crumbs = matches
    .map((m) => (m.handle as RouteHandle | undefined)?.crumb)
    .filter((c): c is string => Boolean(c))

  return (
    <header className="topbar">
      <div className="topbar-left">
        <nav className="breadcrumb" aria-label="面包屑">
          {crumbs.length === 0 && <span className="breadcrumb-item-current">指挥中心</span>}
          {crumbs.map((crumb, i) => (
            <span key={`${crumb}-${i}`} style={{ display: 'contents' }}>
              {i > 0 && <span className="breadcrumb-sep">/</span>}
              <span className={i === crumbs.length - 1 ? 'breadcrumb-item-current' : 'breadcrumb-item'}>
                {crumb}
              </span>
            </span>
          ))}
        </nav>
      </div>

      <div className="topbar-right">
        <button className="topbar-search" onClick={onOpenPalette}>
          <Icon name="search" size={14} />
          <span>搜索或跳转…</span>
          <kbd className="kbd">
            <span>⌘</span>
            <span>K</span>
          </kbd>
        </button>
        <ThemeToggle />
        <span className="env-badge">模拟环境</span>
        <Clock />
      </div>
    </header>
  )
}

/**
 * Theme switch.
 *
 * The palette lives on `<html data-theme>`, so this writes one attribute and
 * lets CSS do the rest — no re-render, no context, and every component picks up
 * the change because they all read the same custom properties.
 */
function ThemeToggle() {
  const [theme, setLocal] = useState<Theme>(() => currentTheme())

  const toggle = useCallback(() => {
    setTheme(theme === 'dark' ? 'light' : 'dark')
    setLocal(theme === 'dark' ? 'light' : 'dark')
  }, [theme])

  // Another tab changing the theme is a real thing: an operator with the
  // console open in two windows should not have one of them silently stale.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === 'opspilot.theme') setLocal(currentTheme())
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const next = theme === 'dark' ? '浅色' : '深色'

  return (
    <button
      className="theme-toggle"
      onClick={toggle}
      title={`切换到${next}主题`}
      aria-label={`切换到${next}主题`}
    >
      <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={14} />
    </button>
  )
}

/** Isolated 1s tick so the clock never re-renders the page below it. */
function Clock() {
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])

  return (
    <time className="topbar-clock" dateTime={now.toISOString()}>
      {now.toLocaleTimeString('zh-CN', { hour12: false })}
    </time>
  )
}
