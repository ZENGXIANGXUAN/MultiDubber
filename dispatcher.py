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
from gradio_client import Client, file as gradio_file
import warnings

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
# GPU 调用（含超时计时日志）
# ──────────────────────────────────────────────
def _call_api_on_server(url: str, ref_audio_path: str,
                        gen_text: str, speed: float,
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
        _log("API_START", f"{label} | text_len={len(gen_text)} speed={speed}")
    watcher.start()

    try:
        client = _get_client(url)
        result = client.predict(
            prompt=gradio_file(ref_audio_path),
            text=gen_text,
            infer_mode='普通推理',
            max_text_tokens_per_sentence=120,
            sentences_bucket_max_size=4,
            param_5=True,
            param_6=0.8,
            param_7=30,
            param_8=speed,
            param_9=0.0,
            param_10=3,
            param_11=10.0,
            param_12=600,
            api_name="/gen_single"
        )
        elapsed = time.time() - t0
        if DEBUG_DISPATCHER:
            _log("API_DONE", f"{label} | 耗时 {elapsed:.2f}s")
        return result["value"] if result else None

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

    def start(self):
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

        cfg = ", ".join(f"{u.rstrip('/').split(':')[-1]}×{n}"
                        for u, n in self.server_configs.items())
        _log("INIT", f"启动: GPU({cfg}) 共{total}线程 | "
                     f"CPU {self.cpu_workers}线程 | 队列容量 {self._gpu_queue.maxsize} | "
                     f"任务最大重试 {self.max_retries} 次")

    def run_feeder(self,
                   task_iter: Iterable[Tuple],
                   post_process_fn: Callable,
                   done_callback: Callable,
                   abort_flag_fn: Callable[[], bool]):
        with self._done_lock:
            self._total_submitted = float('inf')

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
                raw_path = _call_api_on_server(
                    url, task.ref_audio_path, task.gen_text, task.speed,
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
