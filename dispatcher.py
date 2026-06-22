"""
dispatcher.py — 需求驱动分发器（有界队列 + 重试机制）

重试机制：
  - 每个任务最多重试 max_retries 次（retry_count 记录在任务对象上）
  - 单次 API 调用失败后，任务重新放回队列，由任意可用服务器继续处理
  - 只有当某个任务把所有重试次数全部耗尽（在某台服务器上最终失败），
    才判定该服务器下线，其 Worker 退出
"""

import os
import queue
import threading
import time
from typing import Dict, Iterable, List, Callable, Optional, Tuple
from gradio_client import Client
import warnings

# ✨ 动态适配器：安全引入 Gradio 5.x 强制要求的 handle_file，并向下兼容旧版 file
try:
    from gradio_client import handle_file as gradio_file
except ImportError:
    from gradio_client import file as gradio_file

from logger import log as _log

warnings.filterwarnings("ignore", category=UserWarning, module='gradio_client.utils')

# ──────────────────────────────────────────────
# 【日志与监控配置】
# ──────────────────────────────────────────────
DEBUG_DISPATCHER      = False  # 设为 True 可开启详细的队列调度、API启停等底层日志
SLOW_API_THRESHOLD_S  = 45.0   # API 超过此时长打印 API_SLOW 警告 (放宽以减少日常刷屏)
HANG_API_THRESHOLD_S  = 120.0  # API 超过此时长打印 API_HANG 警告（疑似卡死）
IDLE_WARN_THRESHOLD_S = 15.0   # Worker 空闲等待超过此时长打印 WORKER_IDLE 警告
SLOW_CPU_THRESHOLD_S  = 5.0    # CPU 后处理超过此时长打印 CPU_SLOW 警告

# ──────────────────────────────────────────────
# 服务器连接缓存 (线程独立存储，避免并发冲突)
# ──────────────────────────────────────────────
_thread_local = threading.local()


def _get_client(url: str) -> Client:
    # Each thread maintains its own client cache
    if not hasattr(_thread_local, "client_cache"):
        _thread_local.client_cache = {}

    if url not in _thread_local.client_cache:
        thread_name = threading.current_thread().name
        if DEBUG_DISPATCHER:
            _log("CONNECT", f"[{thread_name}] 正在建立独立连接: {url}")
        _thread_local.client_cache[url] = Client(url)
        if DEBUG_DISPATCHER:
            _log("CONNECT", f"[{thread_name}] 连接建立成功: {url}")

    return _thread_local.client_cache[url]


def invalidate_client(url: str):
    """Clear current thread's client for the given URL (used after errors)"""
    if hasattr(_thread_local, "client_cache"):
        _thread_local.client_cache.pop(url, None)


