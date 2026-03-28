"""
dispatcher.py — 需求驱动分发器（有界队列 + 重试机制）

重试机制：
  - 每个任务最多重试 max_retries 次（retry_count 记录在任务对象上）
  - 单次 API 调用失败后，任务重新放回队列，由任意可用服务器继续处理
  - 只有当某个任务把所有重试次数全部耗尽（在某台服务器上最终失败），
    才判定该服务器下线，其 Worker 退出
  - 偶发失败不会触发服务器下线，多线程并发不互相干扰计数
  - 所有服务器都下线时触发 abort_flag，停止整个程序
"""

import os
import queue
import threading
from typing import Dict, Iterable, List, Callable, Optional, Tuple
from gradio_client import Client, file as gradio_file
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module='gradio_client.utils')

# ──────────────────────────────────────────────
# 服务器连接缓存
# ──────────────────────────────────────────────
_client_cache: dict = {}
_client_lock = threading.Lock()


def _get_client(url: str) -> Client:
    with _client_lock:
        if url not in _client_cache:
            print(f"[Dispatcher] 正在连接: {url}")
            _client_cache[url] = Client(url)
            print(f"[Dispatcher] 连接成功: {url}")
        return _client_cache[url]


def invalidate_client(url: str):
    with _client_lock:
        _client_cache.pop(url, None)


# ──────────────────────────────────────────────
# GPU 调用
# ──────────────────────────────────────────────
def _call_api_on_server(url: str, ref_audio_path: str,
                        gen_text: str, speed: float) -> Optional[str]:
    if not os.path.exists(ref_audio_path):
        return None
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
        return result["value"] if result else None
    except Exception as e:
        print(f"[Dispatcher] {url} 调用失败: {e}，重置连接。")
        invalidate_client(url)
        return None


