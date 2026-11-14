/**
 * Shared content + motion contract for the Gallery components.
 *
 * Every capability has exactly one detail route. The card grid, the route
 * table and the detail page all read from this file, so a card can never point
 * at a page that does not exist, and two cards can never share a destination.
 *
 * Recreated from the ThreeUI Gallery specification.
 * Source: ThreeUI Community · https://threeui.com · MIT License
 * (Meng To / Design+Code)
 */

import type { MotionVariant } from '../../three/noiseTextures'

export interface CapabilityStep {
  title: string
  detail: string
}

export interface CapabilityMetric {
  label: string
  value: string
  note?: string
}

export interface Capability {
  index: string
  slug: string
  title: string
  label: string
  /** One-line summary used on the gallery card. */
  description: string
  /** Longer introduction used at the top of the detail page. */
  summary: string
  to: string
  variant: MotionVariant
  image: string
  outcomes: string[]
  steps: CapabilityStep[]
  metrics: CapabilityMetric[]
  related: { label: string; to: string }[]
  scenarios: string[]
}

export const CAPABILITIES: Capability[] = [
  {
    index: '01',
    slug: 'triage',
    title: '故障定级',
    label: 'TRIAGE',
    description: '依据影响范围、严重程度与时间窗口完成分级，确定响应优先级。',
    summary:
      '故障定级为后续调查与恢复提供统一输入：事件级别、影响服务、响应时限与升级路径。',
    to: '/capabilities/triage',
    variant: 'rising-diagonal',
    image: '/gallery/01-triage.jpg',
    outcomes: ['事件级别与影响范围', '响应时限与升级路径', '关联服务与责任团队'],
    steps: [
      { title: '接收告警', detail: '聚合告警来源、服务标识与首次发生时间。' },
      { title: '评估影响', detail: '核对受影响服务、请求量与核心业务指标。' },
      { title: '确定级别', detail: '按 SEV1–SEV4 标准确定事件级别与响应时限。' },
      { title: '建立事件', detail: '创建故障记录并同步相关团队与值班人员。' },
    ],
    metrics: [
      { label: '分诊级别', value: 'SEV1–SEV4' },
      { label: '定级覆盖', value: '12 / 12', note: '基准场景' },
      { label: '越级处置', value: '0', note: '高风险操作' },
    ],
    related: [
      { label: '故障列表', to: '/incidents' },
      { label: 'Agent 运行', to: '/agents' },
    ],
    scenarios: [
      'checkout-db-pool-exhaustion',
      'payment-bad-deployment',
      'gateway-dependency-cascade',
    ],
  },
  {
    index: '02',
    slug: 'evidence',
    title: '证据采集',
    label: 'EVIDENCE',
    description: '并行采集日志、指标、部署与变更记录，单点超时不影响整体调查。',
    summary:
      '证据采集将分散在日志、指标、部署记录与代码变更中的信息整理为可核查的证据清单。',
    to: '/capabilities/evidence',
    variant: 'falling-diagonal',
    image: '/gallery/02-evidence.jpg',
    outcomes: ['证据清单与来源', '采集时间与覆盖范围', '缺失证据与超时记录'],
    steps: [
      { title: '制定计划', detail: '根据事件级别确定需要采集的证据类型与时间窗口。' },
      { title: '并行采集', detail: '同时调用日志、指标、部署与变更查询工具。' },
      { title: '校验完整性', detail: '检查关键证据是否缺失，记录超时与失败原因。' },
      { title: '汇总证据', detail: '按来源与时间排序，形成后续假设验证的输入。' },
    ],
    metrics: [
      { label: '工具并发', value: '6', note: '默认上限' },
      { label: '证据召回率', value: '83%', note: '离线基准' },
      { label: '证据引用率', value: '29%', note: '离线基准' },
    ],
    related: [
      { label: 'Agent 运行', to: '/agents' },
      { label: '可观测性', to: '/observability' },
    ],
    scenarios: ['checkout-db-pool-exhaustion', 'postgres-slow-queries', 'checkout-memory-leak'],
  },
  {
    index: '03',
    slug: 'hypothesis',
    title: '假设生成',
    label: 'HYPOTHESIS',
    description: '基于证据生成候选根因，标注置信度与待验证条件。',
    summary:
      '假设生成将证据与故障模式匹配，输出可验证的候选根因，并明确每条假设的验证条件。',
    to: '/capabilities/hypothesis',
    variant: 'horizontal-sweep',
    image: '/gallery/03-hypothesis.jpg',
    outcomes: ['候选根因列表', '置信度与依据', '待验证条件'],
    steps: [
      { title: '归纳证据', detail: '按服务、时间与异常类型整理证据之间的关系。' },
      { title: '生成候选', detail: '结合故障模式与历史案例生成候选根因。' },
      { title: '标注置信度', detail: '为每条假设标注置信度与支持证据。' },
      { title: '提交验证', detail: '将可验证的假设提交至假设验证节点。' },
    ],
    metrics: [
      { label: '平均重规划', value: '4.9 轮', note: '离线基准' },
      { label: '重规划覆盖', value: '12 / 12', note: '基准场景' },
      { label: '平均否决假设', value: '0.9 个', note: '每次调查' },
    ],
    related: [
      { label: 'Agent 运行', to: '/agents' },
      { label: '评估报告', to: '/evaluations' },
    ],
    scenarios: ['gateway-dependency-cascade', 'payment-high-error-rate', 'redis-failure'],
  },
  {
    index: '04',
    slug: 'falsification',
    title: '假设验证',
    label: 'FALSIFICATION',
    description: '对候选根因执行反证与交叉验证，未通过时重新规划调查路径。',
    summary:
      '假设验证主动寻找与候选根因冲突的证据。无法通过验证的假设会被否决，并触发调查路径重规划。',
    to: '/capabilities/falsification',
    variant: 'vertical-loop',
    image: '/gallery/04-falsify.jpg',
    outcomes: ['证伪结论与依据', '保留假设与置信度变化', '重规划记录'],
    steps: [
      { title: '设计反证', detail: '针对每条假设定义可证伪的检查项。' },
      { title: '执行验证', detail: '调用工具核对反证条件与关键指标。' },
      { title: '评估结果', detail: '判断假设成立、待定或被否决。' },
      { title: '重新规划', detail: '保留有效假设，调整调查路径并补充证据。' },
    ],
    metrics: [
      { label: '验证覆盖', value: '100%', note: '离线基准' },
      { label: '平均否决假设', value: '0.9 个', note: '每次调查' },
      { label: '重规划触发', value: '12 / 12', note: '基准场景' },
    ],
    related: [
      { label: 'Agent 运行', to: '/agents' },
      { label: '评估报告', to: '/evaluations' },
    ],
    scenarios: [
      'checkout-deployment-cascade',
      'gateway-dependency-cascade',
      'payment-high-error-rate',
    ],
  },
  {
    index: '05',
    slug: 'diagnosis',
    title: '根因诊断',
    label: 'DIAGNOSIS',
    description: '输出带证据引用的根因结论；证据不足时明确标注无法判定。',
    summary:
      '根因诊断汇总通过验证的假设，输出可追溯的根因结论。证据不足时不生成确定性结论。',
    to: '/capabilities/diagnosis',
    variant: 'rising-diagonal',
    image: '/gallery/05-diagnosis.jpg',
    outcomes: ['根因结论与证据引用', '结论置信度', '无法判定时的缺失证据说明'],
    steps: [
      { title: '聚合证据', detail: '汇总通过验证的假设及其支持证据。' },
      { title: '匹配根因', detail: '将证据与服务指标、变更记录进行关联。' },
      { title: '评估置信度', detail: '检查证据覆盖度与冲突项，确定结论置信度。' },
      { title: '输出结论', detail: '生成根因结论；证据不足时输出无法判定。' },
    ],
    metrics: [
      { label: '根因准确率', value: '100%', note: '离线基准' },
      { label: '误判结论', value: '0', note: '离线基准' },
      { label: '证据引用率', value: '29%', note: '离线基准' },
    ],
    related: [
      { label: '故障列表', to: '/incidents' },
      { label: '评估报告', to: '/evaluations' },
    ],
    scenarios: ['checkout-db-pool-exhaustion', 'redis-failure', 'payment-bad-deployment'],
  },
  {
    index: '06',
    slug: 'recovery-plan',
    title: '恢复计划',
    label: 'PLAN',
    description: '生成分步恢复方案，包含前置检查、回滚点与影响评估。',
    summary:
      '恢复计划将根因结论转换为可执行步骤，并为每一步定义前置检查、风险等级与回滚条件。',
    to: '/capabilities/recovery-plan',
    variant: 'falling-diagonal',
    image: '/gallery/06-plan.jpg',
    outcomes: ['分步恢复方案', '风险等级与前置检查', '回滚点与预期影响'],
    steps: [
      { title: '读取根因', detail: '根据根因类型选择对应的恢复策略。' },
      { title: '生成步骤', detail: '拆解为可执行、可验证的恢复动作。' },
      { title: '标注风险', detail: '为每一步标注风险等级与前置检查条件。' },
      { title: '生成回滚点', detail: '定义失败时的回滚动作与验证方式。' },
    ],
    metrics: [
      { label: '风险分级', value: '4 级' },
      { label: '回滚点覆盖', value: '100%', note: '恢复方案' },
      { label: '前置检查', value: '3 项', note: '默认模板' },
    ],
    related: [
      { label: '审批队列', to: '/approvals' },
      { label: 'Agent 运行', to: '/agents' },
    ],
    scenarios: ['checkout-memory-leak', 'postgres-slow-queries', 'payment-provider-outage'],
  },
  {
    index: '07',
    slug: 'approval',
    title: '风险审批',
    label: 'APPROVAL',
    description: '按操作风险分级审批，高风险变更强制人工确认。',
    summary:
      '风险审批按恢复操作的影响范围与可逆性确定审批层级，确保生产环境变更经过人工确认。',
    to: '/capabilities/approval',
    variant: 'horizontal-sweep',
    image: '/gallery/07-approval.jpg',
    outcomes: ['审批层级与审批人', '审批结论与时间', '高风险操作拦截记录'],
    steps: [
      { title: '评估风险', detail: '根据操作类型、影响范围与可逆性确定风险等级。' },
      { title: '确定层级', detail: '匹配自动执行、人工审批或仅人工执行的策略。' },
      { title: '人工确认', detail: '审批人核对恢复步骤、影响与回滚方案。' },
      { title: '记录决策', detail: '保存审批结论、审批人与时间，进入执行阶段。' },
    ],
    metrics: [
      { label: '审批层级', value: '4 级' },
      { label: '高风险确认', value: '100%', note: '策略覆盖' },
      { label: '自动执行', value: '0', note: '生产变更' },
    ],
    related: [
      { label: '审批队列', to: '/approvals' },
      { label: 'Agent 运行', to: '/agents' },
    ],
    scenarios: [
      'payment-bad-deployment',
      'checkout-db-pool-exhaustion',
      'payment-provider-outage',
    ],
  },
  {
    index: '08',
    slug: 'recovery-execution',
    title: '执行与回滚',
    label: 'RECOVERY',
    description: '逐步执行恢复操作并记录环境变更，失败时按预案回滚。',
    summary:
      '执行与回滚按审批后的方案逐步操作生产环境，记录每一步的实际影响，并在失败时执行回滚。',
    to: '/capabilities/recovery-execution',
    variant: 'vertical-loop',
    image: '/gallery/08-recovery.jpg',
    outcomes: ['执行步骤与工具调用', '环境变更记录', '回滚结果与失败原因'],
    steps: [
      { title: '前置检查', detail: '确认目标服务、当前状态与回滚条件。' },
      { title: '逐步执行', detail: '按方案顺序执行恢复动作，逐步校验结果。' },
      { title: '记录变更', detail: '记录工具调用、环境变化与执行耗时。' },
      { title: '失败回滚', detail: '任一步骤失败时执行回滚并保留现场记录。' },
    ],
    metrics: [
      { label: '工具调用', value: '162 次', note: '离线基准' },
      { label: '工具失败率', value: '0%', note: '离线基准' },
      { label: '变更记录', value: '100%', note: '执行步骤' },
    ],
    related: [
      { label: 'Agent 运行', to: '/agents' },
      { label: '故障列表', to: '/incidents' },
    ],
    scenarios: ['redis-failure', 'inventory-cpu-saturation', 'checkout-cpu-saturation'],
  },
  {
    index: '09',
    slug: 'verification',
    title: '恢复验证',
    label: 'VERIFY',
    description: '对照基线校验核心指标，验证未通过时保持故障状态并回滚。',
    summary:
      '恢复验证以服务基线为准检查错误率、延迟与资源指标，确认恢复动作实际生效后才关闭故障。',
    to: '/capabilities/verification',
    variant: 'rising-diagonal',
    image: '/gallery/09-verify.jpg',
    outcomes: ['验证结果与基线差异', '恢复后的服务状态', '回滚触发记录'],
    steps: [
      { title: '采集指标', detail: '读取恢复后的错误率、延迟与资源使用率。' },
      { title: '对照基线', detail: '与故障前基线和服务健康标准逐项比较。' },
      { title: '判定结果', detail: '确认恢复是否生效，并记录未通过项。' },
      { title: '关闭或回滚', detail: '验证通过则关闭故障，未通过则执行回滚。' },
    ],
    metrics: [
      { label: '验证一致率', value: '100%', note: '离线基准' },
      { label: '环境修复率', value: '100%', note: '离线基准' },
      { label: '误报成功', value: '0 次', note: '离线基准' },
    ],
    related: [
      { label: '可观测性', to: '/observability' },
      { label: '评估报告', to: '/evaluations' },
    ],
    scenarios: [
      'checkout-db-pool-exhaustion',
      'postgres-slow-queries',
      'payment-third-party-timeout',
    ],
  },
  {
    index: '10',
    slug: 'topology',
    title: '服务拓扑',
    label: 'TOPOLOGY',
    description: '展示服务依赖、健康状态与故障传播路径。',
    summary:
      '服务拓扑汇总服务注册信息与依赖关系，为影响范围判断和故障传播分析提供统一视图。',
    to: '/capabilities/topology',
    variant: 'falling-diagonal',
    image: '/gallery/10-topology.jpg',
    outcomes: ['服务节点与依赖关系', '健康状态与异常服务', '故障传播路径'],
    steps: [
      { title: '同步服务', detail: '读取服务注册表与实例信息，更新节点列表。' },
      { title: '构建依赖', detail: '根据调用关系生成有向依赖图。' },
      { title: '计算健康', detail: '合并健康检查与指标状态，标记异常节点。' },
      { title: '标记传播', detail: '沿依赖关系标记可能受影响的上下游服务。' },
    ],
    metrics: [
      { label: '数据来源', value: '服务注册表' },
      { label: '健康刷新', value: '20s', note: '轮询间隔' },
      { label: '节点范围', value: '全部服务' },
    ],
    related: [
      { label: '服务拓扑', to: '/topology' },
      { label: '故障列表', to: '/incidents' },
    ],
    scenarios: ['gateway-dependency-cascade', 'checkout-deployment-cascade', 'redis-failure'],
  },
  {
    index: '11',
    slug: 'runbook',
    title: '运维手册',
    label: 'RUNBOOK',
    description: '按故障场景维护标准处置步骤，支持检索与引用。',
    summary:
      '运维手册按服务与故障场景组织标准处置步骤，为人工操作和 Agent 恢复方案提供统一依据。',
    to: '/capabilities/runbook',
    variant: 'horizontal-sweep',
    image: '/gallery/11-runbook.jpg',
    outcomes: ['场景化处置步骤', '关联服务与症状', '可检索的手册条目'],
    steps: [
      { title: '选择场景', detail: '按服务、故障类型与症状定位对应手册。' },
      { title: '核对步骤', detail: '确认影响面、变更记录与依赖状态。' },
      { title: '执行处置', detail: '按标准步骤操作，必要时提交审批。' },
      { title: '记录结果', detail: '保存处置过程与验证结果，供复盘引用。' },
    ],
    metrics: [
      { label: '手册来源', value: '场景定义' },
      { label: '步骤结构', value: '5 步' },
      { label: '关联场景', value: '12 个' },
    ],
    related: [
      { label: '运维手册', to: '/runbooks' },
      { label: '故障列表', to: '/incidents' },
    ],
    scenarios: ['checkout-db-pool-exhaustion', 'payment-provider-outage', 'checkout-memory-leak'],
  },
  {
    index: '12',
    slug: 'evaluation',
    title: '离线评估',
    label: 'EVALUATION',
    description: '使用基准场景回放评估诊断与恢复质量，输出可比较指标。',
    summary:
      '离线评估在固定基准场景上回放完整调查与恢复流程，输出准确率、恢复率、耗时与成本指标。',
    to: '/capabilities/evaluation',
    variant: 'vertical-loop',
    image: '/gallery/12-evaluation.jpg',
    outcomes: ['基准评分与失败场景', '诊断与恢复指标', '版本间可比较报告'],
    steps: [
      { title: '加载场景', detail: '读取基准场景及其答案键，确定评测范围。' },
      { title: '运行调查', detail: '以确定性模式执行完整调查与恢复流程。' },
      { title: '逐项评分', detail: '比对根因、证据、恢复结果与验证状态。' },
      { title: '生成报告', detail: '汇总指标与失败场景，输出可比较的评测报告。' },
    ],
    metrics: [
      { label: '基准场景', value: '12 个' },
      { label: '根因准确率', value: '100%' },
      { label: '平均耗时', value: '1,797ms' },
    ],
    related: [
      { label: '评估报告', to: '/evaluations' },
      { label: 'Agent 运行', to: '/agents' },
    ],
    scenarios: [
      'checkout-db-pool-exhaustion',
      'payment-bad-deployment',
      'gateway-dependency-cascade',
    ],
  },
]

export const SYSTEM_FACTS = [
  { value: '15', label: '流程节点' },
  { value: '22', label: '系统工具' },
  { value: '12/12', label: '重规划覆盖' },
  { value: '4.9', label: '平均调查轮次' },
]

export function findCapability(slug: string | undefined): Capability | undefined {
  return CAPABILITIES.find((capability) => capability.slug === slug)
}
