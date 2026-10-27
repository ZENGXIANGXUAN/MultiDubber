# VOCTTS 优化清单（动态服务器池之外）

> 备份点：`607501e` / tag `backup-20261027-2028`
> 本文只记录动态服务器池方案之外、在通读代码与实测后确认有实际收益的点。
> 标注"实测"的数字来自本机 3.12.10 + pydub/soundfile 的合成数据基准，脚本见 §6。

## 0. 结论速览

| # | 问题 | 影响 | 改动量 | 建议批次 |
|---|---|---|---|---|
| B1 | `merge_audio` 用 `+=` 逐段拼接，O(n²) 拷贝 | 20 分钟音频 6.8s → 0.37s（18.5×）；2 小时预计 ~27s+ 及 2× 峰值内存 | 40 行 | P1 |
| B2 | 参考音频抽取成 44.1k 立体声 | 2 小时视频常驻 1.27GB → 345MB | 1 行 ffmpeg 参数 | P1 |
| B3 | 用 `AudioSegment.from_file` 只为读 WAV 时长 | 2 小时 WAV 峰值 ~1.27GB → `sf.info` ~0ms/近零内存 | 10 行 | P1 |
| A1 | `_GpuTask.__lt__` 与哨兵比较会 AttributeError | `stop()` 与喂任务并发时可能崩 feeder | 2 行 | P0 |
| A2 | `invalidate_client` 弹出 Client 但不 close | 网络抖动后连接池/套接字残留 | 5 行 | P0 |
| A3 | `stop()` 逐线程 `join(3s)` | 24 个 worker 最坏 72s 退出 | 10 行 | P0 |
| A4 | `start()` 用 `sleep(0.3)` 阻塞起线程 | 24 个 worker 首次任务延迟 7.2s | 20 行 | P0 |
| C1 | gradio Client 读超时为 `None` | 挂死节点永久占用 worker（只打日志） | 1 行 | P1 |
| C2 | GUI 日志逐行 append、无上限 | 长跑内存增长 + 事件队列洪泛 | 20 行 | P2 |
| C3 | `_check_threads` 只增不减 | 每次增删服务器泄漏一个 QThread 对象 | 3 行 | P2 |
| C4 | `save_status` 非原子写 | 关闭/崩溃恰好写一半 → 进度丢失（可靠 WAV 重扫兜底） | 8 行 | P2 |
| C5 | 日志无轮转 | `logs/` 无限增长 | 10 行 | P3 |
| C6 | 每次合并都跑 `ffmpeg -version` | 每文件多一次子进程 | 3 行 | P3 |
| C7 | 网络重试用 `sleep` 占住 worker | 抖动时并发度骤降 | 30 行 | P2 |
| D1 | 文件级串行：本文件全部 post-process 完才喂下一个文件 | 短文件尾部利用率低 | 设计级 | P3 |
| D3 | 死代码 | 维护噪音 | 删除 | P3 |

P0 与动态池同一批做（都在 dispatcher，顺手且低风险）；P1 是纯收益，建议紧随其后；P2/P3 按需。

## 0.1 实现状态（2026-10-27）

已落地并跑过回归（`tests/`）：

- **A1** 哨兵比较、**A2** `invalidate_client` 关闭连接、**A3** `stop()` 全局截止、**A4** 握手闸门（start/add 不再 sleep 阻塞）。
- **B1** `merge_audio` 一次拼接（实测 20 分钟 6.82s → 0.37s，18.5×）。
- **B2** 参考音频改为 24kHz 单声道。
- **B3** WAV 时长检查改用 `soundfile.info`。
- **C1** gradio Client 读超时 900s。
- **C3** `_check_threads` 结束后移除。
- **C4** `save_status` 原子写 + 紧凑 JSON。
- **C6** `check_ffmpeg` 结果缓存。
- **C2** 只做了 `QTextEdit` 行数上限；按 200ms 批量刷新日志尚未做。
- 未做：**C5** 日志轮转、**C7** 延迟重试队列、**D1–D4**。

注意 B1 的口径修正：新旧实现对**总时长**毫秒级一致；旧实现的间隙静音是 11025Hz 再重采样，
每个间隙会少 1–2 帧（<0.1ms），所以混合采样率/多间隙场景下不是逐字节相同，新实现按目标帧率精确补齐。

## 1. P0：与动态池同批修掉

### A1. 哨兵与任务的比较不对称（潜在崩溃）

`dispatcher.py:404-408` 的 `_GpuTask.__lt__` 直接访问 `other.is_requeued`；`_Sentinel.__lt__` 恒为 True。
一旦 `stop()` 把哨兵放进堆，而 feeder 仍在 `put`（abort 检查与 put 不是原子的），`heapq` 会执行
`_GpuTask < _Sentinel` → `AttributeError`，feeder 线程直接挂掉，整轮任务卡死。

```python
def __lt__(self, other):
    if not isinstance(other, _GpuTask):   # 哨兵永远排最前
        return False
    ...
```

### A2. `invalidate_client` 不关闭旧 Client

