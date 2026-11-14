# 数据库连接池耗尽

**服务**: api-gateway, payment-service, user-service  
**分类**: database  
**严重级别**: high  
**来源**: SRE 运维手册 — v2.3  

---

## 现象

识别信号：

- HTTP 503 / 504 错误，报错信息包含 "connection timeout" 或 "pool limit reached"
- 应用日志出现 `sqlalchemy.exc.TimeoutError: QueuePool limit of size N overflow reached`
- Prometheus 指标 `db_connections_active` 等于 `db_connections_max`（饱和率 100%）
- 所有依赖数据库的接口 P95 延迟升至 2000ms 以上
- 应用日志中出现请求排队："queuing requests"
- 数据库服务器 CPU 使用率可能因连接频繁创建销毁而升高

## 根因模式

连接池耗尽通常发生在以下情况：

1. **慢查询长期占用连接**：执行时间超过 10 秒的 SELECT 会持续占用连接，其他请求无法使用。autovacuum、缺失索引或锁等待是常见诱因。
2. **连接池过小** —— max_connections 配置低于并发峰值需求（例如 500 个并发请求只配了 100 个连接）。
3. **连接泄漏**：代码打开连接后未关闭，连接池在数分钟至数小时内逐步耗尽。
4. **数据库侧阻塞** —— PostgreSQL 自身达到 max_connections 上限，应用连接池发起的新连接被拒绝。

## 处置步骤

### 紧急缓解（前 5 分钟）

1. **重启受影响的服务 Pod**：释放已占用连接。使用编排工具重启：
   ```
   kubectl rollout restart deployment/api-gateway
   ```
   Pod 重启期间预计有约 30 秒不可用。

2. **临时调大连接池**：若重启后仍不足，调高 SQLAlchemy 的 `pool_size` 和 `max_overflow`：
   ```
   pool_size=100  # 原为 50
   max_overflow=20  # 原为 10
   pool_pre_ping=True
   ```
   需要滚动重启才能加载新配置。

### 根因修复（服务恢复健康后）

3. **定位并优化慢查询**：
   ```sql
   SELECT query, calls, total_time, mean_time
   FROM pg_stat_statements
   ORDER BY total_time DESC
   LIMIT 20;
   ```
   补充缺失索引，或重写长时间占用连接的查询。

4. **排查连接泄漏**：检查近期变更过数据库上下文管理器的 PR。确保每个 `get_session()` 使用 `try/finally` 或异步上下文管理器。

5. **调整 PostgreSQL 的 `max_connections`**：若瓶颈位于数据库侧连接上限。

## 验证

- 重启后：检查 `db_connections_active` 降到最大值的 80% 以下
- 调大连接池后：确认 error_rate 回落到基线（< 1%）
- 查询 `pg_stat_activity`，确认没有会话在等待锁
- P95 延迟回落到正常范围（< 500ms）
- 对所有数据库接口执行一轮合成健康检查
