/**
 * Portal root for every overlay in the app.
 *
 * Overlays used to portal straight into `document.body`. That is fine until the
 * tree desyncs and has to be rebuilt: React's own nodes cannot be told apart
 * from anything else sitting in `<body>`, so a rebuild leaves the old overlay
 * behind — and an orphaned `.scrim` is a full-screen div that swallows every
 * click on the app that was just rebuilt to fix the problem.
 *
 * One container gives the rebuild an unambiguous unit to discard.
 */
const PORTAL_ROOT_ID = 'overlay-root'

/** Created on first use, so it never exists in the document before it is needed. */
export function portalRoot(): HTMLElement {
  const existing = document.getElementById(PORTAL_ROOT_ID)
  if (existing) return existing
  const el = document.createElement('div')
  el.id = PORTAL_ROOT_ID
  document.body.appendChild(el)
  return el
}

/**
 * Replace the container with an empty one.
 *
 * The node is swapped rather than emptied: the discarded tree may still hold
 * references into the old container, and a detached container is harmless where
 * a shared one invites the same desync back.
 */
export function resetPortalRoot() {
  const existing = document.getElementById(PORTAL_ROOT_ID)
  if (!existing) return
  const fresh = document.createElement('div')
  fresh.id = PORTAL_ROOT_ID
  existing.replaceWith(fresh)
}
