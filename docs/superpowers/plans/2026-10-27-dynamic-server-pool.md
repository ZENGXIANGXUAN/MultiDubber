# 动态服务器池（运行中增删节点）设计方案

> 目标：配音流程运行期间，GUI 里新增/移除 TTS 服务器立即生效，不再需要"停掉 → 改列表 → 重开"。

## 1. 现状与根因

当前"必须重启"不是单点问题，而是三层快照 + 三处状态机耦合叠加的结果：

| 位置 | 现状 | 后果 |
| --- | --- | --- |
| `gui.pyw:1204-1208` | `start_processing()` 把 `add_btn` / `settings_btn` 全部禁用 | 运行中根本无法添加 |
| `gui.pyw:451-484` | `WorkerThread` 构造时接收 `server_configs` 快照 | 流程启动后 UI 再改字典也不会传进去 |
| `main.py:757-765` | `MultiServerDispatcher(server_configs=...)` 只在流程开始时构造一次 | 池子生命周期 == 整轮流程 |
| `dispatcher.py:486-509` | `start()` 按 `server_configs` 一次性起固定数量的 GPU worker 线程 | 没有"再加线程"的入口 |
| `dispatcher.py:477` | `_active_servers` 只会在熔断时 `discard`，没有任何 `add` 路径 | 摘除不可逆，只能靠新 dispatcher |
| `dispatcher.py:457-460` | `server_protocols` 在 `__init__` 里被复制成新 dict | 运行中改协议表不会生效 |
| `main.py:1058` | `post_process_fn` 读 main 作用域里的旧 `server_protocols` | 即便加了新节点，后处理也拿不到它的协议（IndexTTS 的 speed 会按 VoxCPM 处理错） |
| `gui.pyw:1086-1095` | 每行的 ✕ 只 `takeItem` + 改 settings.json | 对正在跑的池子零影响 |
| `dispatcher.py:561-566` | 所有 GPU 线程都退出（`all(not t.is_alive())`）就当作异常并 `abort` | 一旦支持"删节点"，这个兜底会误杀 |
| `dispatcher.py:630-636` | `_active_servers` 为空就 `abort` + `on_all_down` | "用户主动清空"会被当成"全部熔断"事故，触发 webhook |

所以要做的不只是"加个 add 方法"，而是把 **dispatcher 从"固定线程集合"改成"可变服务器池"**，并把"用户主动移除"与"节点故障熔断"两种语义拆开。

## 2. 目标与非目标

### 目标

1. 运行中 `add`：探测通过后 ≤1s 内新 worker 加入共享队列，开始分担积压，现有任务零中断。
2. 运行中 `remove`：**优雅下线**——不再领新任务，正在推理的那一次跑完，结果照常进 CPU 后处理队列，然后线程退出并关闭连接。
3. 用户把节点删光：**暂停等待**（任务留在队列里），不是"全部熔断"事故，不触发 `ALL_SERVERS_DOWN`、不发熔断 webhook、不写静音占位。
4. 动态加入的节点，协议和线程数立即正确生效（包含后处理里的 speed 语义）。
5. 设置持久化、`runtime_state`、`/api/pipeline/restart` 的上下文与实时池保持一致。
6. 不运行时、单服务器 CLI 模式、断点续跑行为完全不变。

### 非目标

- 不做跨进程/跨机器的服务器池（`control_server` 只做本机 HTTP 入口，见 P2）。
- 不改重试算法、熔断判定、任务优先级、`status.json` 断点格式。
- 不因为改了 `server_threads_per_server` 就去重排已启动的线程（只对之后新增的节点生效）。
- 不承诺"删节点 = 立刻掐断正在跑的 HTTP 请求"（Python 线程无法安全强杀，见 §7）。

## 3. 方案总览

```
GUI (Qt 主线程)                        业务线程 (WorkerThread / process_srt_files)
──────────────                         ────────────────────────────────────────
Add & Connect ──探测──┐                 MultiServerDispatcher (live pool)
                      │                 ├─ server_configs   url -> threads   ← 可增删
                      └─► add_server() ─┤─ server_protocols url -> protocol  ← 可增删
Remove ✕ ─────────────► remove_server()─┤─ _gpu_workers     url -> [Thread]  ← 按 url 分组
                                        ├─ _worker_stop    url -> Event      ← 下线信号
                                        ├─ _active_servers set               ← 只增/只撤，不 abort
                                        ├─ _pool_state     RUNNING/PAUSED/ABORTED
                                        └─ _gpu_queue (共享 PriorityQueue，pull 模型)

main.post_process_fn ──► dispatcher.get_protocol(url)   # 不再读旧闭包快照
runtime_state       ──► update_job_servers(...)         # 重启上下文同步
```

