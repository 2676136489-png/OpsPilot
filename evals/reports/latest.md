# OpsPilot Agent 评测报告

- 运行时间：`2026-10-01T12:05:38+00:00`
- 场景数：**12**，取自模拟器目录
- 模拟器：in-process (ASGI)，已开启评测模式（ground truth 可达）
- 墙钟耗时：22.1s

## 记分卡

| 指标 | 数值 |
| --- | --- |
| 根因判定准确率 | **100.0%** |
| 证据召回率 | 82.6% （被诊断引用 29.1%，可追溯到假设 33.2%） |
| 工具选择准确率 | 100.0% |
| 调查步数 | 平均 18.9 步，涉及 14.0 个不同阶段，12/12 次运行发生了重新规划（平均额外跑 4.9 个节点），被否假设 0.0 个 |
| 恢复成功率 | 100.0% （环境确实被修复 100.0%） |
| 验证准确率 | 100.0% 覆盖 12 次运行 |
| 声称已修复但环境仍是坏的 | 0 |
| 误诊率 | 0.0% （12 次给出结论，0 次弃权） |
| 升级率 | 0.0% |
| 工具调用 | 共 162 次，平均 13.5/次运行，失败 0.0% |
| Token 用量 | 共 0，平均 0.0/次运行 `{'deterministic': 12}` |
| 延迟 | 平均 1797ms，p50 1668ms，p95 2528ms，最大 3151ms |
| 追踪完整性 | 12/12 单一根节点，0 个悬空父节点，平均 130 个 span |

## 逐场景明细

| 场景 | 期望分类 | 判定分类 | 结论 | 工具数 | 召回率 | 环境 | 验证 | 延迟 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-cpu-saturation | capacity | capacity OK | ROOT_CAUSE_PROBABLE | 18 | 0.33 | 已修复 | passed | 3151ms |
| checkout-db-pool-exhaustion | database | database OK | ROOT_CAUSE_CONFIRMED | 10 | 0.75 | 已修复 | passed | 1474ms |
| checkout-deployment-cascade | deployment | deployment OK | ROOT_CAUSE_CONFIRMED | 19 | 1.00 | 已修复 | passed | 1915ms |
| checkout-memory-leak | memory | memory OK | ROOT_CAUSE_CONFIRMED | 10 | 1.00 | 已修复 | passed | 1434ms |
| gateway-dependency-cascade | cascading | dependency OK | ROOT_CAUSE_CONFIRMED | 18 | 1.00 | 已修复 | passed | 1846ms |
| inventory-cpu-saturation | capacity | capacity OK | ROOT_CAUSE_PROBABLE | 14 | 0.33 | 已修复 | passed | 2011ms |
| payment-bad-deployment | deployment | deployment OK | ROOT_CAUSE_CONFIRMED | 9 | 1.00 | 已修复 | passed | 1489ms |
| payment-high-error-rate | deployment | deployment OK | ROOT_CAUSE_CONFIRMED | 9 | 0.75 | 已修复 | passed | 1458ms |
| payment-provider-outage | third_party | third_party OK | ROOT_CAUSE_CONFIRMED | 10 | 1.00 | 已修复 | passed | 1427ms |
| payment-third-party-timeout | third_party | third_party OK | ROOT_CAUSE_CONFIRMED | 10 | 1.00 | 已修复 | passed | 1469ms |
| postgres-slow-queries | database | database OK | ROOT_CAUSE_CONFIRMED | 19 | 0.75 | 已修复 | passed | 2018ms |
| redis-failure | redis | redis OK | ROOT_CAUSE_CONFIRMED | 16 | 1.00 | 已修复 | passed | 1868ms |

## 实际走过的节点路径

重复本身就是结论：一次被重新规划的调查会把 `investigation_planner` → `parallel_investigation` 再跑一遍，而固定流程永远不会。

