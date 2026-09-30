/**
 * Server-Sent Events client for the agent run stream.
 *
 * The backend emits one named SSE message per frame:
 *
 *     id: 42
 *     event: tool.started
 *     data: {"seq":42,"event_id":"…","run_id":"…","event_type":"tool.started",…}
 *
 * Two consequences shape this module:
 *
 * 1. **`onmessage` is useless here.** `EventSource` only dispatches to
 *    `onmessage` when a frame carries no `event:` field, and every frame
 *    carries one. Listeners are registered per event name, taken from the
 *    same registry the rest of the UI renders from — so a frame can never
 *    arrive under a name nothing is listening for.
 * 2. **`id:` is the replay cursor.** The browser resends it as
 *    `Last-Event-ID` when it reconnects, and the backend replays everything
 *    after it from the database. That is what makes a dropped connection a
 *    non-event instead of a gap in the timeline.
 */

import type { AgentEvent, AgentEventType } from '../types'
import { AGENT_EVENT_TYPES } from '../lib/agentEvents'
import { API_BASE } from './client'

export type SseListener = (event: AgentEvent) => void
export type SseStatusListener = (status: SseStatus) => void

export type SseStatus = 'connecting' | 'open' | 'closed' | 'error'

export interface SseHandle {
  eventSource: EventSource
  close: () => void
  on: (type: AgentEventType, listener: SseListener) => void
  onAny: (listener: SseListener) => void
  onStatus: (listener: SseStatusListener) => void
}

/**
 * Subscribe to one run's event stream.
 *
 * @param agentRunId  the run to follow
 * @param afterSeq    resume cursor. Pass the highest `seq` already rendered so
 *                    a fresh mount replays only what it has not seen; omit it
 *                    to replay the whole log from the beginning.
 */
export function createAgentEventSource(
  agentRunId: string,
  afterSeq?: number,
): SseHandle {
  const query = afterSeq && afterSeq > 0 ? `?after_seq=${afterSeq}` : ''
  const url = `${API_BASE}/agent/runs/${encodeURIComponent(agentRunId)}/stream${query}`
  const eventSource = new EventSource(url)

  const listeners: Map<AgentEventType, Set<SseListener>> = new Map()
  const anyListeners: Set<SseListener> = new Set()
  const statusListeners: Set<SseStatusListener> = new Set()

  const setStatus = (status: SseStatus) => {
    statusListeners.forEach((fn) => {
      try {
        fn(status)
      } catch {
        /* a listener throwing must not break the stream */
      }
    })
  }

  const dispatch = (payload: AgentEvent) => {
    anyListeners.forEach((fn) => {
      try {
        fn(payload)
      } catch {
        /* no-op */
      }
    })
    listeners.get(payload.event_type)?.forEach((fn) => {
      try {
        fn(payload)
      } catch {
        /* no-op */
      }
    })
  }

  eventSource.onopen = () => setStatus('open')
  eventSource.onerror = () => {
    // EventSource reconnects on its own; the distinction the UI needs is
    // "the server hung up because the run finished" versus "the transport
    // broke", because only the second is worth showing a retry for.
    setStatus(eventSource.readyState === EventSource.CLOSED ? 'closed' : 'error')
  }

  for (const type of AGENT_EVENT_TYPES) {
    eventSource.addEventListener(type, (ev: Event) => {
      const msg = ev as MessageEvent
      try {
        dispatch(JSON.parse(msg.data) as AgentEvent)
      } catch {
        // A frame we cannot parse is a server bug, not a UI state. Skipping it
        // keeps the rest of the timeline readable.
      }
    })
  }

  const close = () => {
    eventSource.close()
    setStatus('closed')
    listeners.clear()
    anyListeners.clear()
    statusListeners.clear()
  }

  return {
    eventSource,
    close,
    on: (type, listener) => {
      let set = listeners.get(type)
      if (!set) {
        set = new Set()
        listeners.set(type, set)
      }
      set.add(listener)
    },
    onAny: (listener) => anyListeners.add(listener),
    onStatus: (listener) => statusListeners.add(listener),
  }
}

/** Format an ISO timestamp into compact HH:MM:SS for the timeline. */
export function formatSseTimestamp(iso: string | null): string {
  if (!iso) return '--:--:--'
  try {
    return new Date(iso).toLocaleTimeString('en-GB', { hour12: false })
  } catch {
    return iso.slice(11, 19)
  }
}