关键点：队列是 **pull 模型**，worker 自己从共享队列取任务，所以"加线程"天然就能分担积压，不需要调度器改动；"删线程"只需要让它不再取任务并退出。

## 4. 数据结构与生命周期

### 4.1 `MultiServerDispatcher` 新增状态

```python
# __init__ 中
self._spawn_lock   = threading.Lock()        # add/remove/start/stop 串行化
self._server_lock  = threading.RLock()       # 原 Lock 改 RLock，允许回调内再入
self._gpu_workers: Dict[str, List[threading.Thread]] = {}
self._worker_stop: Dict[str, threading.Event] = {}
self._shutdown     = False
self._pool_state   = "RUNNING"               # RUNNING | PAUSED_NO_SERVERS | ABORTED
self._pool_available = threading.Event()     # active 非空时 set
self._pool_available.set()
self._queue_depth_mul = queue_depth_mul      # 动态扩容用
```

`_gpu_worker_threads` 保留为只读属性（`sum(self._gpu_workers.values())` 展平），`stop()`/`run_feeder` 改用它，避免两套账。

### 4.2 模块级"当前活跃池"句柄

GUI 跟业务在同一个进程不同线程，需要拿到正在跑的 dispatcher：

```python
_active_lock = threading.Lock()
_active_dispatcher = None

def set_active_dispatcher(d): ...      # start() 里调用
def clear_active_dispatcher(d): ...    # stop() 里调用（只有自己才清）
def get_active_dispatcher(): ...       # GUI 用
def runtime_add_server(url, protocol, threads) -> bool
def runtime_remove_server(url) -> bool
```

没在跑时返回 `False`，GUI 只更新 UI + settings.json，行为与今天一致。

### 4.3 池状态机

```
                 add_server / 任务成功
   PAUSED_NO_SERVERS ───────────────► RUNNING
          ▲                              │
          │ remove 最后一台              │ 最后一台熔断（非用户操作）
          └──────────────────────────────┴──► ABORTED (on_all_down)
```

- `remove_server` 撤掉最后一个 active → `PAUSED_NO_SERVERS`，**不** abort、**不**回调 `on_all_down`。
- `_mark_server_down` 发现 active 为空：状态是 `PAUSED` 就只记日志；否则走原来的 `ABORTED` 逻辑。
- `add_server` 一定把状态拉回 `RUNNING`。
- 任务成功（`_record_api_success`）不改变状态机，只用于熔断计数。

> 待确认决策 D1：处于 `PAUSED` 时新加的节点如果也熔断，要不要算 `ABORTED`？
> 建议：算 `ABORTED`（与今天"启动时全部连不上"一致，能及时通知），因为探测已经把纯坏地址挡在池外了。

## 5. 关键流程

### 5.1 运行中新增

```python
def add_server(self, url, protocol="voxcpm2", threads=None) -> bool:
    url = normalize_url(url)
    n   = sanitize_threads(threads or self.server_configs.get(url) or DEFAULT)
    with self._spawn_lock:
        if self._shutdown:
            return False
        if url in self._active_servers:          # 幂等
            return False
        self.server_configs[url]   = n
        self.server_protocols[url] = protocol
        with self._server_lock:
            self._active_servers.add(url)
            self._pool_state = "RUNNING"
            self._pool_available.set()
        with _cb_lock:
            _server_states.pop(url, None)        # 关键：重加同 URL 清掉旧熔断计数
        self._worker_stop[url] = threading.Event()
        self._resize_queue_capacity()
        threading.Thread(target=self._spawn_server_workers,
                         args=(url, protocol, n),
                         name=f"POOL-ADD-{tag(url)}", daemon=True).start()
    _log("POOL_ADD", f"{url} [{protocol}] ×{n} 已加入运行中的池")
    if self._on_pool_changed: self._on_pool_changed(self.pool_snapshot())
    return True
```

`_spawn_server_workers` 沿用 `start()` 里 0.3s 错峰起线程的逻辑（Cloudflare 场景避免 SSL 风暴），并在每次循环检查 `_shutdown` / `_worker_stop[url]`，支持"加到一半又被删"：

```python
for i in range(n):
    if self._shutdown or self._worker_stop[url].is_set():
        break
    t = threading.Thread(target=self._gpu_worker_loop, args=(url, protocol),
                         name=f"GPU-{tag(url)}-{i}", daemon=True)
    t.start()
    self._gpu_workers.setdefault(url, []).append(t)
    time.sleep(0.3)
```