`dispatcher.py:83-95` 只做 `pop`。CPython 引用计数会回收，但 httpx 连接池里的 keep-alive 连接
不会被显式关闭，Cloudflare 侧表现为悬挂连接；大量重连后本地端口也会短时堆积。

```python
def invalidate_client(url, error=True):
    cache = getattr(_thread_local, "client_cache", None)
    client = cache.pop(url, None) if cache else None
    if client is not None:
        try: client.close()
        except Exception: pass
    if error: _mark_error_reconnect(url)
```

### A3. `stop()` 的逐线程等待

`dispatcher.py:581-582` 对每个 worker `join(timeout=3)`。24 个 worker 里只要有几个卡在推理，
退出就会串行等十几秒到几十秒。改成共享截止时间：

```python
deadline = time.time() + 5.0
for t in all_threads:
    t.join(max(0.0, deadline - time.time()))
```

### A4. 启动错峰阻塞主流程

`dispatcher.py:509` 每起一个 worker 就 `sleep(0.3)`，12 台 ×2 线程 = 7.2s，期间 feeder 还没开始喂任务。
把错峰下沉到 worker 内部：线程立刻创建，握手前抢一个全局闸门（`_connect_gate`，间隔 0.3s），
`start()` 立即返回。动态加节点时同样复用这个闸门。

## 2. P1：吞吐与内存（实测收益最大）

### B1. `merge_audio` 的 O(n²) 拼接

`audio_processor.py:298-319` 对每段执行 `merged_audio += segment`；pydub 的 `append` 是
`_spawn(seg1._data + seg2._data)`，每次都要把**已累积的全部音频**重新拷一遍，且临时分配 2× 内存。

实测（1s/段、24kHz 单声道、无日志）：

| 段数 | 成品时长 | 现实现 | 一次性 join | 倍率 |
|---|---|---|---|---|
| 600 | 10 min | 1.77s | 0.18s | 9.9× |
| 1200 | 20 min | 6.82s | 0.37s | **18.5×** |
| 2400（外推） | 40 min | ~27s | ~0.75s | ~36× |

两次运行输出 `raw_data` 完全一致。2 小时、44.1k 立体的场景还会叠加 2× 峰值内存。

改法：先扫一遍 header 求目标格式（`sf.info`，2400 个约 0.2s），再逐段 `raw_data` 收集、缺口补零字节，
最后 `AudioSegment(data=b"".join(chunks), ...)` 一次性构造。保留"取最大 channels/frame_rate/sample_width"
的现有语义（混合协议机群会产出不同采样率）。总时长与旧实现毫秒级一致；旧实现每个间隙用 11025Hz 静音
再重采样，会少 1–2 帧（<0.1ms），新实现按目标帧率精确补齐，因此极端混合场景下不是逐字节相同。

### B2. 参考音频 44.1k 立体声 → 24k 单声道

`audio_processor.py:221-222` 的 ffmpeg 参数是 `-ar 44100 -ac 2`，但这份文件**只**用于裁剪参考片段，
而 `_export_reference_clip` 最终一定转成 24kHz 单声道（`main.py:46-54`）。等于先存 3.7 倍数据再丢掉。

- 2 小时视频：1.27GB → 345MB 常驻内存；
- 换文件时的解码/切片也更快；对 TTS 输入无任何影响。

改参数即可：`-ar 24000 -ac 1`。已有旧文件仍可正常复用（裁剪后统一重采样）。

### B3. 读 WAV 时长却整段解码

`main.py:409-412` 只为判断"WAV 是否 >100ms"就 `AudioSegment.from_file(output_audio_file)`。
2 小时 44.1k 立体声会瞬时分配 ~1.27GB。实测 300s/50MB 文件：解码 30ms、峰值 53MB；`sf.info` 1.3ms、近零内存。

```python
import soundfile as sf
info = sf.info(output_audio_file)
duration_ms = int(info.frames / info.samplerate * 1000)
```

同样思路可用于 `is_valid_audio_segment` 的静音判断（可选，收益小）。

### B4.（可选）参考音频随机读取

`main.py:1104-1109` / `main.py:1165-1170` 把整条参考音频读进内存（2 小时 1.27GB，B2 后仍 345MB）。
改成 `sf.SoundFile` + `seek/read` 的窗口读取，峰值降到单段大小（几 MB），代价是 `crop_audio` 多一层封装。
B2 之后这项从"刚需"降为"锦上添花"，建议观察实际内存再决定。

## 3. P2：稳定性与长跑

### C1. 给 gradio Client 加读超时

`dispatcher.py:75` 初始化后是 `httpx.Timeout(None, connect=10.0)`——读超时被禁用。
服务器 hang 住时 worker 永远不返回，只能靠 `API_HANG` 日志提示，任务也不会重试。
建议 `read=900.0`（单段字幕正常 <60s，15 分钟足够宽松），超时后走现有网络重试/熔断链路。

### C2. GUI 日志批量刷新

