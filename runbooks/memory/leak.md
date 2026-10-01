# 内存泄漏 / OOM Kill

**服务**: api-gateway, user-service  
**分类**: memory  
**严重级别**: critical  
**来源**: SRE 运维手册 — v2.2  

---

## 内存泄漏的现象

内存持续增长，最终必然导致故障：

- `process_memory_mb` 指标随时间单调增长（例如每分钟 +120 MB）
- `memory_percent` 达到并持续高于 90%
- 日志出现 `OOM killer: process killed by memory pressure (exit code 137)`
- 进程重启次数增加：`restart_count_1h >= 2`
- GC 回收耗时显著增加：`gc_collection_time` 的 p99 超过 300ms（正常 <50ms）
- 日志：`GC unable to reclaim memory — retained heap growth observed`
- 流量没有相应增长，无法解释内存的增长
- 堆内存 profile 显示某个特定数据结构里存在 "unreleased references"

## 根因模式

内存泄漏几乎都是最近改动引入的代码级问题：

1. **缓存无界增长** —— 内存缓存没有 TTL 或大小上限，条目无限累积
2. **引用未释放** —— 某个 list/dict 从不清理，导致旧对象一直存活（例如请求历史、事件日志）
3. **闭包捕获** —— 函数闭包持有了本应释放的大对象引用
4. **线程局部存储** —— 每线程缓存随线程数增长
5. **连接/流泄漏** —— 打开的文件、socket 或流在异常路径上没有释放
6. **缓存层有 bug** —— 新功能加了请求缓存却没做 TTL："为本请求上下文缓存加上 10min TTL" —— 但实现时忘了 TTL

## 处置步骤

### 紧急缓解

1. **重启服务** —— 回收内存最快的方式：
   ```bash
   kubectl rollout restart deployment/api-gateway
   ```
   这能争取排查时间，但泄漏还会复发。

2. **扩容**（临时）—— 增加副本数，让每个实例处理的请求更少：
   ```bash
   kubectl scale deployment/api-gateway --replicas=6  # 原为 3
   ```

3. **调高内存上限**（短期）—— 仅在重启争取到的时间不够时使用：
   ```yaml
   resources:
     limits:
       memory: 4Gi  # 原为 2Gi
   ```
   这只是治标不治本。修复后要去掉。

### 根因定位

4. **重启前抓取堆转储**（如果可能）：
   ```bash
   # Java/JVM 服务
   jmap -dump:format=b,file=heap.hprof <pid>
   
   # Python
   import tracemalloc
   tracemalloc.start()
   # ... 让它跑一会儿 ...
   snapshot = tracemalloc.take_snapshot()
   ```

5. **用生产流量回放做 profiling**：
   ```bash
   # 在预发环境以 1x 速度回放生产请求
   # 观察内存增长 —— 如果能复现，说明泄漏是确定性的
   ```

6. **二分定位最近的 commit**：
   ```bash
   git log --oneline -20
   # v2.4.0 引入了 RequestContext 缓存 —— 可疑 commit
   # 部署 v2.3.5，看内存是否保持平稳
   ```

7. **修复 bug**：
   ```python
   # 修复前（泄漏）
   _request_cache: dict[str, RequestContext] = {}
   
   def handle_request(request):
       ctx = RequestContext(request)
       _request_cache[request.id] = ctx  # 从不清理！
   
   # 修复后（已修）—— 使用 TTL 缓存
   from cachetools import TTLCache
   _request_cache: TTLCache[str, RequestContext] = TTLCache(maxsize=1000, ttl=600)
   ```

## 验证

部署修复后：

- `process_memory_mb` 保持稳定，或呈现正常的 GC 周期
- `restart_count_1h` 回到 0
- 24 小时内 Pod 日志中不再出现 OOM kill
- GC 回收耗时回到基线（p99 <50ms）
- 预发环境压测 1 小时，内存 profile 显示堆增长平稳
- 代码评审确认已落地 TTL 或有界缓存