- **checkout-cpu-saturation**（额外跑 14 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 制定计划 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **checkout-db-pool-exhaustion**（额外跑 1 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **checkout-deployment-cascade**（额外跑 7 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 制定计划 → 并行取证 → 汇总证据 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **checkout-memory-leak**（额外跑 1 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **gateway-dependency-cascade**（额外跑 7 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 制定计划 → 并行取证 → 汇总证据 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **inventory-cpu-saturation**（额外跑 12 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 制定计划 → 生成假设 → 验证假设 → 制定计划 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **payment-bad-deployment**（额外跑 1 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **payment-high-error-rate**（额外跑 1 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **payment-provider-outage**（额外跑 1 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **payment-third-party-timeout**（额外跑 1 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **postgres-slow-queries**（额外跑 7 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 制定计划 → 并行取证 → 汇总证据 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘
- **redis-failure**（额外跑 6 个节点）：载入上下文 → 分诊 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 制定计划 → 并行取证 → 汇总证据 → 生成假设 → 验证假设 → 根因诊断 → 制定方案 → 风险评估 → 人工审批 → 人工审批 → 执行恢复 → 复查恢复 → 生成复盘

## 提出过的假设

### checkout-cpu-saturation

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | capacity | capacity | 0.65 | testing |
| H002 | capacity | capacity | 0.65 | testing |

### checkout-db-pool-exhaustion

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | database | database | 0.97 | confirmed |
| H002 | deployment | deployment | 0.56 | testing |

### checkout-deployment-cascade

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | deployment | deployment | 0.97 | confirmed |
| H002 | cascading | dependency | 0.62 | testing |

### checkout-memory-leak

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | memory | memory | 0.94 | confirmed |
| H002 | deployment | deployment | 0.56 | testing |

### gateway-dependency-cascade

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | cascading | dependency | 0.82 | confirmed |

### inventory-cpu-saturation

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | capacity | capacity | 0.65 | testing |
| H002 | capacity | capacity | 0.65 | testing |

### payment-bad-deployment

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | deployment | deployment | 0.91 | confirmed |

### payment-high-error-rate

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | deployment | deployment | 0.96 | confirmed |

### payment-provider-outage

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | third_party | third_party | 0.92 | confirmed |

### payment-third-party-timeout

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | third_party | third_party | 0.97 | confirmed |

### postgres-slow-queries

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | slow_database | database | 0.97 | confirmed |
| H002 | cascading | dependency | 0.57 | testing |

### redis-failure

| 引用 | 领域 | 分类 | 置信度 | 状态 |
| --- | --- | --- | --- | --- |
| H001 | redis | redis | 0.70 | testing |
| H002 | redis | redis | 0.97 | confirmed |
| H003 | cascading | dependency | 0.57 | testing |

## 按根因分类

- capacity: 100.0%
- cascading: 100.0%
- database: 100.0%
- deployment: 100.0%
- memory: 100.0%
- redis: 100.0%
- third_party: 100.0%

## 按难度

- easy: 100.0%
- hard: 100.0%
- medium: 100.0%

## 这些数字是怎么定义的

- **root_cause_accuracy** — 判定出的分类是否等于场景的 root_cause_category（接受领域别名，见 CATEGORY_ALIASES）
- **evidence_recall** — 场景 expected_evidence 关键词中，有多少在已收集证据里出现过
- **evidence_utilisation** — 已收集的证据里，最终诊断实际引用了多少
- **tool_selection_accuracy** — 故障所要求的每一类工具是否都被真正调用过
- **recovery_success_rate** — 某个恢复动作成功，且真实环境通过了该场景自己的判定条件
- **verification_accuracy** — Agent 自己给出的验证结论与环境 ground truth 的判定是否一致
- **false_diagnosis_rate** — 给出了 CONFIRMED/PROBABLE 结论但结论是错的（弃权不计入）
- **escalation_rate** — 没有自行收敛、而是移交人工的比例