`logger.py:110-114` 每行日志触发一次 Qt signal，`gui.pyw:1236-1241` 每行 `QTextEdit.append`。
24 线程 + 每任务完成一行 + 合并进度每 5 段一行，长文件时事件队列会堆。
建议：logger 侧进环形缓冲，GUI 侧 `QTimer(200ms)` 批量取；同时
`log_output.document().setMaximumBlockCount(5000)` 限制内存。

### C3. `_check_threads` 清理

`gui.pyw:619/943/994/1061` 把每个探测线程追加进列表，永不删除；增删服务器几百次后
累积一批已结束的 QThread 及其 Python 对象。连接 `finished` 信号做 `remove` 即可。

### C4. `save_status` 原子写

`utils.py:22-28` 直接 `open(...,'w')` 写 JSON。GUI 关闭走 `os._exit(0)`（`gui.pyw:1410`），
若正好写到一半，会留下截断文件；`load_status` 只会返回空集合，靠 WAV 重扫兜底，但那意味着多一次全盘扫描。
改成 `.part` + `os.replace`，顺带 `indent=4` → 紧凑格式（文件小 3×，实测耗时差异仅 1.1×，不是性能点）。

### C5. 日志轮转

`logger.py` 每次启动新建文件，从不清理。建议启动时删除 `logs/` 下 30 天前的 `*.log`，
或按大小切分（10MB）。低风险。

### C6. `check_ffmpeg` 结果缓存

`audio_processor.py:178` 每次合并都跑一次 `ffmpeg -version` 子进程。模块级 `_FFMPEG_OK` 缓存即可。

### C7. 网络重试不再 `sleep` 占住 worker

`dispatcher.py:796-816` 失败后 `time.sleep(backoff_s)`（最长 15s）才把任务放回队列，期间这个 worker
完全空转。节点抖动时 24 个 worker 可能同时进入退避，有效并发骤降。
可改为"延迟可见队列"（任务带 `not_before`，取到未到期的就放回并继续取下一个），
或至少给退避加个全局并发上限。改动比 P0/P1 大，建议单独评估。

## 4. P3：结构性与清理

### D1/D2. 文件级流水线

- `main.py:1141-1148` 每个文件 `run_feeder` 等到**全部任务 + 全部 post-process** 完成才返回
  （`run_feeder` 的完成条件包含 CPU 后处理），然后才开始下一个文件的 TTS。
- 短文件（30 段）在 24 线程下尾部空转明显；长文件影响小。
- 彻底解法是"全局 dispatcher + 按文件分组的任务"，改动较大，建议先用实际日志统计文件尾部空闲率再决定。

### D3. 死代码

- `audio_processor.inspect_audio_segments`：无调用。
- `local_cache_manager.copy_folder_to_local`、`sync_results_to_cloud`：无调用（当前按单文件精确同步）。
- `main.tts_generation_task` + `api_client.generate_audio_api` / `TTSClient`：仅 CLI 单机路径可达，GUI 已全走多服务器。
- 删除约 300 行，降低后续改动的回归面。确认 CLI 无用后可一次性移除。

### D4. `duration_predictor.joblib` 入库

这是运行时训练产物，每 5 段重训一次并 `joblib.dump`，导致每次跑完 `git status` 都脏。
建议移出版本库 + 首次启动自动创建空模型（`model.py` 已有 fallback），或至少备份 tag 前 `git checkout --` 该文件。
不影响运行，只影响你的备份噪音。

## 5. 不建议做

- **每任务一个 hang watcher 线程**（`dispatcher.py:312-328`）：全量 3000 段会创建 3000 个短命线程。
  收益只是省一点线程创建开销，可改为单看门狗 + 截止时间堆，但优先级低。
- **把 `_prepare_tts_params` 挪进 worker**：现在在 feeder 单线程里串行裁剪/导出，单段约几十毫秒；
  只有服务器快到 <1s/段时才成为瓶颈，且会破坏"有界队列控制内存"的设计。
- **用 pydub `speedup` 替代 rubberband**：会改变音高/音质，不属于优化。
- **强行中断在途 HTTP 请求**：见动态池方案 §P3，风险大于收益。

## 6. 基准脚本与口径

- 环境：Windows / Python 3.12.10 / `.venv`；1s/段、24kHz 单声道 PCM16、随机噪声。
- `merge_audio`：`audio_processor.merge_audio` 原函数 vs 预先扫格式 + `b"".join` 实现；
  同一批输入，断言 `raw_data` 完全相等；日志函数替换为 no-op 以排除 I/O 干扰。
- WAV 时长：300s/44.1kHz/立体声（50.5MB），`AudioSegment.from_file` vs `soundfile.info`。
- 状态文件：3000 个索引 × 50 次，`indent=4` vs 紧凑 + `os.replace`。
- 数字仅代表本机、本数据；比例关系（O(n²) vs O(n)）可外推，绝对秒数不要直接搬到生产。

## 7. 建议排期

1. **随动态池 P1 一起**：A1–A4（都在 dispatcher，改动重叠）。
2. **紧随其后半天**：B1、B2、B3、C1（纯收益，互不冲突，可单独回归）。
3. **按需**：C2–C7、D1–D4。
