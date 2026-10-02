/**
 * Fold the hypothesis lifecycle out of the event log.
 *
 * The run record carries a root cause and a confidence, but not the reasoning
 * that got there. The events do: `hypothesis.created` opens each candidate with
 * an initial confidence, `hypothesis.updated` moves it (status, confidence,
 * evidence refs, reasoning), and `hypothesis.rejected` closes it. Replaying
 * those in `seq` order reconstructs which candidates were considered, what
 * they were competing against, and why the losers lost — which is the part of
 * an investigation an operator cannot reconstruct from the conclusion.
 *
 * Folding over the log rather than reading a snapshot is deliberate: a
 * snapshot says H002 is 56% confident, not that it was 44% and climbed, or
 * that it cited one more piece of evidence on the way.
 *
 * Pure and synchronous so it can be unit-tested without a component.
 */

import type { AgentEvent } from '../types'

export type HypothesisStatus = 'testing' | 'confirmed' | 'rejected' | 'abandoned' | 'unknown'

export interface HypothesisTrace {
  ref: string
  statement: string
  category: string
  status: HypothesisStatus
  /** Every confidence this hypothesis has ever been at, oldest first. */
  confidenceTrail: number[]
  confidence: number
  evidenceRefs: string[]
  /** Why the status changed, in the runtime's own words. Empty when absent. */
  reasoning: string
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

function num(v: unknown, fallback: number): number {
  const n = typeof v === 'number' ? v : Number.parseFloat(str(v))
  return Number.isFinite(n) ? n : fallback
}

function refs(v: unknown): string[] {
  return Array.isArray(v) ? v.map(str).filter(Boolean) : []
}

/** `testing` is what the runtime calls an open candidate; the rest close it. */
function normaliseStatus(v: unknown): HypothesisStatus {
  switch (str(v).toLowerCase()) {
    case 'confirmed':
    case 'testing':
    case 'rejected':
    case 'abandoned':
      return str(v).toLowerCase() as HypothesisStatus
    default:
      return 'unknown'
  }
}

/**
 * @param events Run events in any order — they get sorted by `seq` first, so
 *   the caller can pass a merged live+replay array without pre-sorting.
 */
export function foldHypotheses(events: readonly AgentEvent[]): HypothesisTrace[] {
  const byRef = new Map<string, HypothesisTrace>()
  const order: string[] = []

  const ordered = [...events].sort((a, b) => a.seq - b.seq)

  for (const e of ordered) {
    const d = e.data ?? {}
    const ref = str(d.ref)
    if (!ref) continue

    if (e.event_type === 'hypothesis.created') {
      // A re-created ref would otherwise reset the trail and lose the history
      // of how the confidence moved. Keep the existing entry and widen it.
      const existing = byRef.get(ref)
      if (existing) {
        existing.evidenceRefs = unique([...existing.evidenceRefs, ...refs(d.evidence_refs)])
        continue
      }
      byRef.set(ref, {
        ref,
        statement: str(d.statement),
        category: str(d.domain || d.category),
        status: 'testing',
        confidenceTrail: [num(d.confidence, 0)],
        confidence: num(d.confidence, 0),
        evidenceRefs: refs(d.evidence_refs),
        reasoning: '',
      })
      order.push(ref)
      continue
    }

    if (e.event_type === 'hypothesis.updated') {
      const h = byRef.get(ref)
      if (!h) continue
      const next = num(d.confidence, h.confidence)
      h.confidenceTrail.push(next)
      h.confidence = next
      h.status = normaliseStatus(d.status)
      h.evidenceRefs = unique([...h.evidenceRefs, ...refs(d.evidence_refs)])
      if (str(d.reasoning)) h.reasoning = str(d.reasoning)
      if (str(d.statement)) h.statement = str(d.statement)
      continue
    }

    if (e.event_type === 'hypothesis.rejected') {
      const h = byRef.get(ref)
      if (!h) continue
      h.status = 'rejected'
      h.reasoning = str(d.reasoning || d.reason || d.statement) || h.reasoning
      continue
    }
  }

  return order.map((ref) => byRef.get(ref)).filter((h): h is HypothesisTrace => Boolean(h))
}

/** Confirmed candidates first, then the ones still open, then the closed. */
const RANK: Record<HypothesisStatus, number> = {
  confirmed: 0,
  testing: 1,
  unknown: 2,
  abandoned: 3,
  rejected: 4,
}

export function sortHypotheses(list: HypothesisTrace[]): HypothesisTrace[] {
  return [...list].sort((a, b) => RANK[a.status] - RANK[b.status] || b.confidence - a.confidence)
}

function unique(xs: string[]): string[] {
  return [...new Set(xs)]
}
