import { lazy, Suspense, type ComponentType } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AppShell } from './shell/AppShell'
import { RouteErrorBoundary } from './shell/RouteErrorBoundary'
import { LoadingBlock } from './ui/Feedback'

/**
 * Routes are code-split by page.
 *
 * Topology pulls in React Flow + dagre, and the command centre will pull in the
 * charting library — bundling those into the entry chunk would make the first
 * paint pay for screens the operator has not opened. Each page loads on demand
 * behind a shared Suspense fallback.
 *
 * Pages use named exports, so each lazy import re-maps it to a default for
 * React.lazy.
 */
const CommandCenterPage = lazy(() =>
  import('./pages/CommandCenterPage').then((m) => ({ default: m.CommandCenterPage })),
)
const IncidentsPage = lazy(() =>
  import('./pages/IncidentsPage').then((m) => ({ default: m.IncidentsPage })),
)
const IncidentDetailPage = lazy(() =>
  import('./pages/IncidentDetailPage').then((m) => ({ default: m.IncidentDetailPage })),
)
const TopologyPage = lazy(() =>
  import('./pages/TopologyPage').then((m) => ({ default: m.TopologyPage })),
)
const AgentsPage = lazy(() =>
  import('./pages/AgentsPage').then((m) => ({ default: m.AgentsPage })),
)
const ApprovalsPage = lazy(() =>
  import('./pages/ApprovalsPage').then((m) => ({ default: m.ApprovalsPage })),
)
const RunbooksPage = lazy(() =>
  import('./pages/RunbooksPage').then((m) => ({ default: m.RunbooksPage })),
)
const EvaluationsPage = lazy(() =>
  import('./pages/EvaluationsPage').then((m) => ({ default: m.EvaluationsPage })),
)
const ObservabilityPage = lazy(() =>
  import('./pages/ObservabilityPage').then((m) => ({ default: m.ObservabilityPage })),
)

/** Wrap a lazy page so every route shares one loading treatment. */
function page(Component: ComponentType) {
  return (
    <Suspense fallback={<LoadingBlock label="加载页面…" />}>
      <Component />
    </Suspense>
  )
}

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    errorElement: <RouteErrorBoundary />,
    children: [
      { index: true, element: page(CommandCenterPage), handle: { crumb: '指挥中心' } },
      { path: 'incidents', element: page(IncidentsPage), handle: { crumb: '故障' } },
      {
        path: 'incidents/:incidentId',
        element: page(IncidentDetailPage),
        handle: { crumb: '故障详情' },
      },
      { path: 'topology', element: page(TopologyPage), handle: { crumb: '服务拓扑' } },
      { path: 'agents', element: page(AgentsPage), handle: { crumb: 'Agent 运行' } },
      { path: 'approvals', element: page(ApprovalsPage), handle: { crumb: '审批队列' } },
      { path: 'runbooks', element: page(RunbooksPage), handle: { crumb: '运维手册' } },
      { path: 'evaluations', element: page(EvaluationsPage), handle: { crumb: '评估' } },
      { path: 'observability', element: page(ObservabilityPage), handle: { crumb: '可观测性' } },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
