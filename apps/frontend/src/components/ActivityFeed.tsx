import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { EmptyState, LoadingBlock } from '../ui/Feedback'
import { LiveDot, StatusBadge } from '../ui/Badge'
import { Icon } from '../ui/Icon'
import { isRunLive, useAgentRuns } from '../lib/queries'
import { createAgentEventSource, type SseStatus } from '../api/sse'
import { describeEvent, labelOf, toneOf } from '../lib/agentEvents'
import { formatClock } from '../lib/format'
import { zhRunStatus } from '../i18n'
import type { AgentEvent, AgentRun } from '../types'

interface FeedEntry {
  seq: number
  runId: string
  incidentId: string
  label: string
  tone: string
  detail: string
  timestamp: string | null
}

/**
 * Everything the feed knows, tagged with the run it came from.
 *
 * The tag is what lets the panel follow a new run without a reset effect: the
 * rendered list is *derived* (`stream.runId === runId ? … : []`) rather than
 * cleared imperatively, so switching runs can never flash the previous run's
 * rows, and no state is written synchronously from an effect.
 */
interface StreamState {
  runId: string | null
  status: SseStatus
  entries: FeedEntry[]
}

const MAX_FEED = 40

/**
 * ActivityFeed — the live "what is the AI doing right now" panel.
 *
 * Subscribes to the most recently active run's stream and merges frames into
 * a rolling feed. When no run is active it shows the run roster instead, so
 * the panel is never a dead rectangle.
 *
 * Two things this panel deliberately does *not* do: it does not invent a label
 * for an unknown event type (it prints the raw name, toned neutral), and it
 * does not render a frame's raw JSON. `data` is already projected to a
 * declared whitelist server-side, and `describeEvent` turns that into one
 * readable line — dumping the object would technically be "showing the data"
 * while telling the reader nothing.
 *
 * Deduplication is by `seq`, because `id:` is the stream's replay cursor: a
 * reconnect replays from the last delivered `seq`, and a frame that arrives
 * twice must not appear twice.
 */
export function ActivityFeed() {
  const runs = useAgentRuns()
  const [stream, setStream] = useState<StreamState>({
    runId: null,
    status: 'closed',
    entries: [],
  })

  const allRuns = runs.data ?? []
  /**
   * `listRuns` is ordered newest-first by the API, so the first live run is
   * the newest one actually making progress — the one worth watching.
   */
  const activeRun = allRuns.find((r: AgentRun) => isRunLive(r.status))
  const runId = activeRun?.id
  const subtitleRun = activeRun ?? allRuns[0]

  useEffect(() => {
    if (!runId) return
    const handle = createAgentEventSource(runId)
    handle.onStatus((status) => setStream((prev) => ({ ...prev, runId, status })))
    handle.onAny((evt: AgentEvent) => {
      const detail = describeEvent(evt)
      // Heartbeats and status-mirror frames carry no user-facing content; the
      // connection state is already shown by the live dot in the header.
      if (!detail) return
      setStream((prev) => {
        const base = prev.runId === runId ? prev.entries : []
        if (base.some((e) => e.seq === evt.seq)) return prev
        const entry: FeedEntry = {
          seq: evt.seq,
          runId: evt.run_id,
          incidentId: evt.incident_id,
          label: labelOf(evt.event_type),
          tone: toneOf(evt.event_type),
          detail,
          timestamp: evt.created_at,
        }
        return { runId, status: prev.status, entries: [entry, ...base].slice(0, MAX_FEED) }
      })
    })
    return () => handle.close()
  }, [runId])

  const entries = stream.runId === runId ? stream.entries : []
  const status: SseStatus = !runId
    ? 'closed'
    : stream.runId === runId
      ? stream.status
      : 'connecting'

  const transport: 'live' | 'connecting' | 'error' | 'closed' =
    status === 'open'
      ? 'live'
      : status === 'connecting'
        ? 'connecting'
        : status === 'error'
          ? 'error'
          : 'closed'

  return (
    <Panel
      title="AI 活动"
      subtitle={
        runId
          ? `正在跟踪运行 ${runId.slice(0, 8)}`
          : subtitleRun
            ? `最近一次运行 ${subtitleRun.id.slice(0, 8)} 已结束`
            : undefined
      }
      actions={<LiveDot state={transport} label={liveLabel(transport)} />}
      flush
    >
      {runs.isLoading && allRuns.length === 0 ? (
        <LoadingBlock label="加载 Agent 运行…" />
      ) : entries.length > 0 ? (
        <div
          className="activity-feed"
          style={{ padding: 'var(--space-3) var(--space-4)', maxHeight: 320 }}
        >
          {entries.map((e) => (
            <div key={`${e.runId}-${e.seq}`} className="activity-row">
              <time className="activity-time">{formatClock(e.timestamp)}</time>
              <div className="activity-body">
                <div className="activity-title">
                  <span className={`badge badge-sm badge-${e.tone}`}>{e.label}</span>
                  <Link
                    to={`/incidents/${e.incidentId}`}
                    className="activity-link mono"
                    title={`故障 ${e.incidentId}`}
                  >
                    {e.incidentId.slice(0, 8)}
                  </Link>
                </div>
                <div className="activity-detail">{e.detail}</div>
              </div>
            </div>
          ))}
        </div>
      ) : allRuns.length > 0 ? (
        <div style={{ padding: 'var(--space-3) var(--space-4)' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 80 }}>运行</th>
                <th>故障</th>
                <th style={{ width: 110 }}>状态</th>
                <th style={{ width: 90 }}>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {allRuns.slice(0, 6).map((run) => (
                <tr key={run.id}>
                  <td className="col-mono">
                    <Link to={`/incidents/${run.incident_id}`}>{run.id.slice(0, 6)}</Link>
                  </td>
                  <td className="col-mono" style={{ color: 'var(--muted)' }}>
                    {run.incident_id.slice(0, 8)}
                  </td>
                  <td>
                    <StatusBadge status={run.status} label={zhRunStatus(run.status)} size="sm" />
                  </td>
                  <td style={{ color: 'var(--muted)', fontSize: 'var(--text-sm)' }}>
                    {formatClock(run.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState
          icon={<Icon name="cpu" size={18} />}
          title="暂无 Agent 活动"
          hint="在故障上启动一次 AI 调查，这里会实时展示 Agent 的每一步动作。"
          compact
          action={
            <Button size="sm" variant="agent" onClick={() => (window.location.href = '/incidents')}>
              查看故障
            </Button>
          }
        />
      )}
    </Panel>
  )
}

function liveLabel(state: 'live' | 'connecting' | 'error' | 'closed'): string {
  return state === 'live'
    ? '实时'
    : state === 'connecting'
      ? '连接中'
      : state === 'error'
        ? '重连中'
        : '空闲'
}
