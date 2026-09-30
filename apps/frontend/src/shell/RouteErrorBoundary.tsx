import { useEffect } from 'react'
import { isRouteErrorResponse, useNavigate, useRouteError } from 'react-router-dom'
import { Button } from '../ui/Button'
import { Alert, EmptyState } from '../ui/Feedback'
import { PageHeader } from '../ui/Panel'
import { isDomInvariantError, remountApp } from '../lib/remount'
/**
 * Route-level error boundary. Without this, a thrown render error blanks the
 * whole SPA — including the sidebar — leaving the operator with no way back.
 */
export function RouteErrorBoundary() {
  const error = useRouteError()
  const navigate = useNavigate()

  const title = isRouteErrorResponse(error) ? `${error.status} ${error.statusText}` : '页面出现错误'
  const detail =
    error instanceof Error
      ? error.message
      : isRouteErrorResponse(error) && typeof error.data === 'string'
        ? error.data
        : '发生了未预期的错误。'

  // The old copy told every user to check port 8000. That is only true for
  // transport failures — a render crash has nothing to do with the backend, and
  // pointing at it sends people to restart a service that was never involved.
  const isTransport =
    /fetch|network|failed to load|connection refused|ECONNREFUSED|timeout|502|503|504/i.test(
      detail,
    ) || (isRouteErrorResponse(error) && error.status >= 500)

  const isDom = isDomInvariantError(error instanceof Error ? error.message : detail)

  // A DOM invariant failure cannot be cleared by re-rendering: React's node
  // references are already stale, so the next commit fails the same way. Rebuild
  // the container instead of asking the operator to do it. This is the branch
  // that used to be dead — the handler in main.tsx listens on `window`, and React
  // does not send commit-phase errors there while a boundary exists, it sends
  // them here.
  useEffect(() => {
    if (isDom) remountApp()
  }, [isDom])

  return (
    <div style={{ padding: 'var(--space-8)', maxWidth: 720, margin: '0 auto' }}>
      <PageHeader title={title} description="这个页面无法渲染。" />
      <Alert tone="critical" title="错误详情">
        <span className="mono">{detail}</span>
      </Alert>
      <div style={{ marginTop: 'var(--space-5)', display: 'flex', gap: 'var(--space-2)' }}>
        {isDom ? (
          <Button variant="primary" onClick={remountApp}>
            重建界面
          </Button>
        ) : (
          <Button variant="primary" onClick={() => navigate('/')}>
            返回指挥中心
          </Button>
        )}
        <Button variant="default" onClick={() => window.location.reload()}>
          重新加载
        </Button>
      </div>
      <div style={{ marginTop: 'var(--space-8)' }}>
        <EmptyState
          title="如果问题持续存在"
          hint={
            isDom
              ? '界面的 DOM 与 React 失去同步，正在自动重建。连续重建仍失败时会停在重建页并写明检测到的原因。'
              : isTransport
                ? '请检查后端服务是否在 8000 端口运行，然后重试。'
                : '这是前端渲染错误，与后端无关。完整堆栈已输出到浏览器控制台。'
          }
        />
      </div>
    </div>
  )
}
