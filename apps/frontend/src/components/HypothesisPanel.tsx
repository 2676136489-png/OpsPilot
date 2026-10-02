import { useMemo } from 'react'
import clsx from 'clsx'
import { Panel } from '../ui/Panel'
import { foldHypotheses, sortHypotheses, type HypothesisTrace } from '../lib/hypotheses'
import type { AgentEvent } from '../types'

/**
 * The candidates this run weighed, and what happened to each.
 *
 * The run record exposes the conclusion (root cause, confidence) and the
 * events expose the reasoning, but nothing rendered the reasoning — so a
 * finished investigation showed *what* the agent concluded and not *why* it
 * preferred that over the alternatives. For a system whose whole premise is
 * that it does not guess, that gap is the whole point.
 *
 * Every number here is read off the persisted event log, not recomputed: the
 * confidence trail is the sequence the runtime itself published, so a
 * hypothesis that climbed 44% → 56% shows both numbers rather than only the
 * last one.
 */
export function HypothesisPanel({ events }: { events?: readonly AgentEvent[] }) {
  const list = useMemo(
    () => sortHypotheses(foldHypotheses(events ?? [])),
    [events],
  )

  if (list.length === 0) return null

  return (
    <Panel
      title="候选假设与证伪"
      subtitle={`${list.length} 个候选 · 含被排除的方向`}
    >
      <ul className="hypo-list">
        {list.map((h) => (
          <HypothesisRow key={h.ref} h={h} />
        ))}
      </ul>
    </Panel>
  )
}

const STATUS_TEXT: Record<HypothesisTrace['status'], string> = {
  confirmed: '已证实',
  testing: '仍待验证',
  rejected: '已否决',
  abandoned: '已放弃',
  unknown: '状态未知',
}

function HypothesisRow({ h }: { h: HypothesisTrace }) {
  // One candidate moving is worth more than a percentage: 44% → 97% is the
  // difference between a guess that found its evidence and one that was lucky.
  const climbed = h.confidenceTrail.length > 1
  return (
    <li className={clsx('hypo', `hypo-${h.status}`)}>
      <div className="hypo-head">
        <span className="hypo-ref mono">{h.ref}</span>
        <span className="hypo-state">{STATUS_TEXT[h.status]}</span>
        <span className="hypo-conf mono">
          {climbed
            ? `${(h.confidenceTrail[0] * 100).toFixed(0)}% → ${(h.confidence * 100).toFixed(0)}%`
            : `${(h.confidence * 100).toFixed(0)}%`}
        </span>
      </div>
      <div className="hypo-statement">{h.statement}</div>
      {h.reasoning && <div className="hypo-reasoning">{h.reasoning}</div>}
      {h.evidenceRefs.length > 0 && (
        <div className="hypo-evidence">
          <span className="caps-label">引用证据</span>
          {h.evidenceRefs.map((r) => (
            <span key={r} className="ref-chip mono">
              {r}
            </span>
          ))}
        </div>
      )}
    </li>
  )
}