前置条件：**只有 `api_client.test_connection` 探测成功的节点才允许进池**（复用 `_on_server_check_done` 的 success 分支）。这样坏隧道不会先进池再被熔断，白占 6 次重连计数。

### 5.2 运行中移除（优雅 drain）

```python
def remove_server(self, url) -> bool:
    url = normalize_url(url)
    with self._spawn_lock:
        if url not in self._active_servers and url not in self.server_configs:
            return False
        with self._server_lock:
            self._active_servers.discard(url)
            self._pool_available.clear() if not self._active_servers else None
            if not self._active_servers:
                self._pool_state = "PAUSED_NO_SERVERS"
        self.server_configs.pop(url, None)       # 不再参与容量/日志
        # server_protocols 故意保留：在途任务的后处理还要按它算 speed
        ev = self._worker_stop.get(url)
        if ev: ev.set()
    threading.Thread(target=self._join_workers, args=(url,), daemon=True,
                     name=f"POOL-DRAIN-{tag(url)}").start()
    _log("POOL_REMOVE", f"{url} 已摘除：不再派新任务，在途任务完成后退出")
    return True

def _join_workers(self, url, timeout=600):
    for t in self._gpu_workers.get(url, []):
        t.join(timeout)                          # 后台线程里 join，绝不阻塞 Qt 主线程
    with self._spawn_lock:
        self._gpu_workers.pop(url, None)
        self._worker_stop.pop(url, None)
    _log("POOL_REMOVED", f"{url} worker 已全部退出")
```

worker 侧的配合（`_gpu_worker_loop`）：

1. 阻塞取任务从 `get(timeout=IDLE_WARN_THRESHOLD_S)` 改成 `get(timeout=0.5)`，让 drain 信号最多 0.5s 被感知；空闲 15s 的 `WORKER_IDLE` 日志改为按累计空闲时间节流，不丢原有观测。
2. 循环顶部检查 `_abort_event` / `_shutdown` / `stop_event` / `url in _active_servers`，任一成立就退出。
3. **在途任务不取消**：`_call_api_on_server` 正常返回后再走原有分支入 CPU 队列；异常则走统一的重入队 helper。
4. 退出前把 `local_retry_task` 放回队列（现有逻辑保留），并 `finally: _close_thread_clients(url)` 关闭本线程缓存的 gradio `Client`，避免连接池泄漏。
5. 注册表摘除由 `_join_workers` 负责，worker 自己不动列表（避免并发改 list）。

### 5.3 空池暂停（核心语义修正）

`run_feeder` 的等待循环增加池状态判断：

```python
while not (self._abort_event.is_set() or abort_flag_fn()):
    with self._server_lock:
        no_servers = not self._active_servers
    if no_servers:
        if self._log_pause_once:
            _log("POOL_PAUSED", f"无可用服务器，{self._gpu_queue.qsize()} 个任务挂起等待节点加入")
            self._log_pause_once = False
        self._pool_available.wait(0.5)           # add_server 会 set
        continue
    if self._all_done_event.wait(0.5):
        break
```

同时把原来"所有 GPU 线程都死了 → `abort_event.set()`"的兜底改为：

- active 非空但线程全死（异常退出）→ 仍是异常，保持原 abort。
- active 为空 → 走 pause 分支，不 abort。

### 5.4 协议与后处理

`main.process_srt_files` 里 `post_process_fn` 改为向 dispatcher 要实时协议：

```python
proto = dispatcher.get_protocol(server_url) if dispatcher else server_protocols.get(server_url, "voxcpm2")
```

`dispatcher.get_protocol` 返回 `server_protocols.get(url, "voxcpm2")`，读锁保护。这样运行中加入 IndexTTS 节点，`effective_speed` 才会用它自己的 speed，而不是被当成 VoxCPM 强制 1.0。

### 5.5 队列容量

容量从 `sum(threads) * queue_depth_mul` 而来，加入节点时按差值扩容：

```python
def _resize_queue_capacity(self):
    target = max(4, sum(self.server_configs.values()) * self._queue_depth_mul)
    self._gpu_queue.maxsize = target     # CPython 的 Queue.put 每轮读取该属性，安全
```

缩容同理，下限 `max(4, qsize())`。容量只影响背压和内存占用（`task_iterator` 会为每个任务切参考音频），不扩也不会死锁，属于可选项。

### 5.6 持久化与状态同步

