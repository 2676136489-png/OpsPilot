# 内存泄漏与 OOM

**服务**: api-gateway, user-service  
**分类**: memory  
**严重级别**: critical  
**来源**: SRE 运维手册 — v2.2  

---

## 识别信号

内存持续增长并最终导致故障，典型信号包括：

- `process_memory_mb` 指标随时间单调增长（例如每分钟 +120 MB）
- `memory_percent` 达到并持续高于 90%
- 日志出现 `OOM killer: process killed by memory pressure (exit code 137)`
- 进程重启次数增加：`restart_count_1h >= 2`
- GC 回收耗时显著增加：`gc_collection_time` 的 p99 超过 300ms（正常 <50ms）
- 日志：`GC unable to reclaim memory — retained heap growth observed`
- 流量未同步增长，无法解释内存增长
- 堆内存 profile 显示某个特定数据结构里存在 "unreleased references"

## 根因模式

内存泄漏通常由近期代码变更引入：

1. **缓存无界增长**：内存缓存缺少 TTL 或容量上限，条目持续累积
2. **引用未释放**：list/dict 未清理，导致旧对象长期存活（例如请求历史、事件日志）
3. **闭包捕获**：函数闭包持有本应释放的大对象引用
4. **线程局部存储**：每线程缓存随线程数增长
5. **连接或流泄漏**：文件、socket 或流在异常路径未释放
6. **缓存实现缺陷**：新增请求缓存未设置 TTL，导致条目持续累积

## 处置步骤

### 紧急缓解

1. **重启服务**：快速回收内存：
   ```bash
   kubectl rollout restart deployment/api-gateway
   ```
   该操作可暂时恢复服务，但泄漏仍会复发。

2. **临时扩容**：增加副本数，降低单实例请求量：
   ```bash
   kubectl scale deployment/api-gateway --replicas=6  # 原为 3
   ```

3. **临时提高内存上限**：仅在重启无法维持服务时使用：
   ```yaml
   resources:
     limits:
       memory: 4Gi  # 原为 2Gi
   ```
   该措施仅用于临时缓解，根因修复后应恢复原配置。

### 根因定位

4. **重启前采集堆转储**（如条件允许）：
   ```bash
   # Java/JVM 服务
   jmap -dump:format=b,file=heap.hprof <pid>
   
   # Python
   import tracemalloc
   tracemalloc.start()
   # ... 运行一段时间 ...
   snapshot = tracemalloc.take_snapshot()
   ```

5. **使用生产流量回放进行性能分析**：
   ```bash
   # 在预发环境以 1x 速度回放生产请求
   # 观察内存增长；若可复现，说明泄漏具有确定性
   ```

6. **通过二分法定位近期变更**：
   ```bash
   git log --oneline -20
   # v2.4.0 引入 RequestContext 缓存，属于疑似变更
   # 部署 v2.3.5，看内存是否保持平稳
   ```

7. **修复 bug**：
   ```python
   # 修复前（存在泄漏）
   _request_cache: dict[str, RequestContext] = {}
   
   def handle_request(request):
       ctx = RequestContext(request)
       _request_cache[request.id] = ctx  # 未清理
   
   # 修复后：使用 TTL 缓存
   from cachetools import TTLCache
   _request_cache: TTLCache[str, RequestContext] = TTLCache(maxsize=1000, ttl=600)
   ```

## 验证

修复部署后：

- `process_memory_mb` 保持稳定，或呈现正常的 GC 周期
- `restart_count_1h` 回到 0
- 24 小时内 Pod 日志无 OOM 终止记录
- GC 回收耗时回到基线（p99 <50ms）
- 预发环境压测 1 小时，内存 profile 显示堆增长平稳
- 代码评审确认已落地 TTL 或有界缓存
