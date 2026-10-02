// Self-hosted so the console renders identically with no network: a webfont
// from a CDN is a blank flash here whenever that CDN is slow or blocked, and
// the numbers this product is about are the first thing that must not shift.
// Latin subsets only — CJK falls through to the system face, which is both
// faster and better hinted than anything we would ship.
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import '@fontsource/ibm-plex-mono/latin-600.css'

import { StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import App from './App.tsx'
import {
  claimRepair,
  clearRepairBudget,
  foreignDomMutation,
  isDomInvariantError,
  registerRemount,
} from './lib/remount'
import { resetPortalRoot } from './lib/portal'
import { applyInitialTheme } from './lib/theme'
import './index.css'

// Theme is applied before first paint so there is no flash of the wrong palette.
applyInitialTheme()

let root: Root | null = null

/**
 * Mount into a brand-new #root container.
 *
 * The old container is detached rather than cleared: a tree that already failed
 * a DOM invariant can throw again on unmount, and an orphaned container is
 * harmless because nothing in the document references it any more.
 */
function mount() {
  const previous = document.getElementById('root')
  const container = document.createElement('div')
  container.id = 'root'
  if (previous) previous.replaceWith(container)
  else document.body.appendChild(container)

  root = createRoot(container)
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

mount()

/**
 * A mount that survives this long is treated as healthy, which resets the repair
 * budget. Without it, one crash would spend the budget for the rest of the
 * session and a genuinely transient fault later on would find recovery already
 * exhausted.
 */
const HEALTHY_AFTER_MS = 10_000
let healthyTimer: number | undefined

function armHealthTimer() {
  window.clearTimeout(healthyTimer)
  healthyTimer = window.setTimeout(() => clearRepairBudget(), HEALTHY_AFTER_MS)
}

/**
 * Last-resort page, built with plain DOM.
 *
 * Once the repair budget is spent, the same commit-phase error has survived
 * several fresh mounts — so mounting React a fourth time is not a fix, it is a
 * loop. This page deliberately does not use React: there is no tree to desync,
 * so it renders and stays rendered, and it says which fault it kept hitting.
 */
function renderFallback(reason: string | null) {
  console.error(
    '[OpsPilot] repair budget exhausted — stopping the rebuild loop and serving a plain-DOM page.',
    { foreignMutation: reason },
  )
  const container = document.createElement('div')
  container.id = 'root'
  // Hard-coded to the console's own dark palette: this page renders with no
  // stylesheet, so if it used a light ground it would be the one screen in the
  // product that looks like it belongs to something else.
  container.style.cssText =
    'padding:48px;max-width:720px;margin:0 auto;min-height:100vh;box-sizing:border-box;' +
    'background:#070a12;color:#e6edf9;' +
    'font:14px/1.7 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif'

  const h = document.createElement('h1')
  h.textContent = '界面无法稳定渲染'
  h.style.cssText =
    'font-size:22px;margin:0 0 14px;font-weight:600;letter-spacing:-0.02em;color:#e6edf9'

  const p1 = document.createElement('p')
  p1.textContent =
    '页面 DOM 与 React 视图连续多次失去同步，已停止自动重建，以免反复覆盖真正的故障。'
  p1.style.cssText = 'color:#b8c6de;margin:0'

  const p2 = document.createElement('p')
  p2.style.cssText = 'color:#fbbf24;margin-top:14px'
  p2.textContent = reason
    ? `检测到外部改动：${reason}。请关闭该页面的浏览器翻译后重新加载。`
    : '若浏览器装有会改写页面内容的扩展（翻译、阅读模式、取词插件），请在本站点停用后重新加载。'

  const btn = document.createElement('button')
  btn.textContent = '重新加载'
  btn.style.cssText =
    'margin-top:24px;padding:9px 20px;border-radius:6px;border:1px solid #3a4864;' +
    'background:#172033;color:#e6edf9;cursor:pointer;font:inherit'
  btn.addEventListener('click', () => window.location.reload())

  container.append(h, p1, p2, btn)
  document.body.innerHTML = ''
  document.body.appendChild(container)
}

// React reports each failed commit, and one broken tree can fail several in a
// row. Repairing from inside a repair would interleave unmount/mount.
let repairing = false

function repair(source: string, reason: unknown) {
  if (repairing) return
  repairing = true
  try {
    const foreign = foreignDomMutation()
    console.error(
      `[OpsPilot] DOM invariant violated (${source}) — rebuilding the interface.`,
      { error: reason, foreignMutation: foreign },
    )

    if (!claimRepair()) {
      renderFallback(foreign)
      return
    }

    try {
      root?.unmount()
    } catch {
      /* expected — the tree being discarded is the broken one */
    }
    // The broken tree may still be holding overlays it never got to remove, and
    // an orphaned scrim would cover the interface we are about to rebuild.
    resetPortalRoot()
    mount()
    armHealthTimer()
  } finally {
    repairing = false
  }
}

armHealthTimer()
registerRemount(() => repair('error boundary', new Error('DOM invariant violated')))

// React rethrows commit-phase errors that no boundary catches, so those reach
// window as uncaught errors. The router's errorElement catches the rest — that
// path calls remountApp() directly, because when a boundary exists React hands
// the error to the boundary and it never arrives here.
window.addEventListener('error', (event) => {
  const value = event.error ?? event.message
  if (!isDomInvariantError(value)) return
  event.preventDefault()
  repair('window', value)
})
