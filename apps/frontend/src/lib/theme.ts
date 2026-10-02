/**
 * Theme resolution — one place, so nothing else has to know the rules.
 *
 * Why dark is the default rather than the OS preference: this is an incident
 * console. It is read at 3am by someone who is looking for the one thing that
 * changed, and the whole product is built around a dark instrument ground —
 * luminous state colours on near-black, hairline structure, glow reserved for
 * live data. A user whose OS happens to be light would silently get a different
 * product, not a different skin. Light stays one click away, and an explicit
 * choice always wins.
 */

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'opspilot.theme'

export const DEFAULT_THEME: Theme = 'dark'

function readStored(): Theme | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    return raw === 'light' || raw === 'dark' ? raw : null
  } catch {
    // Private mode / storage disabled. A wrong first paint is not worth
    // breaking boot over — fall through to the default.
    return null
  }
}

/**
 * Resolve and apply the theme for this document.
 *
 * Called synchronously from `main.tsx` before the first render so the very
 * first paint is already in the right palette; there is no flash of the other
 * theme because nothing has been drawn yet.
 */
export function applyInitialTheme(): Theme {
  const theme = readStored() ?? DEFAULT_THEME
  document.documentElement.dataset.theme = theme
  return theme
}

/** Persist and apply a user choice. */
export function setTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  try {
    window.localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    /* the palette still switches for this session; only the memory is lost */
  }
}

/** What is on screen right now. Falls back to the default if the attribute is
 *  missing entirely — which is exactly the state a hand-edited DOM or an
 *  extension that strips attributes can leave behind. */
export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
}
