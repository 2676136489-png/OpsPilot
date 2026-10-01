# 数据库死锁

**服务**: payment-service, order-service  
**分类**: database  
**严重级别**: medium  
**来源**: SRE 运维手册 — v2.1  

---

## 现象

死锁的表现和连接池耗尽不一样：

- 应用日志出现 PostgreSQL 的死锁检测消息：`ERROR: deadlock detected`
- 特定事务 ID 显示回滚：`DETAIL: Process X waits for ShareLock on transaction Y`
- 错误率飙升但延迟**不**飙升（这点和连接池耗尽不同）
- 重试风暴 —— 客户端在死锁回滚后重试，进一步加剧压力
- `pg_stat_activity` 显示多个会话同时卡在 `Lock` 状态

## 根因模式

当两个或多个事务以互相矛盾的顺序获取锁时，就会形成死锁：

```
Transaction A: UPDATE orders SET ... WHERE id=1;   -- 持有 order 1 的锁
Transaction B: UPDATE orders SET ... WHERE id=2;   -- 持有 order 2 的锁
Transaction A: UPDATE orders SET ... WHERE id=2;   -- 等待 B
Transaction B: UPDATE orders SET ... WHERE id=1;   -- 等待 A → 死锁
```

常见原因：

1. **锁顺序不一致** —— 不同代码路径的加锁顺序不同（例如接口 1 先更新 orders 再更新 payments，接口 2 先更新 payments 再更新 orders）
2. **间隙锁（Gap Lock）** —— 并发的 INSERT 落入索引区间
3. **外键检查** —— 一处锁父行，另一处同时在锁子行
4. **长事务** —— 持锁时间长会扩大死锁窗口

## 处置步骤

### 检测

1. 查询当前活跃的锁：
   ```sql
   SELECT blocked.pid AS blocked_pid,
          blocked.query AS blocked_query,
          blocker.pid AS blocker_pid,
          blocker.query AS blocker_query
   FROM pg_locks blocked
   JOIN pg_locks blocker ON blocked.locktype = blocker.locktype
   WHERE NOT blocked.granted AND blocker.granted;
   ```

2. 开启死锁日志：`ALTER SYSTEM SET log_lock_waits = on; ALTER SYSTEM SET deadlock_timeout = '1s';`

### 紧急处置

3. **PostgreSQL 的牺牲者选择机制** —— PostgreSQL 会自动杀掉死锁环中最年轻的事务。如果因级联重试而不够用：
   - 手动杀掉阻塞方：`SELECT pg_terminate_backend(<pid>);`
   - 扩容只读副本，把读流量分担出去

### 长期预防

4. **统一加锁顺序** —— 检查所有 UPDATE/DELETE 路径，确保它们访问表的顺序始终一致（例如始终先 orders 后 payments，绝不反过来）。

5. **保持事务简短** —— 把非关键操作（发邮件、统计分析）移出事务边界。

6. **对队列式处理使用 SELECT ... FOR UPDATE SKIP LOCKED**，避免等待被锁的行。

7. **在客户端加上带指数退避的重试**：
   ```
   重试 3 次，延迟：100ms → 200ms → 400ms
   ```

## 验证

- 未来 24 小时内死锁率降到零
- `pg_stat_statements` 中不再出现 `deadlock` 错误
- 修复后更新操作的延迟分位数保持稳定
- 所有重试路径都在预发环境验证通过
