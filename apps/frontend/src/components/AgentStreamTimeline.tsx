import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import clsx from 'clsx'
import { createAgentEventSource, formatSseTimestamp, type SseStatus } from '../api/sse'
import { describeEvent, labelOf, toneOf, type EventTone } from '../lib/agentEvents'
import { stageLabel } from '../lib/labels'
import { zhStatus } from '../i18n'
import { LiveDot } from '../ui/Badge'
import { EmptyState } from '../ui/Feedback'
import { Icon } from '../ui/Icon'
import type { AgentEvent, AgentRun, RunTimeline, ToolCallRecord } from '../types'

interface AgentStreamTimelineProps {
  run: AgentRun
  timeline: RunTimeline | null | undefined
  /** False once the run is terminal — the caller stops polling, we stop reading. */
  live: boolean
  timelineLoading?: boolean
}

type View = 'steps' | 'events'

/**
 * The agent's trace for one run, in two honest halves.
 *
 * **Steps** come from `GET /agent/runs/{id}/steps`: the node executions that
 * actually happened, each with the tool calls it made and how long it took.
 * **Events** come from the SSE stream, which replays the persisted log from
 * `seq` 0 on connect and then follows it live.
 *
 * Nothing here reconstructs state. The previous implementation built a fake
 * event list out of a run snapshot — inventing `agent_started` /
 * `evidence_added` rows and stamping them all with the same two timestamps
 * from `created_at` / `updated_at` — because there was no API to read the real
 * thing. A timeline whose ordering is inferred cannot show a re-plan, a
 * rejected hypothesis, or a rollback, which are exactly the moments that prove
 * the agent is reasoning rather than reciting.
 *
 * A finished run still shows its full history: the replay lives in the
 * database, not in a ring buffer, so opening the page an hour later yields the
 * same timeline as watching it happen.
 */
export function AgentStreamTimeline({
  run,
  timeline,
  live,
  timelineLoading,
}: AgentStreamTimelineProps) {
  const [view, setView] = useState<View>('steps')
  // Events carry a stable `key` derived at receive time. Keying the list on the
  // array index let the identity of a row slide every time the array changed,
  // so React reconciled one event's DOM against another's — the live timeline
  // is the one list in the app that mutates while the user is watching it.
  const [events, setEvents] = useState<{ key: string; evt: AgentEvent }[]>([])
  const uid = useRef(0)
  const [status, setStatus] = useState<SseStatus>('connecting')
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const autoscroll = useRef(true)
  const reduced = useReducedMotion()

  // Subscribe for the whole life of the run, including after it finishes:
  // a terminal run's replay is how the page fills its timeline on first load.
  //
  // No reset happens in here. `events` and `status` are per-run, so the caller
  // keys this component by `run.id` — a new run is a new component instance
  // with empty state, which is both cheaper and impossible to get wrong.
  useEffect(() => {
    const handle = createAgentEventSource(run.id)
    handle.onStatus(setStatus)
    handle.onAny((evt) => {
      // `seq` is the server's own ordering key, so it is the natural identity
      // for a frame. Frames without one get a local id instead of an array
      // index, which would shift under them.
      const key = evt.seq !== 0 ? `s${evt.seq}` : `u${(uid.current += 1)}`
      // The stream can deliver a frame the replay already covered, and a
      // reconnect re-sends from the cursor — de-duplicate on that identity.
      setEvents((prev) => (prev.some((p) => p.key === key) ? prev : [...prev, { key, evt }]))
    })
    return () => handle.close()
  }, [run.id])

  const steps = timeline?.steps ?? []
  const visibleEvents = useMemo(
    // Heartbeats are a transport detail, not something that happened.
    () =>
      events.filter((e) => e.evt.event_type !== 'heartbeat' && e.evt.event_type !== 'state.sync'),
    [events],
  )

  useEffect(() => {
    if (!autoscroll.current) return
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [visibleEvents.length, steps.length, view])

  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    autoscroll.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }, [])

  const transport: 'live' | 'connecting' | 'error' | 'closed' =
    status === 'open'
      ? 'live'
      : status === 'connecting'
        ? 'connecting'
        : status === 'error'
          ? 'error'
          : 'closed'

  const toolCallCount = steps.reduce((n, s) => n + s.tool_calls.length, 0)

  return (
    <div className="agent-timeline">
      <div className="agent-timeline-head">
        <div className="agent-timeline-title">
          <Icon name="cpu" size={14} />
          Agent 调查过程
          {live && <LiveDot state={transport} label={liveLabel(transport)} />}
        </div>
        <div className="agent-timeline-head-right">
          <div className="seg" role="tablist">
            <button
              role="tab"
              aria-selected={view === 'steps'}
              className={clsx('seg-btn', view === 'steps' && 'seg-btn-on')}
              onClick={() => setView('steps')}
            >
              步骤 {steps.length}
            </button>
            <button
              role="tab"
              aria-selected={view === 'events'}
              className={clsx('seg-btn', view === 'events' && 'seg-btn-on')}
              onClick={() => setView('events')}
            >
              事件 {visibleEvents.length}
            </button>
          </div>
        </div>
      </div>

      {view === 'steps' && (
        <div className="agent-timeline-sub">
          {toolCallCount} 次工具调用
          {run.current_node && live ? ` · 当前节点 ${stageLabel(run.current_node)}` : ''}
        </div>
      )}

      <div className="agent-timeline-scroll" ref={scrollRef} onScroll={onScroll}>
        {view === 'steps' ? (
          timelineLoading && !timeline ? (
            <div className="agent-timeline-empty">
              <span>载入调查步骤…</span>
            </div>
          ) : steps.length === 0 ? (
            <EmptyState compact title="尚无已执行的节点" hint="Agent 正在启动。" />
          ) : (
            <AnimatePresence initial={false}>
              {steps.map((step) => (
                <StepRow key={step.id} step={step} reduced={Boolean(reduced)} />
              ))}
            </AnimatePresence>
          )
        ) : visibleEvents.length === 0 ? (
          <div className="agent-timeline-empty">
            <span>{live ? '等待 Agent 事件…' : '本次运行没有记录到事件'}</span>
          </div>
        ) : (
          <AnimatePresence initial={false}>
            {visibleEvents.map((item) => (
              <EventRow key={item.key} evt={item.evt} reduced={Boolean(reduced)} />
            ))}
          </AnimatePresence>
        )}
      </div>
    </div>
  )
}