# ──────────────────────────────────────────────
# 任务数据结构
# ──────────────────────────────────────────────
class _GpuTask:
    __slots__ = ('task_id', 'ref_audio_path', 'gen_text', 'speed',
                 'post_process_fn', 'done_callback', 'retry_count')

    def __init__(self, task_id, ref_audio_path, gen_text, speed,
                 post_process_fn, done_callback, retry_count=0):
        self.task_id         = task_id
        self.ref_audio_path  = ref_audio_path
        self.gen_text        = gen_text
        self.speed           = speed
        self.post_process_fn = post_process_fn
        self.done_callback   = done_callback
        self.retry_count     = retry_count   # 已重试次数


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
    """
    参数：
        server_configs  : {url: workers}
        max_retries     : 单个任务最大重试次数。
                          某台服务器上某任务重试次数耗尽时，判定该服务器下线。
        cpu_workers     : CPU 后处理线程数（0=自动）
        queue_depth_mul : gpu_queue 容量倍数（默认2）
        on_server_down  : 服务器下线时的回调 fn(url, reason_str)
        on_all_down     : 所有服务器下线时的回调 fn()
    """

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

        # 每台服务器独立的下线标记（线程安全通过 _server_lock 保护）
        # 无需计数器：由任务重试耗尽触发下线

        # 活跃服务器集合
        self._active_servers = set(self.server_configs.keys())
        self._server_lock    = threading.Lock()

        self._total_submitted = 0
        self._total_done      = 0
        self._done_lock       = threading.Lock()
        self._all_done_event  = threading.Event()
        self._abort_event     = threading.Event()

    # ── 生命周期 ──────────────────────────────
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
        print(f"[Dispatcher] 启动: GPU({cfg}) 共{total}线程 | "
              f"CPU {self.cpu_workers}线程 | 队列容量 {self._gpu_queue.maxsize} | "
              f"任务最大重试 {self.max_retries} 次 | "
              f"重试耗尽则判定该服务器下线")

    def run_feeder(self,
                   task_iter: Iterable[Tuple],
                   post_process_fn: Callable,
                   done_callback: Callable,
                   abort_flag_fn: Callable[[], bool]):
        submitted = 0
        for task_id, ref_audio_path, gen_text, speed in task_iter:
            if abort_flag_fn() or self._abort_event.is_set():
                break
            task = _GpuTask(task_id, ref_audio_path, gen_text, speed,
                            post_process_fn, done_callback)
            self._gpu_queue.put(task)
            submitted += 1

        with self._done_lock:
            self._total_submitted = submitted

        if submitted == 0:
            self._all_done_event.set()
            return

        self._all_done_event.wait()

    def join(self):
        self._all_done_event.wait()

    def stop(self):
        try:
            while True:
                self._gpu_queue.get_nowait()
                self._gpu_queue.task_done()
        except queue.Empty:
            pass

        for _ in self._gpu_worker_threads:
            self._gpu_queue.put(_SENTINEL)
        for t in self._gpu_worker_threads:
            t.join(timeout=10)

        try:
            while True:
                self._cpu_queue.get_nowait()
                self._cpu_queue.task_done()
        except queue.Empty:
            pass

        for _ in self._cpu_worker_threads:
            self._cpu_queue.put(_SENTINEL)
        for t in self._cpu_worker_threads:
            t.join(timeout=10)

    @property
    def all_servers_down(self) -> bool:
        return self._abort_event.is_set()

    # ── 服务器下线处理 ────────────────────────
    def _mark_server_down(self, url: str, reason: str):
        """将服务器标记为下线，触发回调，必要时触发全局停止"""
        with self._server_lock:
            if url not in self._active_servers:
                return  # 已经处理过
            self._active_servers.discard(url)
            print(f"[Dispatcher] !! 服务器下线: {url} | {reason}")
            if self._on_server_down:
                try:
                    self._on_server_down(url, reason)
                except Exception:
                    pass

            if not self._active_servers:
                print("[Dispatcher] !! 所有服务器均已下线，停止程序！")
                self._abort_event.set()
                self._all_done_event.set()
                if self._on_all_down:
                    try:
                        self._on_all_down()
                    except Exception:
                        pass

    # ── GPU Worker ────────────────────────────
    def _gpu_worker_loop(self, url: str):
        while True:
            # 已下线则退出
            with self._server_lock:
                is_down = url not in self._active_servers
            if is_down:
                break

            task = self._gpu_queue.get()
            if task is _SENTINEL:
                self._gpu_queue.task_done()
                break

            raw_path = None
            success  = False
            try:
                raw_path = _call_api_on_server(
                    url, task.ref_audio_path, task.gen_text, task.speed
                )
                success = raw_path is not None
            except Exception as e:
                print(f"[Dispatcher][GPU {url}] 异常: {e}")
                success = False
            finally:
                if success:
                    # ── 成功：直接送往 CPU 处理 ──
                    self._cpu_queue.put(_CpuTask(
                        task.task_id, raw_path, url,
                        task.post_process_fn, task.done_callback
                    ))
                    self._gpu_queue.task_done()
                else:
                    # ── 失败：先判断任务重试次数 ──
                    task.retry_count += 1
                    task_exhausted = task.retry_count > self.max_retries

                    if not task_exhausted and not self._abort_event.is_set():
                        # 还有重试机会：重新入队，由任意可用服务器处理
                        print(f"[Dispatcher] Task {task.task_id} 失败，"
                              f"第 {task.retry_count}/{self.max_retries} 次重试，重新入队...")
                        try:
                            self._gpu_queue.put_nowait(task)
                        except queue.Full:
                            self._gpu_queue.put(task)
                        self._gpu_queue.task_done()
                    else:
                        # 重试耗尽：说明这台服务器无法完成该任务，判定下线
                        if task_exhausted:
                            reason = (f"Task {task.task_id} 经过 {self.max_retries} 次重试全部失败，"
                                      f"服务器判定下线")
                            print(f"[Dispatcher] Task {task.task_id} 已达最大重试次数 "
                                  f"({self.max_retries})，使用静音占位，并标记服务器下线。")
                            self._mark_server_down(url, reason)
                        # 以静音占位完成任务
                        self._cpu_queue.put(_CpuTask(
                            task.task_id, None, url,
                            task.post_process_fn, task.done_callback
                        ))
                        self._gpu_queue.task_done()
                        break  # Worker 退出

    # ── CPU Worker ────────────────────────────
    def _cpu_worker_loop(self):
        while True:
            task = self._cpu_queue.get()
            if task is _SENTINEL:
                self._cpu_queue.task_done()
                break
            try:
                task.post_process_fn(task.task_id, task.raw_path, task.server_url)
                task.done_callback(task.task_id, task.server_url)
            except Exception as e:
                print(f"[Dispatcher][CPU] Task {task.task_id} 异常: {e}")
                try:
                    task.done_callback(task.task_id, task.server_url)
                except Exception:
                    pass
            finally:
                self._cpu_queue.task_done()
                with self._done_lock:
                    self._total_done += 1
                    if (self._total_submitted > 0 and
                            self._total_done >= self._total_submitted):
                        self._all_done_event.set()