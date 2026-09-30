/**
 * HTTP API client for OpsPilot backend.
 *
 * Uses the browser's built-in fetch API — no extra npm dependencies.
 */

import type {
  AgentEvent,
  AgentRun,
  AgentStats,
  Deployment,
  Incident,
  IncidentTimeline,
  ListResponse,
  RunTimeline,
  RunTrace,
  Scenario,
  Service,
} from '../types'

export const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

/**
 * Error carrying the HTTP status and parsed response body.
 *
 * Callers can branch on `status` (404 vs 500 vs network failure) instead of
 * parsing a formatted string.
 */
export class ApiError extends Error {
  readonly status: number
  readonly payload: unknown
  readonly url: string

  constructor(
    message: string,
    status: number,
    payload: unknown,
    url: string,
    options?: { cause?: unknown },
  ) {
    super(message, options)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
    this.url = url
  }

  /** Human-readable detail, preferring the backend's `detail` field. */
  get detail(): string {
    const p = this.payload as { detail?: unknown } | null
    if (p && typeof p === 'object' && 'detail' in p) {
      const d = p.detail
      if (typeof d === 'string') return d
      if (Array.isArray(d)) {
        // FastAPI validation errors: [{loc, msg, type}, ...]
        return d
          .map((item) =>
            typeof item === 'object' && item !== null && 'msg' in item
              ? String((item as { msg: unknown }).msg)
              : JSON.stringify(item),
          )
          .join('; ')
      }
    }
    return this.message
  }
}

async function apiFetch<T = unknown>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE}${path}`
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    ...(options?.headers as Record<string, string> | undefined),
  }

  let res: Response
  try {
    res = await fetch(url, { ...options, headers })
  } catch (cause) {
    // Network-level failure (backend down, DNS, CORS preflight rejected).
    // The cause is attached rather than stringified: the banner shows a stable
    // message while devtools still gets the underlying error.
    throw new ApiError(`无法连接后端（${url}）`, 0, null, url, { cause })
  }

  if (!res.ok) {
    const raw = await res.text().catch(() => '')
    let payload: unknown = raw
    try {
      payload = raw ? JSON.parse(raw) : null
    } catch {
      /* keep raw text */
    }
    throw new ApiError(
      `API ${res.status} ${res.statusText}`,
      res.status,
      payload,
      url,
    )
  }

  // 204 No Content
  if (res.status === 204) return undefined as T

  // Some endpoints (health) may return plain text
  const contentType = res.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    return (await res.json()) as T
  }
  return (await res.text()) as unknown as T
}

// --- Incidents ---
const incidents = {
  list: (params?: {
    status?: string
    severity?: string
    offset?: number
    limit?: number
  }) => {
    const qs = new URLSearchParams()
    if (params?.status) qs.set('status', params.status)
    if (params?.severity) qs.set('severity', params.severity)
    if (params?.offset !== undefined) qs.set('offset', String(params.offset))
    if (params?.limit !== undefined) qs.set('limit', String(params.limit))
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return apiFetch<ListResponse<Incident>>(`/incidents${suffix}`)
  },
  get: (id: string) => apiFetch<Incident>(`/incidents/${id}`),
  timeline: (id: string) =>
    apiFetch<IncidentTimeline>(`/incidents/${encodeURIComponent(id)}/events`),
  create: (payload: Partial<Incident>) =>
    apiFetch<Incident>('/incidents', { method: 'POST', body: JSON.stringify(payload) }),
  update: (id: string, payload: Partial<Incident>) =>
    apiFetch<Incident>(`/incidents/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  delete: (id: string) =>
    apiFetch<void>(`/incidents/${id}`, { method: 'DELETE' }),
}

// --- Services ---
const services = {
  list: async (): Promise<Service[]> => {
    // Backend returns ListResponse<Service> with {items, total, ...} —
    // unwrap .items so callers can treat this as a plain Service[].
    const resp = await apiFetch<ListResponse<Service>>('/services')
    return resp.items
  },
  // `service_id` is a UUID on the wire; the old signature took a *name* and
  // would have 422'd for every caller.
  get: (serviceId: string) =>
    apiFetch<Service>(`/services/${encodeURIComponent(serviceId)}`),
}

// --- Deployments ---
const deployments = {
  list: async (serviceId?: string): Promise<Deployment[]> => {
    const qs = serviceId ? `?service_id=${encodeURIComponent(serviceId)}` : ''
    // ListResponse, not a bare array — this used to be asserted as the latter
    // and would have handed callers `{items: …}` masquerading as a list.
    const resp = await apiFetch<ListResponse<Deployment>>(`/deployments${qs}`)
    return resp.items
  },
  latest: (serviceId: string) =>
    apiFetch<Deployment | null>(
      `/deployments/latest?service_id=${encodeURIComponent(serviceId)}`,
    ),
}

// --- Simulator (mock incident injection) ---
const simulator = {
  listScenarios: () => apiFetch<Scenario[]>('/simulator/scenarios'),
  services: () => apiFetch<Service[]>('/simulator/services'),
  logs: (service: string) =>
    apiFetch<any[]>(`/simulator/logs?service=${encodeURIComponent(service)}`),
  injectScenario: (name: string) =>
    apiFetch<any>(`/simulator/incidents/${encodeURIComponent(name)}/inject`, {
      method: 'POST',
    }),
  resetScenario: (name: string) =>
    apiFetch<any>(`/simulator/incidents/${encodeURIComponent(name)}/reset`, {
      method: 'POST',
    }),
}

// --- Agent ---
const agent = {
  startInvestigation: (incidentId: string, scenario?: string) => {
    const body: Record<string, unknown> = { incident_id: incidentId }
    if (scenario) body.scenario = scenario
    return apiFetch<AgentRun>(`/agent/incidents/${encodeURIComponent(incidentId)}/start`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  /** The incident's run, or `null` if it has never been investigated. */
  runForIncident: (incidentId: string) =>
    apiFetch<AgentRun | null>(
      `/agent/incidents/${encodeURIComponent(incidentId)}/run`,
    ),
  getRun: (runId: string) =>
    apiFetch<AgentRun>(`/agent/runs/${encodeURIComponent(runId)}`),
  /** The real node-by-node timeline, with each node's tool calls nested. */
  timeline: (runId: string) =>
    apiFetch<RunTimeline>(`/agent/runs/${encodeURIComponent(runId)}/steps`),
  /** Persisted events, replayable from a cursor. */
  events: (runId: string, afterSeq = 0) =>
    apiFetch<{
      run_id: string
      after_seq: number
      count: number
      last_seq: number
      events: AgentEvent[]
    }>(`/agent/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`),
  trace: (runId: string) =>
    apiFetch<RunTrace>(`/agent/runs/${encodeURIComponent(runId)}/trace`),
  listRuns: () => apiFetch<AgentRun[]>('/agent/runs'),
  stats: () => apiFetch<AgentStats>('/agent/stats'),
  approveRecovery: (approvalId: string) =>
    apiFetch<AgentRun>(`/agent/approvals/${encodeURIComponent(approvalId)}/approve`, {
      method: 'POST',
    }),
  rejectRecovery: (approvalId: string, reason: string) =>
    apiFetch<AgentRun>(`/agent/approvals/${encodeURIComponent(approvalId)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
}

// --- Health ---
const health = {
  check: () => apiFetch<{ status: string; version?: string }>('/health'),
}

export const api = {
  incidents,
  services,
  deployments,
  simulator,
  agent,
  health,
}