/** One node execution, expandable to the tool calls it made. */
function StepRow({
  step,
  reduced,
}: {
  step: RunTimeline['steps'][number]
  reduced: boolean
}) {
  const [open, setOpen] = useState(false)
  const tone: EventTone =
    step.status === 'failed'
      ? 'critical'
      : step.status === 'timeout'
        ? 'warning'
        : step.status === 'running'
          ? 'info'
          : 'neutral'
  const hasCalls = step.tool_calls.length > 0

  return (
    <motion.div
      className={clsx('agent-event', `agent-event-${tone}`)}
      initial={reduced ? false : { opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="agent-event-rail">
        <span className={clsx('agent-event-marker', `marker-${tone}`)} />
      </div>
      <div className="agent-event-main">
        <div className="agent-event-top">
          <span className="step-seq mono">{step.sequence}</span>
          <span className={clsx('badge', 'badge-sm', `badge-${tone}`)}>
            {stageLabel(step.stage)}
          </span>
          {step.attempt > 1 && (
            <span className="badge badge-sm badge-warning">第 {step.attempt} 次</span>
          )}
          <time className="agent-event-time">
            {step.duration_ms != null ? `${step.duration_ms}ms` : zhStatus(step.status)}
          </time>
        </div>
        {step.error && <div className="agent-event-desc hl-crit">{step.error}</div>}
        {hasCalls ? (
          <>
            <button className="agent-event-payload-toggle" onClick={() => setOpen((v) => !v)}>
              <Icon name={open ? 'chevron-down' : 'chevron-right'} size={11} />
              {step.tool_calls.length} 次工具调用
            </button>
            <AnimatePresence initial={false}>
              {open && (
                <motion.div
                  className="toolcall-list"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.16 }}
                >
                  {step.tool_calls.map((call) => (
                    <ToolCallRow key={call.id} call={call} />
                  ))}
                </motion.div>
              )}
            </AnimatePresence>
          </>
        ) : (
          <div className="agent-event-desc agent-muted">未调用工具</div>
        )}
      </div>
    </motion.div>
  )
}