- `_on_server_check_done` 成功后照旧 `_save_current_settings()`（`servers` / `server_protocols` 本来就是按 UI 状态生成的），所以动态增删自动落盘。
- `runtime_state` 新增 `update_job_servers(configs, protocols)`、`mark_server_removed(url)`、`pool_state` 字段；dispatcher 通过 `on_pool_changed` 回调把它们同步过去，保证 `POST /api/pipeline/restart` 用的是最新池，而不是流程启动时的快照。
- 日志新增 `POOL_ADD` / `POOL_REMOVE` / `POOL_PAUSED` / `POOL_RESUME` 四类事件，GUI 日志区可见。

## 6. 代码改动清单

### `dispatcher.py`（主体，~270 行）

| 改动 | 说明 |
| --- | --- |
| 新增模块级活跃池注册表 + `runtime_add_server` / `runtime_remove_server` | GUI 入口，未运行时返回 False |
| `__init__` | `_gpu_workers` / `_worker_stop` / `_spawn_lock` / `_pool_state` / `_pool_available` / `_shutdown`；`_server_lock` 改 RLock；不再复制传入的 protocol 字典（或明确以 dispatcher 为准） |
| `start()` | 改为委托 `_spawn_server_workers`；注册活跃池 |
| 新增 `add_server` / `remove_server` / `_spawn_server_workers` / `_join_workers` / `_resize_queue_capacity` / `get_protocol` / `pool_snapshot` | 核心 |
| `_gpu_worker_loop` | 0.5s 轮询、退出条件、关闭线程本地 Client、失败路径抽 `_requeue_or_hold(task, reason)` |
| `run_feeder` | 空池暂停 + 兜底判定修正 + 单次暂停日志 |
| `stop()` | 遍历 `_gpu_workers`；先置 `_shutdown`、set 所有 stop event，再送 sentinel；唤醒 feeder |
| `_mark_server_down` | 锁内只做状态变更，回调移到锁外；空池时区分 PAUSED / ABORTED |

### `main.py`（~15 行）

- `post_process_fn` 用 `dispatcher.get_protocol(...)`。
- 构造 dispatcher 时传 `on_pool_changed`，同步 `runtime_state`。
- `_on_all_down` 保持原样（只服务"真熔断"路径）。

### `gui.pyw`（~40 行）

- `start_processing`：不再禁用 `add_btn`；`add_folder_btn` / `settings_btn` 仍禁用（避免中途改文件夹/重试参数）。
- `_on_server_check_done` 成功分支：`if dispatcher.runtime_add_server(url, proto, threads): 日志`；返回 False 时静默（未运行 / 已在池中）。
- `_remove_server_item`：先 `dispatcher.runtime_remove_server(url)`，再删 UI 行；日志注明"优雅下线，在途任务完成后退出"。
- 行内 ✕ 在运行中保持可用；如果该 URL 不在池中（离线/从未启动）则只删 UI。
- 可选：状态栏 `QTimer(2s)` 显示 `节点 N / 线程 M / 队列 Q`。

### `control_server.py`（P2，可选，~50 行）

```
GET    /api/pool
POST   /api/pool/servers      {"url": "...", "protocol": "indextts_15"}
DELETE /api/pool/servers?url=...
```

仅 127.0.0.1，便于外部脚本（例如批量开 Cloudflare 隧道）热插拔。不复用 `/api/pipeline/restart`。

## 7. 竞态与边界清单

| 场景 | 处理 |
| --- | --- |
| 重复添加同一 URL | 幂等返回 False，日志 `POOL_EXISTS` |
| 添加到一半又删除 | spawner 每轮检查 stop event，最多多起 1 个线程后自行退出 |
| 删除正在推理的节点 | 等在途完成，结果照常入 CPU 队列；日志给出 `draining` 提示 |
| 删除后立刻重加同一 URL | `add_server` 清 `_server_states[url]`，新线程新连接，熔断计数从 0 开始 |
| 同一 URL 换协议 | 先 remove 再 add；`server_protocols` 覆盖写 |
| 删除最后一个节点 | `PAUSED_NO_SERVERS`，队列保留，不 abort、不发熔断通知；`START DUBBING` 仍显示运行中 |
| 删光后再加节点 | `POOL_RESUME`，被挂起的任务继续，无需重跑已完成的片段 |
| 节点探测失败 | 不入池；UI 标红，运行中的池不受影响 |
| 删除节点后它在途调用失败 | `_mark_server_down` 的 `url not in _active_servers` 早退，不会误报"服务器下线" |
| 队列满 + 池空 | feeder 的 `put(timeout=0.5)` 自旋，加节点后自动恢复；30s 打一次 `POOL_STALL` |
| `stop()` 与 add 并发 | `_shutdown` 先置位，spawner 检查后不再起线程 |
| 线程本地连接泄漏 | worker 退出 `finally` 里 `client.close()` 并清 `client_cache[url]` |
| GUI 卡死 | add/remove 只做状态变更 + 起后台线程；join 一律在 daemon 线程 |
| 回调重入死锁 | `_server_lock` 改 RLock；`_on_server_down` / `_on_pool_changed` 在锁外调用 |

