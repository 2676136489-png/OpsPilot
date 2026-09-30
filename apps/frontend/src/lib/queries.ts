import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

/**
 * Query hooks — one place that defines every server read, its cache key and
 * its refresh cadence. Pages consume hooks, never `api.*` directly, so
 * refetch intervals stay consistent across the product.
 *
 * Cadence reflects how fast each resource can actually change. A run's
 * progress arrives over SSE, so `useAgentRun` polls only as a safety net; the
 * step timeline only ever grows, so it polls while the run is live and stops
 * once it is terminal.
 */

export const queryKeys = {
  health: ['health'] as const,
  incidents: (params?: Record<string, unknown>) => ['incidents', params ?? {}] as const,
  incident: (id: string) => ['incident', id] as const,
  incidentTimeline: (id: string) => ['incident', id, 'timeline'] as const,
  services: ['services'] as const,
  service: (id: string) => ['service', id] as const,
  agentStats: ['agent', 'stats'] as const,
  agentRuns: ['agent', 'runs'] as const,
  agentRun: (id: string) => ['agent', 'run', id] as const,
  /** The run belonging to an incident — the detail page's entry point. */
  incidentRun: (incidentId: string) => ['agent', 'incident-run', incidentId] as const,
  runTimeline: (runId: string) => ['agent', 'run', runId, 'timeline'] as const,
  runTrace: (runId: string) => ['agent', 'run', runId, 'trace'] as const,
  metrics: ['metrics'] as const,
  scenarios: ['simulator', 'scenarios'] as const,
}

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: () => api.health.check(),
    refetchInterval: 20_000,
    retry: false,
  })
}

export function useIncidents(params?: { status?: string; severity?: string; limit?: number }) {
  return useQuery({
    queryKey: queryKeys.incidents(params),
    queryFn: () => api.incidents.list(params),
    refetchInterval: 15_000,
  })
}

export function useIncident(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.incident(id ?? ''),
    queryFn: () => api.incidents.get(id!),
    enabled: Boolean(id),
  })
}

/** The incident's business timeline — every recorded transition, in order. */
export function useIncidentTimeline(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.incidentTimeline(id ?? ''),
    queryFn: () => api.incidents.timeline(id!),
    enabled: Boolean(id),
    refetchInterval: 15_000,
  })
}

export function useServices() {
  return useQuery({
    queryKey: queryKeys.services,
    queryFn: () => api.services.list(),
    refetchInterval: 15_000,
  })
}

export function useAgentStats() {
  return useQuery({
    queryKey: queryKeys.agentStats,
    queryFn: () => api.agent.stats(),
    refetchInterval: 10_000,
  })
}

export function useAgentRuns() {
  return useQuery({
    queryKey: queryKeys.agentRuns,
    queryFn: () => api.agent.listRuns(),
    refetchInterval: 5_000,
  })
}

/**
 * The run for an incident: the live one if it has one, else the most recent,
 * else `null`.
 *
 * This is what replaces "POST on mount". `null` means the incident has never
 * been investigated, which is a real state the UI acts on (offer to start)
 * rather than a loading placeholder to paper over.
 */
export function useIncidentRun(incidentId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.incidentRun(incidentId ?? ''),
    queryFn: () => api.agent.runForIncident(incidentId!),
    enabled: Boolean(incidentId),
    refetchInterval: (query) => {
      const run = query.state.data
      if (!run) return 10_000
      return run.status === 'completed' || run.status === 'failed' ? false : 3_000
    },
  })
}

/** Single run by id — used when polling one run directly. */
export function useAgentRun(runId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.agentRun(runId ?? ''),
    queryFn: () => api.agent.getRun(runId!),
    enabled: Boolean(runId),
    refetchInterval: 3_000,
  })
}

/**
 * The persisted step timeline for a run.
 *
 * While the run is live this is polled (the SSE stream carries events, not
 * steps); once it is terminal the timeline can never change again, so polling
 * stops instead of hammering the API forever.
 */
export function useRunTimeline(runId: string | undefined, live: boolean) {
  return useQuery({
    queryKey: queryKeys.runTimeline(runId ?? ''),
    queryFn: () => api.agent.timeline(runId!),
    enabled: Boolean(runId),
    refetchInterval: live ? 3_000 : false,
  })
}

export function useRunTrace(runId: string | undefined, live: boolean) {
  return useQuery({
    queryKey: queryKeys.runTrace(runId ?? ''),
    queryFn: () => api.agent.trace(runId!),
    enabled: Boolean(runId),
    refetchInterval: live ? 5_000 : false,
  })
}

export function useScenarios() {
  return useQuery({
    queryKey: queryKeys.scenarios,
    queryFn: () => api.simulator.listScenarios(),
    staleTime: Infinity,
  })
}

/** True while the run can still make progress — i.e. it is worth polling. */
export function isRunLive(status: string | undefined): boolean {
  return status !== 'completed' && status !== 'failed'
}