function ToolCallRow({ call }: { call: ToolCallRecord }) {
  const [open, setOpen] = useState(false)
  const tone: EventTone =
    call.status === 'succeeded'
      ? 'success'
      : call.status === 'failed'
        ? 'critical'
        : call.status === 'timeout' || call.status === 'blocked'
          ? 'warning'
          : 'neutral'
  const args = Object.entries(call.arguments ?? {})
  const result = call.result ?? null
  const details = [
    ...args.map(([k, v]) => [k, v] as const),
    ...(result ? [['结果', result] as const] : []),
  ]

  return (
    <div className="toolcall">
      <div className="toolcall-top">
        <span className={clsx('toolcall-dot', `marker-${tone}`)} />
        <span className="toolcall-name mono">{call.tool_name}</span>
        <span className="toolcall-status">{toolStatusLabel(call.status)}</span>
        {call.duration_ms != null && (
          <span className="toolcall-duration mono">{call.duration_ms}ms</span>
        )}
      </div>
      {call.error_message && <div className="toolcall-error hl-crit">{call.error_message}</div>}
      {details.length > 0 && (
        <>
          <button className="agent-event-payload-toggle" onClick={() => setOpen((v) => !v)}>
            <Icon name={open ? 'chevron-down' : 'chevron-right'} size={11} />
            参数
          </button>
          <AnimatePresence initial={false}>
            {open && (
              <motion.dl
                className="agent-event-payload kv"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.16 }}
              >
                {details.map(([k, v]) => (
                  <div className="kv-row" key={k}>
                    <dt className="kv-key">{k}</dt>
                    <dd className="kv-val">{previewOf(v)}</dd>
                  </div>
                ))}
              </motion.dl>
            )}
          </AnimatePresence>
        </>
      )}
    </div>
  )
}

function EventRow({ evt, reduced }: { evt: AgentEvent; reduced: boolean }) {
  const [open, setOpen] = useState(false)
  const tone = toneOf(evt.event_type)
  // `data` arrives over the wire, so the declared type is a claim about the
  // server's envelope rather than something the compiler can enforce. The
  // stream's own control frames used to omit it entirely and this line took
  // the whole page down with "Cannot convert undefined or null to object".
  // A frame with no payload shows as a frame with no payload.
  const entries = Object.entries(evt.data ?? {})
  const description = describeEvent(evt)

  return (
    <motion.div
      className={clsx('agent-event', `agent-event-${tone}`)}
      initial={reduced ? false : { opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="agent-event-rail">
        <span className={clsx('agent-event-marker', `marker-${tone}`)} />
      </div>
      <div className="agent-event-main">
        <div className="agent-event-top">
          <span className={clsx('badge', 'badge-sm', `badge-${tone}`)}>
            {labelOf(evt.event_type)}
          </span>
          {evt.stage && <span className="agent-event-stage mono">{stageLabel(evt.stage)}</span>}
          <time className="agent-event-time">{formatSseTimestamp(evt.created_at)}</time>
        </div>
        {description && <div className="agent-event-desc">{description}</div>}
        {entries.length > 0 && (
          <>
            <button className="agent-event-payload-toggle" onClick={() => setOpen((v) => !v)}>
              <Icon name={open ? 'chevron-down' : 'chevron-right'} size={11} />
              字段
            </button>
            <AnimatePresence initial={false}>
              {open && (
                <motion.dl
                  className="agent-event-payload kv"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.16 }}
                >
                  {entries.map(([key, value]) => (
                    <div className="kv-row" key={key}>
                      <dt className="kv-key">{key}</dt>
                      <dd className="kv-val">{previewOf(value)}</dd>
                    </div>
                  ))}
                </motion.dl>
              )}
            </AnimatePresence>
          </>
        )}
      </div>
    </motion.div>
  )
}

function toolStatusLabel(status: ToolCallRecord['status']): string {
  switch (status) {
    case 'succeeded':
      return '成功'
    case 'failed':
      return '失败'
    case 'timeout':
      return '超时'
    case 'blocked':
      return '被预算拦截'
    case 'running':
      return '执行中'
    default:
      return '等待'
  }
}

/** Compact one-line rendering for an arbitrary JSON value. */
function previewOf(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    const text = JSON.stringify(value)
    return text.length > 240 ? `${text.slice(0, 240)}…` : text
  } catch {
    return String(value)
  }
}

function liveLabel(state: 'live' | 'connecting' | 'error' | 'closed'): string {
  return state === 'live'
    ? '实时'
    : state === 'connecting'
      ? '连接中'
      : state === 'error'
        ? '重连中'
        : '已结束'
}