# ──────────────────────────────────────────────
# GPU 调用（含超时计时日志 & 1.5/2.0 版本路由分流）
# ──────────────────────────────────────────────
def _call_api_on_server(url: str, ref_audio_path: str,
                        gen_text: str, speed: float,
                        version: str = "1.5",  # ✨ 新增参数：由调度 Worker 显式下发
                        task_id=None, queue_size="?") -> Optional[str]:
    if not os.path.exists(ref_audio_path):
        _log("API_ERR", f"Task {task_id} 参考音频不存在: {ref_audio_path}")
        return None

    label = f"Task {task_id} @ {url.rstrip('/').split(':')[-1]}"

    # ── 启动一个后台线程做"挂起检测" ──
    _stop_hang_watcher = threading.Event()

    def _hang_watcher(start: float):
        while not _stop_hang_watcher.wait(timeout=SLOW_API_THRESHOLD_S):
            elapsed = time.time() - start
            if elapsed >= HANG_API_THRESHOLD_S:
                _log("API_HANG",
                     f"{label} | 已等待 {elapsed:.1f}s — 推理端疑似卡死！"
                     f" 队列深度={queue_size}")
            else:
                _log("API_SLOW",
                     f"{label} | 已等待 {elapsed:.1f}s — 推理偏慢，继续等待…"
                     f" 队列深度={queue_size}")

    watcher = threading.Thread(target=_hang_watcher, args=(time.time(),), daemon=True)

    t0 = time.time()
    if DEBUG_DISPATCHER:
        _log("API_START", f"{label} | version={version} text_len={len(gen_text)} speed={speed}")
    watcher.start()

    try:
        client = _get_client(url)

        # ═════════════════ 分支 1：1.5 老版本接口路由 ═════════════════
        if version == "1.5":
            result = client.predict(
                prompt=gradio_file(ref_audio_path),
                text=gen_text,
                infer_mode='普通推理',
                max_text_tokens_per_sentence=120,
                sentences_bucket_max_size=4,
                param_5=True,
                param_6=0.8,
                param_7=30,
                param_8=speed,  # 1.5 版本的语速控制线
                param_9=0.0,
                param_10=3,
                param_11=10.0,
                param_12=600,
                api_name="/gen_single"
            )
            elapsed = time.time() - t0
            if DEBUG_DISPATCHER:
                _log("API_DONE", f"{label} (v1.5) | 耗时 {elapsed:.2f}s")
            # 【修复 I3】使用 isinstance + .get 规避非预期 dict 导致的 KeyError 风险
            if isinstance(result, dict):
                return result.get("value") or result.get("path") or result.get("url")
            if isinstance(result, str):
                return result
            return None

        # ═════════════════ 分支 2：2.0 新版本接口路由 ═════════════════
        else:
            # ✨ 严格类型对齐的 24 位位置参数流，int/float/bool 精确匹配后端断言
            result = client.predict(
                "Same as the voice reference",   # [0] 情感控制模式 (str)
                gradio_file(ref_audio_path),     # [1] 参考音频主路径 (filepath)
                gen_text,                        # [2] 目标文本 (str)
                gradio_file(ref_audio_path),     # [3] 情感参考音频占位 (filepath)
                0.65,                            # [4] 情感控制权重 (float)
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, # [5]-[12] vec1~vec8 (float)
                "",                              # [13] 情感文本描述 (str)
                False,                           # [14] 是否随机采样 (bool)
                120,                             # [15] 每段最大Token数 (int)
                True,                            # [16] do_sample (bool)
                0.8,                             # [17] top_p (float)
                30,                              # [18] top_k (int)
                0.8,                             # [19] temperature (float)
                0.0,                             # [20] length_penalty (float)
                3,                               # [21] num_beams (int)
                10.0,                            # [22] repetition_penalty (float)
                1500,                            # [23] max_mel_tokens (int)
                api_name="/gen_single"
            )
            elapsed = time.time() - t0
            if DEBUG_DISPATCHER:
                _log("API_DONE", f"{label} (v2.0) | 耗时 {elapsed:.2f}s")

            # v2.0 的 Gradio 如果返回了 File 字典，安全解出本地路径字符串
            if result is None:
                return None
            if isinstance(result, dict):
                # Gradio FileData 字典：key 优先级 path > url > name
                return result.get("path") or result.get("url") or result.get("name")
            if isinstance(result, str):
                return result  # 新版 gradio_client 可能直接返回路径字符串
            return None

    except Exception as e:
        elapsed = time.time() - t0
        _log("API_ERR", f"{label} | 调用失败 ({elapsed:.2f}s): {e}，重置连接。")
        invalidate_client(url)
        return None

    finally:
        _stop_hang_watcher.set()


# ──────────────────────────────────────────────
# 任务数据结构
# ──────────────────────────────────────────────
class _GpuTask:
    __slots__ = ('task_id', 'ref_audio_path', 'gen_text', 'speed',
                 'post_process_fn', 'done_callback', 'retry_count',
                 'enqueue_time')

    def __init__(self, task_id, ref_audio_path, gen_text, speed,
                 post_process_fn, done_callback, retry_count=0):
        self.task_id         = task_id
        self.ref_audio_path  = ref_audio_path
        self.gen_text        = gen_text
        self.speed           = speed
        self.post_process_fn = post_process_fn
        self.done_callback   = done_callback
        self.retry_count     = retry_count
        self.enqueue_time    = time.time()