## 8. 分阶段实施

### P1（核心，1–2 天）

1. `dispatcher.py` 池化：状态结构、`add_server` / `remove_server`、worker 退出条件、`stop()` 适配。
2. `run_feeder` 空池暂停 + 兜底判定修正。
3. `main.py` 协议实时查询 + `on_pool_changed`。
4. `gui.pyw` 运行中 add/remove 接线（复用现有探测线程）。
5. 手工回归：12 台 Cloudflare 隧道跑一个长文件，中途加 2 台 / 删 2 台。

### P2（可观测性与运维，半天）

6. `pool_snapshot` + GUI 状态栏 + `POOL_*` 日志统一。
7. `runtime_state` 同步与 `/api/pipeline/status` 增加池信息。
8. `control_server` 池接口（可选）。
9. `server_threads_per_server` 对新增节点生效（已在 `add_server` 参数里）。

### P3（可选，不建议先做）

10. 已熔断节点的定时复探与自动回池（需要定义"复探不打断在途任务"）。
11. 硬取消在途请求（关闭 httpx transport），风险高：可能污染连接池/触发半包，收益低。

## 9. 验收用例

### pytest（`tests/test_dynamic_pool.py`，仓库当前没有 tests/，需新建）

用 `monkeypatch` 把 `dispatcher._call_api_on_server` 换成受 Event 控制的假实现：

1. `test_add_server_worker_consumes_queue`：1 台 1 线程喂 4 个任务，卡住第一个；`add_server` 后 3 个在 1s 内被第二台消费；`_total_done == 4`。
2. `test_remove_server_drains_inflight`：在途任务阻塞时 `remove_server`，释放后断言结果进了 CPU 队列、线程退出、后续任务不再落到该 URL。
3. `test_remove_last_server_pauses_without_abort`：删光后 `_abort_event` 未 set、`_all_done_event` 未 set、`all_servers_down is False`；`add_server` 后完成。
4. `test_readd_clears_breaker_state`：手工把 `consecutive_failures` 拉到阈值，remove + add 后从 0 开始。
5. `test_add_while_spawning_then_remove`：压 add/remove 交错 20 轮，无死锁、无丢任务（`submitted == done`）。
6. `test_protocol_lookup_for_dynamic_server`：动态加 indextts 节点后 `get_protocol` 返回新值。

### 手工

- 运行中加入同协议节点：日志立刻出现 `POOL_ADD`，任务耗时下降，进度条不回退。
- 运行中删除某节点：该节点不再出现在后续任务的 `[server_tag]` 里；其最后一个任务正常完成。
- 删除全部：出现 `POOL_PAUSED`，进程不退出、无熔断弹窗/webhook；再加一台后 `POOL_RESUME` 继续。
- 删除后重加同一 URL：连接正常，不被旧熔断计数立刻踢掉。
- 停止按钮 / 关闭窗口：所有新老 worker 都退出，无残留线程。

## 10. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 线程生命周期变复杂，回归面变大 | 所有增删都在 `_spawn_lock` 下；worker 只在循环边界退出，不在调用中途改状态 |
| 误把"用户删光"当故障 | 显式池状态机 + `remove_server` 不触发 `on_all_down` |
| `_server_states` 全局表残留 | add/remove 时同步清理；保留 reason 日志 |
| 队列 `maxsize` 动态修改 | 只影响背压；缩容下限 `max(4, qsize())`，不同步也只是多占内存 |
| 连接泄漏 | worker 退出 `finally` 关闭 Client，删除后 daemon join 兜底 |
| 需要一键回到旧行为 | `dispatcher.LIVE_POOL_ENABLED = True` 开关：关闭时 `runtime_add_server/remove` 直接返回 False，UI 行为回到"下次启动生效" |

## 11. 待确认的决策点

- **D1**：`PAUSED` 状态下新加的节点如果也熔断，是否算 `ABORTED`？建议：算（探测已挡掉坏地址）。
- **D2**：运行中是否允许改 `server_threads_per_server`？建议：不允许（`settings_btn` 继续禁用），只对后续新增节点用当前值。
- **D3**：是否需要 `control_server` 的池接口？建议：P2 再做，先保证 GUI 路径稳定。
- **D4**：删除节点后 `server_protocols` 是否清理？建议：不清理（在途任务的后处理仍需要），地图规模远小于内存压力。
