import { useMemo, useState } from 'react'
import { PageHeader, Panel } from '../ui/Panel'
import { Button } from '../ui/Button'
import { EmptyState } from '../ui/Feedback'
import { Badge } from '../ui/Badge'
import { Icon } from '../ui/Icon'
import { severityZh } from '../i18n'
import { useScenarios } from '../lib/queries'
import { api } from '../api/client'
import { useToast } from '../ui/Toast'
import { toMessage } from '../utils/errors'
import type { Scenario } from '../types'

/**
 * RunbooksPage — the playbook library.
 *
 * A runbook is the *human-authored* counterpart to an agent recovery plan: the
 * steps an operator follows by hand, which the agent's plan is measured against.
 * Entries are grouped by the incident scenario they respond to.
 *
 * The catalogue is read from the simulator's scenario definitions — the same
 * source that backs incident injection — so a runbook and the incident it
 * addresses can never drift apart.
 */
export function RunbooksPage() {
  const scenariosQ = useScenarios()
  const toast = useToast()
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const grouped = useMemo(() => {
    const map = new Map<string, Scenario[]>()
    for (const s of scenariosQ.data ?? []) {
      const list = map.get(s.service) ?? []
      list.push(s)
      map.set(s.service, list)
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [scenariosQ.data])

  async function inject(name: string) {
    setBusy(name)
    try {
      await api.simulator.injectScenario(name)
      toast.success('已注入故障', `场景 ${name} 已在模拟器中激活`)
    } catch (e) {
      toast.error('注入失败', toMessage(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <PageHeader
        title="Runbooks"
        description="按故障场景组织的处置手册。每一步都对应 Agent 恢复方案中的一次受控操作。"
        actions={
          <Button
            icon={<Icon name="refresh" size={14} />}
            loading={scenariosQ.isFetching}
            onClick={() => scenariosQ.refetch()}
          >
            刷新
          </Button>
        }
      />

      {scenariosQ.isLoading ? (
        <Panel>
          <div className="skeleton skeleton-text" style={{ width: '40%' }} />
          <div className="skeleton skeleton-text" />
          <div className="skeleton skeleton-text" style={{ width: '70%' }} />
        </Panel>
      ) : grouped.length === 0 ? (
        <Panel>
          <EmptyState
            icon={<Icon name="book" size={20} />}
            title="暂无 Runbook"
            hint="Runbook 目录来自模拟器的场景定义。启动 Simulator 后这里会出现条目。"
          />
        </Panel>
      ) : (
        <div className="runbook-groups">
          {grouped.map(([service, scenarios]) => (
            <Panel key={service} title={service} subtitle={`${scenarios.length} 篇`} flush>
              <div className="runbook-list">
                {scenarios.map((sc) => {
                  const open = expanded === sc.name
                  return (
                    <div key={sc.name} className={`runbook-item${open ? ' runbook-item-open' : ''}`}>
                      <button
                        className="runbook-head"
                        onClick={() => setExpanded(open ? null : sc.name)}
                      >
                        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={13} />
                        <span className="runbook-title">{sc.description || sc.name}</span>
                        <Badge tone={severityTone(sc.severity)} size="sm">
                          {severityZh[sc.severity] ?? sc.severity}
                        </Badge>
                        <span className="runbook-name mono">{sc.name}</span>
                      </button>

                      {open && (
                        <div className="runbook-body">
                          <div className="caps-label">症状</div>
                          <ul className="runbook-symptoms">
                            {sc.symptoms.map((sym, i) => (
                              <li key={i}>
                                <Icon name="dot" size={9} />
                                <span>{sym}</span>
                              </li>
                            ))}
                          </ul>

                          <div className="caps-label" style={{ marginTop: 'var(--space-4)' }}>
                            处置步骤
                          </div>
                          <ol className="step-list">
                            <li className="step-item">
                              <span className="step-index">1</span>
                              <span className="step-title">
                                确认影响面：核对 <code>{service}</code> 的错误率与 P95 延迟是否偏离基线。
                              </span>
                            </li>
                            <li className="step-item">
                              <span className="step-index">2</span>
                              <span className="step-title">
                                拉取最近一次部署记录与变更时间，排查是否由发布引入。
                              </span>
                            </li>
                            <li className="step-item">
                              <span className="step-index">3</span>
                              <span className="step-title">
                                检查下游依赖（数据库 / 缓存 / 第三方）是否出现级联异常。
                              </span>
                            </li>
                            <li className="step-item">
                              <span className="step-index">4</span>
                              <span className="step-title">
                                生成恢复方案并提交审批；未获批前不得执行任何改动生产环境的操作。
                              </span>
                            </li>
                            <li className="step-item">
                              <span className="step-index">5</span>
                              <span className="step-title">
                                恢复后验证错误率与延迟回到基线，再关闭故障。
                              </span>
                            </li>
                          </ol>

                          <div className="row" style={{ gap: 'var(--space-2)', marginTop: 'var(--space-4)' }}>
                            <Button
                              size="sm"
                              variant="agent"
                              loading={busy === sc.name}
                              onClick={() => inject(sc.name)}
                              icon={<Icon name="lightning" size={13} />}
                            >
                              注入此故障
                            </Button>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </Panel>
          ))}
        </div>
      )}
    </>
  )
}

function severityTone(sev: Scenario['severity']): 'critical' | 'warning' | 'neutral' {
  if (sev === 'critical') return 'critical'
  if (sev === 'high' || sev === 'medium') return 'warning'
  return 'neutral'
}