class _CpuTask:
    __slots__ = ('task_id', 'raw_path', 'server_url',
                 'post_process_fn', 'done_callback')

    def __init__(self, task_id, raw_path, server_url,
                 post_process_fn, done_callback):
        self.task_id         = task_id
        self.raw_path        = raw_path
        self.server_url      = server_url
        self.post_process_fn = post_process_fn
        self.done_callback   = done_callback


_SENTINEL = object()


# ──────────────────────────────────────────────
# 核心分发器
# ──────────────────────────────────────────────
class MultiServerDispatcher:
    def __init__(self, server_configs: Dict[str, int],
                 max_retries: int = 3,
                 cpu_workers: int = 0,
                 queue_depth_mul: int = 2,
                 on_server_down: Optional[Callable] = None,
                 on_all_down: Optional[Callable] = None):
        self.server_configs = {
            u.strip(): max(1, n)
            for u, n in server_configs.items() if u.strip()
        }
        self.max_retries = max(1, max_retries)
        total_gpu_threads = sum(self.server_configs.values())
        self.cpu_workers = cpu_workers if cpu_workers > 0 else max(2, os.cpu_count() or 4)
        self._on_server_down = on_server_down
        self._on_all_down    = on_all_down

        maxsize = max(total_gpu_threads * queue_depth_mul, 4)
        self._gpu_queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._cpu_queue: queue.Queue = queue.Queue()

        self._gpu_worker_threads: List[threading.Thread] = []
        self._cpu_worker_threads: List[threading.Thread] = []

        self._active_servers = set(self.server_configs.keys())
        self._server_lock    = threading.Lock()

        self._total_submitted = 0
        self._total_done      = 0
        self._done_lock       = threading.Lock()
        self._all_done_event  = threading.Event()
        self._abort_event     = threading.Event()

        # ✨ 新增：静态服务器版本缓存字典 (URL -> "1.5" 或 "2.0")
        self.server_versions: Dict[str, str] = {}

    def start(self):
        # ─── ✨ 核心优化：在多线程 Worker 启动前，对所有配置的节点进行静态测试与接口打标 ───
        _log("INIT", "📡 正在对集群节点进行连通性测试与接口版本嗅探...")
        for url in self.server_configs.keys():
            try:
                cli = _get_client(url)
                api_info = cli.view_api(return_format="dict", print_info=False)
                named_endpoints = api_info.get("named_endpoints", {})

                # 使用 v2.0 专属特征端点做精准判定（/gen_single 两代都有，不能用）
                if "/on_method_change" in named_endpoints or "/on_add_glossary_term" in named_endpoints:
                    self.server_versions[url] = "2.0"
                else:
                    self.server_versions[url] = "1.5"
                _log("INIT", f"✅ 节点连通测试成功: {url} | 成功识别接口协议: {self.server_versions[url]}")
            except Exception as e:
                self.server_versions[url] = "1.5"
                _log("WARN", f"⚠️ 节点握手失败: {url} ({e})，协议安全临时降级为 1.5")

        # ─── 顺理成章地继续启动你原本的多线程 Worker ───
        total = 0
        for url, n in self.server_configs.items():
            for i in range(n):
                t = threading.Thread(
                    target=self._gpu_worker_loop,
                    args=(url,),
                    name=f"GPU-{url.rstrip('/').split(':')[-1]}-{i}",
                    daemon=True
                )
                t.start()
                self._gpu_worker_threads.append(t)
            total += n

        for i in range(self.cpu_workers):
            t = threading.Thread(
                target=self._cpu_worker_loop,
                name=f"CPU-{i}",
                daemon=True
            )
            t.start()
            self._cpu_worker_threads.append(t)

        cfg = ", ".join(f"{u.rstrip('/').split(':')[-1]}×{n}({self.server_versions.get(u, '1.5')})"
                        for u, n in self.server_configs.items())
        _log("INIT", f"启动完成: GPU集群({cfg}) 共{total}线程 | "
                     f"CPU {self.cpu_workers}线程 | 队列容量 {self._gpu_queue.maxsize} | "
                     f"任务最大重试 {self.max_retries} 次")

    def run_feeder(self,
                   task_iter: Iterable[Tuple],
                   post_process_fn: Callable,
                   done_callback: Callable,
                   abort_flag_fn: Callable[[], bool]):
        with self._done_lock:
            self._total_done = 0          # 重置已完成计数
            self._all_done_event.clear()  # 清除上一个文件的完成事件信号
            self._total_submitted = float('inf')

        # ── 分发前重连：清除死连接缓存并重新握手 ──
        # start() 到 run_feeder() 之间可能间隔数分钟，Cloudflare/Gradio Live
        # 会在空闲 60-90 秒后踢掉连接，导致首批任务集中失败。
        # 此处对所有活跃节点逐一清除缓存并重新握手，确保分发时连接是新鲜的。
        _log("INIT", "🔄 分发前重新探测所有节点连接...")
        for url in list(self._active_servers):
            invalidate_client(url)
            try:
                _get_client(url)
                _log("INIT", f"✅ 节点重连成功: {url}")
            except Exception as e:
                _log("WARN", f"⚠️ 节点重连失败: {url} ({e})，首个任务将触发重试。")

        submitted = 0
        for task_id, ref_audio_path, gen_text, speed in task_iter:
            if abort_flag_fn() or self._abort_event.is_set():
                break

            task = _GpuTask(task_id, ref_audio_path, gen_text, speed,
                            post_process_fn, done_callback)

            if self._gpu_queue.full():
                if DEBUG_DISPATCHER:
                    _log("QUEUE_BLOCK", f"Task {task_id} 入队阻塞 — GPU 队列已满...")
                t_block = time.time()
                self._gpu_queue.put(task)
                if DEBUG_DISPATCHER:
                    block_s = time.time() - t_block
                    _log("QUEUE_UNBLOCK", f"Task {task_id} 入队成功，阻塞了 {block_s:.2f}s")
            else:
                self._gpu_queue.put(task)

            submitted += 1

        with self._done_lock:
            self._total_submitted = submitted
            if submitted == 0 or self._total_done >= submitted:
                self._all_done_event.set()
                return

        self._all_done_event.wait()

    def join(self):
        self._all_done_event.wait()

    def stop(self):
        self._abort_event.set()

        for t in self._gpu_worker_threads:
            if t.is_alive():
                self._gpu_queue.put(_SENTINEL)
        for t in self._gpu_worker_threads:
            t.join(timeout=10)

        abandoned = 0
        try:
            while True:
                task = self._gpu_queue.get_nowait()
                self._gpu_queue.task_done()
                if task is _SENTINEL: continue
                abandoned += 1
                self._cpu_queue.put(_CpuTask(
                    task.task_id, None, "",
                    task.post_process_fn, task.done_callback
                ))
        except queue.Empty:
            pass
        if abandoned:
            _log("STOP", f"已将 {abandoned} 个未处理任务转为静音占位")

        for t in self._cpu_worker_threads:
            if t.is_alive():
                self._cpu_queue.put(_SENTINEL)
        for t in self._cpu_worker_threads:
            t.join(timeout=10)

        try:
            while True:
                task = self._cpu_queue.get_nowait()
                self._cpu_queue.task_done()
                if task is not _SENTINEL:
                    try:
                        task.done_callback(task.task_id, task.server_url)
                    except Exception:
                        pass
        except queue.Empty:
            pass

    @property
    def all_servers_down(self) -> bool:
        return self._abort_event.is_set()

    def _mark_server_down(self, url: str, reason: str):
        with self._server_lock:
            if url not in self._active_servers: return
            self._active_servers.discard(url)
            _log("SERVER_DOWN", f"!! 服务器下线: {url} | {reason}")
            if self._on_server_down:
                try: self._on_server_down(url, reason)
                except Exception: pass

            if not self._active_servers:
                _log("ALL_DOWN", "!! 所有服务器均已下线，停止程序！")
                self._abort_event.set()
                self._all_done_event.set()
                if self._on_all_down:
                    try: self._on_all_down()
                    except Exception: pass

    def _gpu_worker_loop(self, url: str):
        worker_name = threading.current_thread().name

        # ── Worker 启动时重连：清除本线程可能缓存的死连接 ──
        # run_feeder() 的探测仅作用于主线程，Worker 线程各自有独立的
        # _thread_local.client_cache，需要在本线程内清除并重新握手。
        invalidate_client(url)
        try:
            _get_client(url)
        except Exception:
            pass  # 首个任务的 _call_api_on_server 会触发重试并记录错误

        while True:
            with self._server_lock:
                is_down = url not in self._active_servers
            if is_down: break

            try:
                task = self._gpu_queue.get(timeout=IDLE_WARN_THRESHOLD_S)
            except queue.Empty:
                if DEBUG_DISPATCHER:
                    _log("WORKER_IDLE", f"{worker_name} 已空闲 >{IDLE_WARN_THRESHOLD_S}s")
                continue

            if task is _SENTINEL:
                self._gpu_queue.task_done()
                break

            if DEBUG_DISPATCHER:
                queue_wait_s = time.time() - task.enqueue_time
                if queue_wait_s > 2.0:
                    _log("QUEUE_WAIT", f"Task {task.task_id} 排队等待了 {queue_wait_s:.2f}s")
                _log("WORKER_PICK", f"{worker_name} 取出 Task {task.task_id} (retry={task.retry_count})")

            raw_path = None
            success  = False
            try:
                q_depth = self._gpu_queue.qsize()

                # ✨ 核心重构：直接从初始化时探测好的注册表中取出该服务器对应的静态版本，传递给分发引擎
                srv_version = self.server_versions.get(url, "1.5")

                raw_path = _call_api_on_server(
                    url, task.ref_audio_path, task.gen_text, task.speed,
                    version=srv_version,  # ✨ 注入版本参数
                    task_id=task.task_id, queue_size=q_depth
                )
                success = raw_path is not None
            except Exception as e:
                _log("WORKER_ERR", f"{worker_name} Task {task.task_id} 异常: {e}")
                success = False
            finally:
                if success:
                    self._cpu_queue.put(_CpuTask(
                        task.task_id, raw_path, url,
                        task.post_process_fn, task.done_callback
                    ))
                    self._gpu_queue.task_done()
                else:
                    task.retry_count += 1
                    task_exhausted = task.retry_count > self.max_retries

                    if not task_exhausted and not self._abort_event.is_set():
                        _log("RETRY", f"Task {task.task_id} 失败，第 {task.retry_count}/{self.max_retries} 次重试，重新入队…")
                        try: self._gpu_queue.put_nowait(task)
                        except queue.Full: self._gpu_queue.put(task)
                        self._gpu_queue.task_done()
                    else:
                        if task_exhausted:
                            reason = f"Task {task.task_id} 经过 {self.max_retries} 次重试全部失败，服务器判定下线"
                            _log("RETRY_EXHAUSTED", f"Task {task.task_id} 已达最大重试次数，标记服务器下线。")
                            self._mark_server_down(url, reason)
                        if self._abort_event.is_set():
                            self._gpu_queue.task_done()
                            break
                        self._cpu_queue.put(_CpuTask(
                            task.task_id, None, url,
                            task.post_process_fn, task.done_callback
                        ))
                        self._gpu_queue.task_done()
                        break

    def _cpu_worker_loop(self):
        worker_name = threading.current_thread().name
        while True:
            task = self._cpu_queue.get()
            if task is _SENTINEL:
                self._cpu_queue.task_done()
                break
            t0 = time.time()
            try:
                task.post_process_fn(task.task_id, task.raw_path, task.server_url)
                task.done_callback(task.task_id, task.server_url)
            except Exception as e:
                _log("CPU_ERR", f"{worker_name} Task {task.task_id} 异常: {e}")
                try: task.done_callback(task.task_id, task.server_url)
                except Exception: pass
            finally:
                if DEBUG_DISPATCHER:
                    elapsed = time.time() - t0
                    if elapsed > SLOW_CPU_THRESHOLD_S:
                        _log("CPU_SLOW", f"{worker_name} Task {task.task_id} 后处理耗时 {elapsed:.2f}s")
                self._cpu_queue.task_done()
                with self._done_lock:
                    self._total_done += 1
                    if (self._total_submitted > 0 and self._total_done >= self._total_submitted):
                        self._all_done_event.set()
