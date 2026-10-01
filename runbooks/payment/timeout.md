# 第三方支付超时

**服务**: checkout, payment-service  
**分类**: third_party  
**严重级别**: high  
**来源**: SRE 运维手册 — v1.8  

---

## 现象

如何识别支付服务商故障：

- 日志出现 `httpx.ReadTimeout: connect timeout (30s) calling POST https://api.acme-pay.com/v2/charge`
- 服务日志中可见支付服务商返回的 HTTP 504 Gateway Timeout
- `payment_timeout_rate` 指标升到 20% 以上（正常 <2%）
- 支付成功率从基线（约 97%）掉到 50% 以下
- P99 延迟飙升到 7000ms 以上（请求要等超时加多次重试）
- 连续失败后熔断器切到 OPEN 状态
- Webhook 投递延迟增加（上游队列积压）

## 根因模式

外部支付服务商故障：

1. **服务商区域性故障** —— AWS 可用区故障、CDN 问题，或服务商机房宕机
2. **服务商限流** —— 我们的请求量超过服务商每秒配额，返回 429
3. **网络连通性** —— BGP 路由泄漏，或针对服务商边缘网络的 DDoS
4. **服务商 API 版本弃用** —— 旧 SDK 调用了正在被限流的已弃用端点
5. **TLS/证书问题** —— 服务商轮换证书，客户端握手失败

## 处置步骤

### 第 1 步 —— 确认不是我们自己的代码问题

```bash
# 查看部署历史 —— 如果近期没有部署，几乎可以确定是上游问题
kubectl rollout history deployment/payment-service

# 在日志中区分 429 / 504 / 连接错误
kubectl logs -l app=payment-service --since=10m | grep -E "504|429|timeout" | head -20
```

### 第 2 步 —— 客户端带指数退避地重试

如果重试风暴正在放大问题：
```python
# 当前重试策略 —— 可能过于激进
重试 5 次、无退避 → 改为重试 2 次，退避 1s、2s

# 加抖动，避免惊群
import random
delay = min(2**attempt, 10) + random.uniform(0, 0.5)
```

### 第 3 步 —— 熔断器

确认熔断器配置正确：
```yaml
# Resilience4j 或类似组件
failureRateThreshold: 50  # 失败率达 50% 时打开
waitDurationInOpenState: 30s  # 30s 后重试
slidingWindowSize: 20  # 最近 20 次调用
```

熔断器状态流转应为：CLOSED → OPEN → HALF_OPEN → CLOSED（服务商恢复后）。

### 第 4 步 —— 切换到备用服务商

```python
# 回退到备用服务商
primary = AcmePayClient()
secondary = StripeClient()

try:
    result = primary.charge(request)
except TimeoutError:
    result = secondary.charge(request)
```

确认备用服务商的凭据已预先配置并验证可用。

### 第 5 步 —— 通知服务商

- 查看服务商状态页（status.acme-pay.com）
- 提交带事故 ID 的优先支持工单
- 附上错误率和延迟指标作为证据

## 验证

- 介入后 10 分钟内 `payment_timeout_rate` 降到 5% 以下
- 支付成功率回到 >95%
- 熔断器回到 CLOSED 状态
- P99 延迟降到 2000ms 以下
- 主备服务商面板均可访问且状态健康
- 最近 5 分钟没有新的 504 错误

## 预防措施

- 配置服务商健康检查（每 30s 轮询 `/health` 端点）
- 针对 `payment_timeout_rate > 10%` 配置告警
- 预先接入至少一家备用支付服务商
- 加入隔板模式 —— 把支付服务的线程池与应用的其余部分隔离
