# 数据库连接池耗尽

**服务**: api-gateway, payment-service, user-service  
**分类**: database  
**严重级别**: high  
**来源**: SRE 运维手册 — v2.3  

---

## 现象

出现以下现象说明发生了数据库连接池耗尽事故：

- HTTP 503 / 504 错误，报错信息包含 "connection timeout" 或 "pool limit reached"
- 应用日志出现 `sqlalchemy.exc.TimeoutError: QueuePool limit of size N overflow reached`
- Prometheus 指标 `db_connections_active` 等于 `db_connections_max`（饱和率 100%）
- 所有依赖数据库的接口 P95 延迟飙升至 2000ms 以上
- 应用日志中出现请求排队："queuing requests"
- 数据库服务器 CPU 使用率可能因连接频繁创建销毁而升高

## 根因模式

连接池耗尽通常发生在以下情况：

1. **慢查询长期占用连接** —— 一条执行 10 秒以上的 SELECT 会一直占着连接，其他请求无法使用。autovacuum、缺失索引或锁等待是常见诱因。
2. **连接池过小** —— max_connections 配置低于并发峰值需求（例如 500 个并发请求只配了 100 个连接）。
3. **连接泄漏** —— 某段代码打开连接后没有关闭，几分钟到几小时内逐步耗尽连接池。
4. **数据库侧阻塞** —— PostgreSQL 自身达到 max_connections 上限，应用连接池发起的新连接被拒绝。

## 处置步骤

### 紧急缓解（前 5 分钟）

1. **重启受影响的服务 Pod** —— 这会释放所有被占用的连接。用编排工具重启：
   ```
   kubectl rollout restart deployment/api-gateway
   ```
   Pod 重启期间预计有约 30 秒不可用。

2. **临时调大连接池** —— 如果仅靠重启不够，调高 SQLAlchemy 的 `pool_size` 和 `max_overflow`：
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
   为缺失的索引补上索引，或重写长时间占用连接的查询。

4. **排查连接泄漏** —— 检查近期改动过数据库上下文管理器的 PR。确保每个 `get_session()` 都用 `try/finally` 包裹，或采用异步上下文管理器写法。

5. **调整 PostgreSQL 的 `max_connections`**，如果瓶颈在数据库侧上限的话。

## 验证

- 重启后：检查 `db_connections_active` 降到最大值的 80% 以下
- 调大连接池后：确认 error_rate 回落到基线（< 1%）
- 查询 `pg_stat_activity`，确认没有会话在等待锁
- P95 延迟回落到正常范围（< 500ms）
- 对所有数据库接口跑一遍合成健康检查
