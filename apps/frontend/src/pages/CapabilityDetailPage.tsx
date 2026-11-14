import { Link, Navigate, useParams } from 'react-router-dom'
import { PageHeader, Panel } from '../ui/Panel'
import { EmptyState } from '../ui/Feedback'
import { Icon } from '../ui/Icon'
import { useScenarios } from '../lib/queries'
import { findCapability } from '../components/gallery/galleryData'
import { severityZh } from '../i18n'

/**
 * One page per capability. The route is unique per card in the command-centre
 * gallery, so no two cards can lead to the same screen.
 */
export function CapabilityDetailPage() {
  const { slug } = useParams()
  const capability = findCapability(slug)
  const scenariosQ = useScenarios()

  if (!capability) {
    return <Navigate to="/capabilities" replace />
  }

  const relatedScenarios =
    scenariosQ.data?.filter((scenario) => capability.scenarios.includes(scenario.name)) ?? []

  return (
    <>
      <PageHeader
        title={capability.title}
        description={capability.summary}
        actions={
          <Link to="/capabilities" className="btn btn-default">
            <Icon name="grid" size={14} />
            返回能力目录
          </Link>
        }
      />

      <section className="capability-hero">
        <div
          className="capability-hero-media"
          style={{ backgroundImage: `url("${capability.image}")` }}
          role="img"
          aria-label={`${capability.title}示意图`}
        />
        <div className="capability-hero-body">
          <div className="gallery-kicker">
            <span className="gallery-kicker-index">{capability.index}</span>
            {capability.label}
          </div>
          <h2 className="capability-hero-title">{capability.description}</h2>
          <div className="capability-metrics">
            {capability.metrics.map((metric) => (
              <div key={metric.label} className="capability-metric">
                <span className="capability-metric-value">{metric.value}</span>
                <span className="capability-metric-label">{metric.label}</span>
                {metric.note != null && (
                  <span className="capability-metric-note">{metric.note}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      </section>

      <div className="capability-columns">
        <Panel title="执行流程" subtitle="按顺序执行，每一步均可追溯">
          <ol className="capability-steps">
            {capability.steps.map((step, index) => (
              <li key={step.title} className="capability-step">
                <span className="capability-step-index mono">
                  {String(index + 1).padStart(2, '0')}
                </span>
                <div>
                  <div className="capability-step-title">{step.title}</div>
                  <p className="capability-step-detail">{step.detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </Panel>

        <Panel title="输出" subtitle="该能力产生的记录与结果">
          <ul className="capability-outcomes">
            {capability.outcomes.map((outcome) => (
              <li key={outcome}>
                <Icon name="check" size={14} />
                <span>{outcome}</span>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      <Panel
        title="相关场景"
        subtitle="该能力覆盖的基准故障场景"
        actions={
          <Link to="/runbooks" className="link">
            查看全部场景
          </Link>
        }
      >
        {relatedScenarios.length === 0 ? (
          <EmptyState
            compact
            title="场景数据暂不可用"
            hint="后端连接恢复后，这里会显示与该能力关联的故障场景。"
          />
        ) : (
          <div className="capability-scenarios">
            {relatedScenarios.map((scenario) => (
              <article key={scenario.name} className="capability-scenario">
                <div className="capability-scenario-head">
                  <span className="mono capability-scenario-name">{scenario.name}</span>
                  <span className="caps-label">{severityZh[scenario.severity] ?? scenario.severity}</span>
                </div>
                <h3 className="capability-scenario-title">{scenario.title}</h3>
                <p className="capability-scenario-description">{scenario.description}</p>
              </article>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="相关入口" subtitle="进入对应的业务页面">
        <div className="capability-links">
          {capability.related.map((link) => (
            <Link key={link.to} to={link.to} className="capability-link">
              {link.label}
              <Icon name="arrow-right" size={14} />
            </Link>
          ))}
        </div>
      </Panel>
    </>
  )
}
