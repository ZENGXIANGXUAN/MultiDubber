# 熔断机制重设计：按服务器追踪连续重连失败

## 问题

当前 `dispatcher.py` 的熔断逻辑基于**单个任务**的 `retry_count`：当一个任务重试次数耗尽，标记最后执行它的服务器下线。但失败任务放回全局队列后可能在多台服务器间轮转，最后一棒的服务器被随机判定下线，与该服务器是否健康无关。

## 设计

### 核心思路

每台服务器独立维护状态。以"错误触发的重连"为事件节点，用"重连间有没有成功产出"判定有效性。连续 N 次无效重连 → 熔断。

### 两种重连的区分

| 触发方式 | 位置 | 含义 | 计入连续失败 |
|---------|------|------|------------|
| 错误重连 | `_call_api_on_server` 第 170 行 | API 调用失败后 `invalidate_client` | ✅ |
| 空闲重连 | `_gpu_worker_loop` 第 461-468 行 | Worker 空闲 60s 预防性重建 | ❌ |

### 数据结构

```python
class _ServerState:
    error_reconnect_pending: bool    # 是否有待评估的错误重连
    tasks_since_reconnect: int       # 上次重连后的成功任务数
    consecutive_failures: int        # 连续无效重连计数
```

### 状态转换

1. **API 调用失败** → `invalidate_client` → 设置 `error_reconnect_pending = True`
2. **API 调用成功** → `tasks_since_reconnect += 1`
3. **下次 `_get_client` 建立新连接**（错误重连触发）：
   - 如果 `error_reconnect_pending`：
     - `tasks_since_reconnect == 0` → `consecutive_failures += 1`
     - `tasks_since_reconnect > 0` → `consecutive_failures = 0`
     - 重置 `tasks_since_reconnect = 0`，`error_reconnect_pending = False`
4. **`consecutive_failures >= N`** → `_mark_server_down` → 熔断

### 边界情况

- **首次连接失败**（Worker 启动预热）：算一次失败
- **多 Worker 共享服务器**：状态 per-thread（连接是 per-thread 的），熔断标记全局
- **N 值**：默认 3，可配置
- **空闲重连**：不参与连续计数，避免误判

### 改动范围

仅修改 `dispatcher.py`：
- 新增 `_ServerState` 类
- 新增 per-server 状态存储（`threading.local` 或 dict）
- 修改 `_call_api_on_server`：失败时标记 `error_reconnect_pending`
- 修改 `_gpu_worker_loop`：成功时更新 `tasks_since_reconnect`，重连时评估连续失败
- 修改 `_get_client`：建新连接时触发评估逻辑
- 移除旧的基于 `retry_count` 的熔断逻辑
- 新增 `circuit_breaker_threshold` 构造参数（默认 3）
