# Redis 不可用

**服务**: api-gateway, gateway-service  
**分类**: redis  
**严重级别**: critical  
**来源**: SRE 运维手册 — v2.0  

---

## 现象

识别信号：

- 日志出现 `redis.exceptions.ConnectionError: Error 111 connecting to redis.internal:6379. Connection refused.`
- 日志反复出现 `Connection refused after N retries`
- Prometheus 的 `redis_connected` 指标从 1 掉到 0
- 缓存命中率由 80%–95% 降至接近 0
- 应用流量回退至数据库，导致数据库负载上升
- 数据库兜底路径使 P95 延迟上升 5–10 倍
- Redis 客户端一侧可能触发熔断器

## 根因模式

Redis 故障通常源自：

1. **Pod 崩溃 / OOM** —— Redis 内存上限过低、淘汰策略失效，进程被内核 OOM killer 终止
2. **网络分区** —— K8s 节点级网络策略变更或 DNS 故障，导致 Redis 服务不可达
3. **异常发布** —— 配置变更（例如将 maxmemory 从 4gb 降至 256mb）导致服务不稳定
4. **故障转移进行中** —— Redis Sentinel 或 Cluster 正在提升副本；存在短暂不可用窗口
5. **磁盘写满** —— 数据目录磁盘写满后，AOF/RDB 持久化失败

## 处置步骤

### 第 1 步 —— 快速连通性检查

```bash
redis-cli -h redis.internal -p 6379 ping
# 预期：PONG
```

返回 `Connection refused` 表示 Redis 进程已终止；请求超时表示网络连通性异常。

### 第 2 步 —— 检查 Redis Pod 健康状态

```bash
kubectl get pods -l app=redis -n infra
kubectl describe pod <redis-pod> -n infra
```

重点检查 OOMKilled、CrashLoopBackOff 与调度状态。

### 第 3 步 —— 重启或扩容 Redis

```bash
# 单实例 Redis 的情况
kubectl rollout restart deployment/redis -n infra

# Sentinel/Cluster 环境：必要时手动提升副本
redis-cli -h redis-sentinel sentinel failover mymaster
```

### 第 4 步 —— 验证恢复

```bash
redis-cli -h redis.internal info server | grep redis_version
redis-cli -h redis.internal info memory | grep used_memory_human
redis-cli -h redis.internal CONFIG GET maxmemory
```

检查 `redis_connected` 指标回到 1，缓存命中率回升。

### 第 5 步 —— 后续加固

- 设置合适的 `maxmemory-policy allkeys-lru`，避免 OOM
- 配置健康检查：`kubectl edit deployment redis`，补充就绪探针
- 部署 3 个副本的 Redis Sentinel，实现自动故障转移
- 监控 Redis 数据目录的磁盘使用，在 80% 时告警

## 验证

- `redis-cli ping` 返回 PONG
- `redis_connected` 指标稳定在 1.0
- 缓存命中率回到基线（80-95%）
- Redis 恢复后错误率降到 1% 以下
- P95 延迟回到事故前的基线
- 恢复后 1 小时内 Redis Pod 日志中没有 OOM 事件
