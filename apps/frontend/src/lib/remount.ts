/**
 * Escape hatch for unrecoverable DOM desync.
 *
 * Some render crashes — notably `NotFoundError: Failed to execute 'insertBefore'
 * on 'Node'` — happen in React DOM's commit phase. By then React's internal map
 * of the tree no longer matches what is actually in the document, so no error
 * boundary or state reset can repair it: every subsequent commit keeps throwing
 * against the same stale node references.
 *
 * The only reliable repair is to throw away the container React is bound to and
 * mount into a fresh one. `main.tsx` owns that procedure and registers it here;
 * the rest of the app calls `remountApp()` without knowing how it works.
 *
 * This module also owns the **repair budget**, because the two callers — the
 * router's error boundary and `window.onerror` — used to keep separate counters
 * and neither could see the other's. A repair that triggers another crash must
 * eventually stop repairing and say so, and the budget has to survive the hard
 * reload that ends the sequence, so it lives in `sessionStorage`.
 */

type RemountFn = () => void

let registered: RemountFn | null = null

export function registerRemount(fn: RemountFn) {
  registered = fn
}

export function remountApp() {
  if (registered) registered()
  else window.location.reload()
}

/** True for the commit-phase DOM errors that only a remount can clear. */
const DOM_INVARIANT =
  /insertBefore|removeChild|appendChild|replaceChild|not a child of this node|The node to be removed is not a child/i

export function isDomInvariantError(value: unknown): boolean {
  if (value instanceof Error) return DOM_INVARIANT.test(value.message)
  if (typeof value === 'string') return DOM_INVARIANT.test(value)
  return false
}

/* -----------------------------------------------------------------------------
   Repair budget
   -------------------------------------------------------------------------- */

const BUDGET_KEY = 'opspilot.domRepairs'
/** Across soft mounts *and* hard reloads: one broken mount is not a loop. */
const MAX_REPAIRS = 3

function readBudget(): number {
  try {
    return Number(window.sessionStorage.getItem(BUDGET_KEY) ?? '0') || 0
  } catch {
    return 0
  }
}

function writeBudget(n: number) {
  try {
    window.sessionStorage.setItem(BUDGET_KEY, String(n))
  } catch {
    /* storage blocked — the budget degrades to "unlimited", which is what the
       old code did anyway */
  }
}

/**
 * Claim one repair. Returns false when the budget is spent, which means the
 * tree broke the same way several times in a row and silently rebuilding again
 * would only hide a real, persistent fault.
 */
export function claimRepair(): boolean {
  const used = readBudget()
  if (used >= MAX_REPAIRS) return false
  writeBudget(used + 1)
  return true
}

/** Called once a mount has survived long enough to be considered healthy. */
export function clearRepairBudget() {
  writeBudget(0)
}

/* -----------------------------------------------------------------------------
   Foreign DOM mutation
   -------------------------------------------------------------------------- */

/**
 * Detect a mutation of the mounted tree that React did not make.
 *
 * React's DOM references are the one thing it cannot defend against: if another
 * actor removes or replaces a node inside the tree, every later commit is
 * operating on a node the document no longer holds. In practice the actor is
 * browser machine translation — Chrome and Edge both rewrite text nodes into
 * `<font>` wrappers — and the symptom is precisely the insertBefore/removeChild
 * family this module repairs.
 *
 * Naming it turns an unreproducible "the page broke again" into an actionable
 * line on the error page, and points at the fix that actually works (turn the
 * translation off for this site) rather than reloading forever.
 */
export function foreignDomMutation(): string | null {
  const html = document.documentElement

  if (html.classList.contains('translated-ltr') || html.classList.contains('translated-rtl')) {
    return '浏览器翻译（translated-ltr / translated-rtl）正在改写页面文本节点'
  }
  if (html.getAttribute('translate') === 'yes') {
    return '浏览器翻译把 <html translate> 改成了 yes，页面文本节点已被改写'
  }

  // Translate wraps each translated run of text in <font>. The app itself never
  // renders that tag, so any occurrence inside the React container is foreign.
  const injected = document.querySelectorAll('#root font').length
  if (injected > 0) {
    return `页面文本被改写：React 容器内出现 ${injected} 个 <font> 包裹节点（浏览器翻译的特征）`
  }

  return null
}
