import { useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import dagre from 'dagre'
import '@xyflow/react/dist/style.css'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { Alert, EmptyState, LoadingBlock } from '../ui/Feedback'
import { HealthDot, LiveDot } from '../ui/Badge'
import { Icon } from '../ui/Icon'
import { useServices, useIncidents } from '../lib/queries'
import { zhHealth } from '../i18n'
import { serviceLabel } from '../lib/labels'
import { formatPercent } from '../lib/format'
import type { Service, ServiceHealth } from '../types'

const NODE_W = 188
const NODE_H = 62

interface ServiceNodeData extends Record<string, unknown> {
  label: string
  /**
   * Nullable to match `Service.health`. A node whose health was never measured
   * must render as "未知", not as a healthy green dot — the topology is the
   * one screen where a fabricated green is most misleading.
   */
  health: ServiceHealth | null
  errorRate: number | null
  p95: number | null
  incidentCount: number
}

type ServiceNode = Node<ServiceNodeData, 'service'>

/**
 * React Flow keys its internal node registry on this object's identity, so it
 * has to be created once. Passing `{{ service: ServiceNodeView }}` inline made a
 * new object every render — and this page re-renders on each services refetch —
 * which invalidates every node entry and rebuilds the node subtrees. Rebuilding
 * DOM under a library that also measures and positions those nodes is exactly
 * how a commit ends up inserting against a node that is no longer a child.
 */
const NODE_TYPES = { service: ServiceNodeView }

/**
 * TopologyPage — service dependency graph.
 *
 * Layout is computed with dagre (top-to-bottom), which keeps the graph readable
 * without hand-placing nodes. Node colour encodes health only — that is the one
 * signal an operator scans for, so it gets the strongest visual channel.
 */
export function TopologyPage() {
  return (
    <ReactFlowProvider>
      <TopologyInner />
    </ReactFlowProvider>
  )
}

function TopologyInner() {
  const servicesQ = useServices()
  const incidentsQ = useIncidents({ limit: 100 })
  const navigate = useNavigate()
  const { fitView } = useReactFlow()

  const { nodes: layoutNodes, edges: layoutEdges } = useMemo(
    () => buildGraph(servicesQ.data ?? [], (incidentsQ.data?.items ?? []).map((i) => serviceLabel(i))),
    [servicesQ.data, incidentsQ.data],
  )

  const [nodes, setNodes, onNodesChange] = useNodesState<ServiceNode>(layoutNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(layoutEdges)

  // Re-apply layout whenever the underlying service data changes.
  useEffect(() => {
    setNodes(layoutNodes)
    setEdges(layoutEdges)
  }, [layoutNodes, layoutEdges, setNodes, setEdges])

  // `!== 'healthy'` used to count every service with no verdict as degraded.
  // Only an actual verdict counts.
  const degraded = (servicesQ.data ?? []).filter(
    (s) => s.health === 'down' || s.health === 'degraded',
  ).length

  return (
    <>
      <PageHeader
        title="服务拓扑"
        description="服务依赖关系与实时健康状态。颜色只表达健康度——一眼扫出问题服务。"
        actions={
          <>
            <LiveDot
              state={servicesQ.isFetching ? 'connecting' : 'live'}
              label={servicesQ.isFetching ? '刷新中' : '实时'}
            />
            <Button
              icon={<Icon name="refresh" size={14} />}
              loading={servicesQ.isFetching}
              onClick={() => {
                servicesQ.refetch()
                incidentsQ.refetch()
              }}
            >
              刷新
            </Button>
          </>
        }
      />

      {servicesQ.error ? (
        <Alert tone="critical" title="加载服务列表失败">
          {servicesQ.error instanceof Error ? servicesQ.error.message : String(servicesQ.error)}
        </Alert>
      ) : null}

      <Panel
        flush
        title="依赖图"
        subtitle={`${servicesQ.data?.length ?? 0} 个服务${degraded > 0 ? ` · ${degraded} 个异常` : ''}`}
      >
        {servicesQ.isLoading ? (
          <LoadingBlock label="构建拓扑…" />
        ) : (servicesQ.data ?? []).length === 0 ? (
          <EmptyState
            icon={<Icon name="topology" size={20} />}
            title="暂无服务数据"
            hint="拓扑图由服务清单生成。加载服务后这里会自动出现节点。"
          />
        ) : (
          <div className="topo-canvas">
            <ReactFlow<ServiceNode>
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              nodeTypes={NODE_TYPES}
              fitView
              proOptions={{ hideAttribution: true }}
              minZoom={0.3}
              maxZoom={1.8}
              onNodeClick={(_, node) => navigate(`/incidents?service=${node.data.label}`)}
            >
              <Background gap={20} size={1} color="var(--topo-grid)" />
              <Controls showInteractive={false} />
              <MiniMap
                pannable
                zoomable
                nodeColor={(n) => healthColor((n.data as ServiceNodeData).health)}
                maskColor="var(--topo-mask)"
              />
            </ReactFlow>
            <Button
              size="sm"
              variant="ghost"
              className="topo-fit"
              onClick={() => fitView({ duration: 400, padding: 0.2 })}
            >
              适应视图
            </Button>
          </div>
        )}
      </Panel>
    </>
  )
}

function ServiceNodeView({ data }: NodeProps<ServiceNode>) {
  const health = data.health ?? 'unknown'
  return (
    <div className={`topo-node topo-node-${health}`}>
      <div className="topo-node-top">
        <span className="topo-node-name">{data.label}</span>
        <HealthDot health={health} label={zhHealth(health)} />
      </div>
      <div className="topo-node-metrics">
        <span className="mono">{formatPercent(data.errorRate, 2)}</span>
        <span className="mono">{data.p95 != null ? `${data.p95}ms` : '—'}</span>
        {data.incidentCount > 0 && <span className="topo-node-badge">{data.incidentCount}</span>}
      </div>
    </div>
  )
}

function healthColor(health: ServiceHealth | null): string {
  switch (health) {
    case 'down':
      return 'var(--critical)'
    case 'degraded':
      return 'var(--warning)'
    case 'healthy':
      return 'var(--success)'
    default:
      return 'var(--muted)'
  }
}

/**
 * Build a dagre-laid-out graph from the service list.
 *
 * Edges are derived from a naming convention: a service depends on another when
 * its name carries the other's as a prefix segment (`api-gateway` → `api`).
 * That is an honest heuristic over the simulated catalogue rather than a
 * fabricated dependency API — when the backend exposes real edges this function
 * is the single place that changes.
 */
function buildGraph(services: Service[], incidentServices: string[]) {
  const incidentCounts = new Map<string, number>()
  for (const name of incidentServices) {
    incidentCounts.set(name, (incidentCounts.get(name) ?? 0) + 1)
  }

  const nodes: ServiceNode[] = []
  const edges: Edge[] = []

  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 40, ranksep: 70, marginx: 20, marginy: 20 })
  g.setDefaultEdgeLabel(() => ({}))

  for (const svc of services) {
    g.setNode(svc.name, { width: NODE_W, height: NODE_H })
  }

  // Derive parent/child edges from shared name prefixes.
  for (const svc of services) {
    const parent = services.find(
      (other) => other.name !== svc.name && svc.name.startsWith(`${other.name}-`),
    )
    if (parent) {
      g.setEdge(parent.name, svc.name)
      edges.push({
        id: `${parent.name}->${svc.name}`,
        source: parent.name,
        target: svc.name,
        type: 'smoothstep',
        animated: svc.health === 'down' || svc.health === 'degraded',
        style: {
          stroke: svc.health === 'down' ? 'var(--critical)' : 'var(--topo-edge)',
          strokeWidth: 1.4,
        },
      })
    }
  }

  dagre.layout(g)

  for (const svc of services) {
    const pos = g.node(svc.name)
    nodes.push({
      id: svc.name,
      type: 'service',
      position: { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 },
      data: {
        label: svc.name,
        health: svc.health,
        errorRate: svc.error_rate,
        p95: svc.latency_p95,
        incidentCount: incidentCounts.get(svc.name) ?? 0,
      },
    })
  }

  return { nodes, edges }
}
