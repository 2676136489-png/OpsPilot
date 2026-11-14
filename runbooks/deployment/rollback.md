# 部署回滚

**服务**: 任意  
**分类**: deployment  
**严重级别**: critical  
**来源**: SRE 运维手册 — v3.0  

---

## 识别信号

满足以下条件时，可判断故障与最近一次部署相关：

- **健康状态立即恶化**：时间点与 ArgoCD / GitHub Actions 中的部署时间戳一致
- 发布开始后 5 分钟内，错误率由基线（<1%）升至 10% 以上
- 日志中出现导入错误、启动失败或运行时崩溃（panic），且指向新代码
- Pod 就绪探针失败：滚动更新后 0/N 个副本就绪
- `service_replicas_ready` 指标从正常值掉到 0
- commit message 里存在破坏性变更："BREAKING CHANGE" 或 "bump version to 3.0"
- 依赖服务的熔断器被触发（例如 api-gateway 把 checkout 标记为 DOWN）

## 根因模式

常见原因包括：

1. **破坏性 API 变更** —— 内部导入被重命名或删除（例如 `PriceCalculator` → `PricingEngine`）
2. **数据库迁移不一致** —— 代码依赖一个新列，但迁移还没执行
3. **配置漂移** —— 新增的配置项（特性开关、环境变量）引发了非预期行为
4. **依赖升级** —— 上游库（HTTP 客户端、ORM）在小版本里改动了行为
5. **测试覆盖不足**：正常路径测试通过，但边界场景在生产流量下失败

## 处置步骤

### 阶段 1 —— 检测（优先自动化）

```bash
# 检查最近的部署
argo list apps --status degraded

# 查看当前失败的是哪个版本
kubectl rollout history deployment/<service-name>

# 对比部署时间戳前后的错误率
# 使用 Prometheus 范围查询
```

### 阶段 2 —— 回滚流程

```bash
# 方案 A：通过 kubectl 回滚到上一个版本
kubectl rollout undo deployment/<service-name> --to-revision=N

# 方案 B：通过 ArgoCD 回滚
argo rollback <app-name> --to-sync-wave=0

# 方案 C：流量切分 —— 把 100% 流量导回旧版本（如果使用 Istio/Linkerd）
kubectl apply -f traffic-split-old-v100.yaml
```

回滚应在 30–90 秒内完成。确认新 Pod 运行的是旧镜像。

### 阶段 3 —— 冻结并排查

- 暂停该服务的 CI/CD 流水线：`argo pause <app-name>`
- 把出问题的 commit SHA 通知给值班开发
- 清理前先采集异常版本日志
- 为异常版本添加标签：`git tag bad-vX.Y.Z <sha>`

### 阶段 4 —— 前向修复（无法回滚时）

当无法回滚（库表迁移已执行、数据已写入）时：
- 在异常版本基础上部署修复提交
- 先把修复金丝雀发布到 10% 流量
- 观察 15 分钟后再全量发布

## 回滚后的验证

- 所有副本就绪：`kubectl get pods -l app=<service> | grep Running | wc -l` 的结果与预期数量一致
- `service_replicas_ready` 指标回到基线值
- 错误率从事故水平回落到部署前的基线（<1%）
- P95 延迟回到正常范围
- 健康检查端点 `/health` 在所有实例上都返回 200 OK
- 冒烟测试：执行一笔合成交易（例如 下单 → 支付 → 确认）
- 依赖服务的熔断器自动闭合
- ArgoCD 应用同步到 "Synced" 和 "Healthy"

## 事后复盘

- 从最后一个正常版本拉出 hotfix 分支
- 补一个能捕获该回归的集成测试
- 复盘部署检查清单：破坏性变更是否已提前沟通
- 考虑为该服务引入渐进式发布（金丝雀 → 蓝绿）
