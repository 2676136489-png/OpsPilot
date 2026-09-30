import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Alert } from '../ui/Feedback'

interface Props {
  children: ReactNode
  /** Shown as the error title — name the boundary's scope so the message is actionable. */
  label?: string
}

interface State {
  error: Error | null
}

/**
 * Keeps a render error from blanking the whole app.
 *
 * Without this, any throw inside a component unmounts the entire tree and the
 * user is left staring at an empty page with no indication of what broke —
 * which reads as "the site failed to load" rather than "a component crashed".
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep the component stack: the message alone usually points at a generic
    // "cannot read property of undefined" and is useless for locating the tree.
    console.error('[OpsPilot] render error:', error, info.componentStack)
  }

  private reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="page">
        <Alert
          tone="critical"
          title={this.props.label ?? '页面渲染失败'}
          action={
            <button type="button" className="btn btn-default" onClick={this.reset}>
              重试
            </button>
          }
        >
          <div>{error.message || String(error)}</div>
          <div style={{ marginTop: 8, opacity: 0.75 }}>
            错误详情已输出到浏览器控制台。重试会重新渲染该区域；若持续失败请刷新页面。
          </div>
        </Alert>
      </div>
    )
  }
}
