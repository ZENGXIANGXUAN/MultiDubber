"""
dispatcher.py — 需求驱动分发器（有界队列 + 重试机制 + 服务器熔断）

重试机制：
  - 每个任务最多重试 max_retries 次（retry_count 记录在任务对象上）
  - 单次 API 调用失败后，任务重新放回队列，由任意可用服务器继续处理

熔断机制（per-server 连续重连失败）：
  - 每台服务器独立追踪"错误重连后的成功产出"
  - API 调用失败 → invalidate_client → 标记待评估
  - 下次重建连接时检查：上次重连后有没有成功处理过任务
  - 连续 N 次重连无产出 → 判定服务器下线
  - 空闲超时重连不计入连续失败（预防性动作，服务器可能完全健康）
"""

import os
import socket
import ssl
import queue
import shutil
import hashlib
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Callable, Optional, Tuple
from gradio_client import Client
import httpx
import warnings

# ✨ 动态适配器：安全引入 Gradio 5.x 强制要求的 handle_file，并向下兼容旧版 file
try:
    from gradio_client import handle_file as gradio_file
except ImportError:
    from gradio_client import file as gradio_file

from logger import log as _log
import indextts_15
import voxcpm2

warnings.filterwarnings("ignore", category=UserWarning, module='gradio_client.utils')

# ──────────────────────────────────────────────
# 【日志与监控配置】
# ──────────────────────────────────────────────
DEBUG_DISPATCHER      = False  # 设为 True 可开启详细的队列调度、API启停等底层日志
SLOW_API_THRESHOLD_S  = 45.0   # Hang watcher 检查间隔（秒）
HANG_API_THRESHOLD_S  = 120.0  # API 超过此时长打印 API_HANG 警告（疑似卡死）
IDLE_WARN_THRESHOLD_S = 15.0   # Worker 空闲等待超过此时长打印 WORKER_IDLE 警告
SLOW_CPU_THRESHOLD_S  = 5.0    # CPU 后处理超过此时长打印 CPU_SLOW 警告
RECONNECT_IDLE_S      = 300.0  # 仅长时间空闲后预防性重连；普通批次复用现有连接
MAX_NETWORK_ATTEMPTS_PER_TASK = 5     # 单个任务最多 5 次网络尝试，避免坏节点长期占用队列
NETWORK_RETRY_BACKOFF_MAX_S = 15.0   # 网络重试退避等待上限
TTS_READ_TIMEOUT_S    = 900.0  # 单次推理读超时；防止挂死节点永久占用 worker（正常单段 < 60s）
CONNECT_SPACING_S     = 0.3    # 同一时刻只允许一个 worker 开始握手，避免 SSL 风暴

# 运行时服务器池开关：关闭后 add/remove 不生效，行为回到“下次流程启动才更新”。
LIVE_POOL_ENABLED = True

# ──────────────────────────────────────────────
# 产物私有化：把 gradio_client 的下载结果复制成本任务独占的文件
# ──────────────────────────────────────────────
# gradio_client 2.x 的下载目录是全局共享的（默认 %TEMP%/gradio），且按
# 内容 sha256 分目录：dest = <output_dir>/<sha256(content)>/<filename>。
# 也就是说“内容相同的两次返回”必然映射到同一个本地路径，而本项目过去把它
# 当成本任务私有文件、在后处理结束时 os.remove —— 同一路径被两个任务持有
# 时，先跑完的那个会把文件删掉，另一个只能读到空文件（静音占位）或读到
# 对方的内容（整句串台）。这里统一复制到任务独占路径，从根本上断开共享。
RAW_STAGE_DIR = os.path.join(tempfile.gettempdir(), "MultTTS_raw")


def is_owned_raw_path(path: Optional[str]) -> bool:
    """判断路径是否为调度器私有化后的产物（只有这种路径才允许后处理删除）。"""
    if not path:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(RAW_STAGE_DIR)]) == os.path.abspath(RAW_STAGE_DIR)
    except (ValueError, OSError):
        return False


def materialize_private_audio(raw_path: str, task_id) -> Optional[str]:
    """
    把返回音频复制到本任务独占路径。失败返回 None（交给重试）。

    复制而不是移动：源文件可能仍被其他任务的下载流程引用；移动会破坏它们。
    """
    try:
        os.makedirs(RAW_STAGE_DIR, exist_ok=True)
        ext = os.path.splitext(raw_path)[1] or ".wav"
        dest = os.path.join(RAW_STAGE_DIR, f"raw_{task_id}_{uuid.uuid4().hex[:8]}{ext}")
        shutil.copy2(raw_path, dest)
        return dest
    except Exception as e:
        _log("RAW", f"Task {task_id} 产物私有化失败，放弃本次返回: {e}")
        return None


def discard_private_audio(path: Optional[str]) -> None:
    """只删除私有化目录下的文件；外部路径一律不碰。"""
    if not is_owned_raw_path(path):
        return
    try:
        os.remove(path)
    except OSError:
        pass


def cleanup_stale_raw_files(max_age_s: float = 6 * 3600) -> None:
    """清理崩溃/强杀残留的私有产物，避免 %TEMP% 无限膨胀。"""
    if not os.path.isdir(RAW_STAGE_DIR):
        return
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        for name in os.listdir(RAW_STAGE_DIR):
            if not (name.startswith("raw_") and name.endswith(".wav")):
                continue
            path = os.path.join(RAW_STAGE_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return
    if removed:
        _log("RAW", f"清理残留私有产物 {removed} 个（>{max_age_s / 3600:.0f}h）")


class _ProductRegistry:
    """
    同一批任务内对“返回音频内容”做指纹登记，用于发现多节点并发串台。

    判定规则：
      - 内容完全相同 + 文本相同  → 合法（SRT 里本来就有重复台词），允许复用
      - 内容完全相同 + 文本不同  → 服务端把 A 的产物给了 B，必须重试

    这正是本次事故的形态：index 4（“完整拆解夺冠策略…”）与 index 7
    （“而是等它开始往一侧逼仓…”）拿到了字节完全相同的音频。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._by_digest = {}    # digest -> (task_id, text)
        self._task_digest = {}  # task_id -> digest

    def reset(self) -> None:
        with self._lock:
            self._by_digest.clear()
            self._task_digest.clear()

    @staticmethod
    def _digest(path: str) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()

    def check(self, task_id, text: str, path: str) -> Tuple[bool, Optional[int]]:
        """返回 (是否与别的任务串台, 对方 task_id)。本任务的旧登记会被新指纹覆盖。"""
        # 极短文本（语气词、单词应答）本来就容易生成几乎相同的气口，
        # 判重收益低而误伤概率高，直接放行。
        if len((text or "").strip()) < 4:
            return False, None
        try:
            digest = self._digest(path)
        except OSError:
            return False, None
        with self._lock:
            previous = self._task_digest.get(task_id)
            if previous and previous != digest and self._by_digest.get(previous, (None,))[0] == task_id:
                self._by_digest.pop(previous, None)

            owner = self._by_digest.get(digest)
            if owner is not None and owner[0] != task_id and owner[1] != text:
                return True, owner[0]

            self._by_digest[digest] = (task_id, text)
            self._task_digest[task_id] = digest
            return False, None


_PRODUCT_REGISTRY = _ProductRegistry()


# ──────────────────────────────────────────────
# 服务器连接缓存 (线程独立存储，避免并发冲突)
# ──────────────────────────────────────────────
_thread_local = threading.local()


def _get_client(url: str) -> Client:
    """获取当前线程的 Gradio 客户端连接。若不存在则新建。"""
    if not hasattr(_thread_local, "client_cache"):
        _thread_local.client_cache = {}

    if url not in _thread_local.client_cache:
        thread_name = threading.current_thread().name
        if DEBUG_DISPATCHER:
            _log("CONNECT", f"[{thread_name}] 正在建立独立连接: {url}")
        # Client 初始化会拉取 Gradio 配置/API 信息。Cloudflare/TLS 抖动时如果
        # 没有超时，Worker 可能永久卡在握手。初始化时设置短超时；成功后切换为
        # 只限制 connect，避免真正的长时间 TTS 推理被 read timeout 中断。
        client = Client(
            url,
            verbose=False,
            httpx_kwargs={"timeout": httpx.Timeout(15.0, connect=8.0)},
        )
        client.httpx_kwargs["timeout"] = httpx.Timeout(TTS_READ_TIMEOUT_S, connect=10.0)
        _thread_local.client_cache[url] = client
        if DEBUG_DISPATCHER:
            _log("CONNECT", f"[{thread_name}] 连接建立成功: {url}")

    return _thread_local.client_cache[url]


def _pop_client(url: str):
    """从当前线程缓存中摘除指定连接（不负责关闭）。"""
    cache = getattr(_thread_local, "client_cache", None)
    if not cache:
        return None
    return cache.pop(url, None)


def _close_client(client) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


def _close_thread_clients(url: str) -> None:
    """关闭当前线程对指定 URL 的连接，避免 worker 退出后连接池残留。"""
    _close_client(_pop_client(url))


def invalidate_client(url: str, error: bool = True):
    """清除当前线程对指定 URL 的客户端缓存。

    Args:
        url: 服务器地址
        error: True 表示由 API 调用失败触发（计入熔断评估），
               False 表示空闲超时预防性重连（不计入）。
    """
    _close_client(_pop_client(url))
    if error:
        _mark_error_reconnect(url)


# ──────────────────────────────────────────────
# 熔断状态：per-server per-thread 追踪
# ──────────────────────────────────────────────
# 全局注册表：url -> _ServerState（所有线程共享，读写通过 _cb_lock 保护）
_cb_lock = threading.Lock()
_server_states: Dict[str, "_ServerState"] = {}


class _ServerState:
    """单台服务器的熔断追踪状态。"""
    __slots__ = ('error_reconnect_pending', 'tasks_since_reconnect',
                 'consecutive_failures', 'last_failure_time')

    def __init__(self):
        self.error_reconnect_pending = False   # 是否有待评估的错误重连
        self.tasks_since_reconnect = 0         # 上次重连后的成功任务数
        self.consecutive_failures = 0          # 连续无效重连计数
        self.last_failure_time = 0.0           # 最后一次失败时间戳（用于并发防抖）


def _get_server_state(url: str) -> _ServerState:
    """获取（或创建）指定服务器的熔断状态对象。"""
    if url not in _server_states:
        with _cb_lock:
            if url not in _server_states:
                _server_states[url] = _ServerState()
    return _server_states[url]


def _mark_error_reconnect(url: str):
    """API 调用失败后调用：标记下次重连需要评估。"""
    state = _get_server_state(url)
    with _cb_lock:
        state.error_reconnect_pending = True


def reset_circuit_breaker_states():
    """重置所有服务器的熔断状态。

    重启整条流程前必须调用，否则旧的 consecutive_failures 会导致
    服务器刚开始运行就被判定为熔断。
    """
    global _server_states
    with _cb_lock:
        _server_states.clear()


def _record_api_success(url: str):
    """API 调用成功后调用：累加重连间的成功任务数。"""
    state = _get_server_state(url)
    with _cb_lock:
        state.tasks_since_reconnect += 1


def _evaluate_reconnect(url: str):
    """错误重连后立即调用：评估本次重连是否有效。

    返回 True 表示触发了熔断（调用方应中止），False 表示正常。
    """
    state = _get_server_state(url)
    with _cb_lock:
        if not state.error_reconnect_pending:
            return False
        # 上次重连后没有成功产出 → 连续失败
        if state.tasks_since_reconnect == 0:
            now = time.time()
            # 时间防抖：2秒内的并发失败只算作1次（防止多线程同时因同一网络抖动导致计数激增）
            if now - state.last_failure_time > 2.0:
                state.consecutive_failures += 1
                state.last_failure_time = now
        else:
            state.consecutive_failures = 0  # 有产出，重置
            state.last_failure_time = 0.0   # 重置时间戳
        state.tasks_since_reconnect = 0
        state.error_reconnect_pending = False
        # 阈值为 -1 表示禁用熔断，永远不触发
        if _circuit_breaker_threshold < 0:
            return False
        return state.consecutive_failures >= _circuit_breaker_threshold


# 模块级熔断阈值，由 MultiServerDispatcher.__init__ 设置
_circuit_breaker_threshold: int = 6


# ──────────────────────────────────────────────
# API 调用结果分类
# ──────────────────────────────────────────────
# none    : 成功
# api     : 配音/API 本身失败，计入 task.retry_count
# network : 网络/连接失败，不计入 task.retry_count，但仍参与服务器熔断
_FAIL_NONE = "none"
_FAIL_API = "api"
_FAIL_NETWORK = "network"


def _iter_exception_chain(exc: Exception):
    """遍历异常链，兼容 requests/httpx/httpcore/gradio_client 包装后的异常。"""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)


def _is_network_error(exc: Exception) -> bool:
    """
    判断是否属于网络/连接层错误。

    这类错误不应该消耗“单个配音任务最大重试次数”：
    - 连接失败/连接重置/连接中断
    - 读写超时
    - DNS/代理/SSL/Cloudflare 502/503/504 等网关问题
    - gradio_client 无法获取配置
    """
    network_modules = ("requests", "urllib3", "httpx", "httpcore", "websockets", "aiohttp")
    network_class_keywords = (
        "timeout", "connect", "connection", "network", "proxy",
        "ssl", "readerror", "writeerror", "remoteprotocol", "protocolerror",
    )
    network_message_keywords = (
        "timed out", "timeout", "read timeout", "connect timeout",
        "connection reset", "connection aborted", "connection refused",
        "connection error", "remote end closed", "server disconnected",
        "network is unreachable", "temporary failure in name resolution",
        "name or service not known", "nodename nor servname provided",
        "dns", "ssl", "bad gateway", "service unavailable",
        "gateway timeout", "502", "503", "504", "cloudflare",
        "winerror 10054", "winerror 10060", "errno 104", "errno 110",
        "could not fetch config", "fetch config", "cannot connect",
        "connection closed", "connection broken", "connection terminated",
        "no route to host", "host unreachable",
    )

    messages = []
    for item in _iter_exception_chain(exc):
        messages.append(str(item).lower())
        if isinstance(item, (TimeoutError, ConnectionError, socket.timeout, ssl.SSLError)):
            return True

        module_name = item.__class__.__module__.lower()
        class_name = item.__class__.__name__.lower()
        if module_name.startswith(network_modules) and any(k in class_name for k in network_class_keywords):
            return True

    joined = " | ".join(messages)
    return any(k in joined for k in network_message_keywords)


def _extract_audio_path(result) -> Optional[str]:
    """把 VoxCPM /generate 返回的 (audio_path, seed) 提取成音频路径。"""
    if result is None:
        return None
    if isinstance(result, (tuple, list)):
        return _extract_audio_path(result[0]) if result else None
    if isinstance(result, dict):
        return result.get("value") or result.get("path") or result.get("url") or result.get("name")
    if isinstance(result, str):
        return result
    return None


def _validate_returned_audio(path: Optional[str]) -> Tuple[bool, str]:
    """
    检查云端返回的音频文件是否有效可用：
    1. 路径非空且在本地文件系统实际存在
    2. 文件大小 >= 100 字节（排除空文件或返回的纯文本错误信息）
    3. 音频头格式合法且至少包含 1 个有效音频帧（排除损坏文件）
    """
    if not path:
        return False, "云端返回为空或未包含有效音频路径"
    if not os.path.exists(path):
        return False, f"云端返回的本地文件不存在: {path}"
    try:
        size = os.path.getsize(path)
        if size < 100:
            return False, f"云端返回文件过小 (size={size}B)，判定为传输或生成不完整"
        with open(path, "rb") as f:
            header = f.read(12)
        if header.startswith(b"RIFF"):
            import wave
            with wave.open(path, "rb") as w:
                if w.getnframes() <= 0:
                    return False, "WAV 文件中无有效音频帧 (frames=0)"
        elif not (header.startswith(b"ID3") or header.startswith(b"\xff\xfb") or header.startswith(b"OggS") or header.startswith(b"fLaC")):
            return False, f"云端返回文件非音频格式 (header={header[:8]!r})"
    except Exception as e:
        return False, f"解析返回音频失败 (文件可能已损坏): {e}"
    return True, ""


# ──────────────────────────────────────────────
# GPU 调用（根据节点绑定的静态协议直接调用，无动态判断）
# ──────────────────────────────────────────────
def _call_api_on_server(url: str, ref_audio_path: str,
                        gen_text: str, speed: float,
                        task_id=None, queue_size="?",
                        network_attempt: int = 1,
                        protocol: str = "voxcpm2") -> Tuple[Optional[str], str]:
    """
    返回 (raw_audio_path, fail_kind)

    fail_kind:
      - _FAIL_NONE    成功（且返回音频文件已验证可用）
      - _FAIL_API     配音/API 本身失败或返回音频不可用，计入任务失败重试次数
      - _FAIL_NETWORK 网络/连接问题，不计入任务失败重试次数
    """
    if not os.path.exists(ref_audio_path):
        _log("API_ERR", f"Task {task_id} 参考音频不存在: {ref_audio_path}")
        return None, _FAIL_API

    proto_name = "IndexTTS 1.5" if protocol == "indextts_15" else "VoxCPM 2.0"
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

    watcher = threading.Thread(target=_hang_watcher, args=(time.time(),), daemon=True)

    t0 = time.time()
    if DEBUG_DISPATCHER:
        _log("API_START", f"{label} ({proto_name}) | text_len={len(gen_text)} speed={speed:.2f}")
    watcher.start()

    try:
        client = _get_client(url)

        if protocol == "indextts_15":
            result = indextts_15.predict_audio(
                client,
                ref_audio_path,
                gen_text,
                speed,
                gradio_file,
            )
        else:
            result = voxcpm2.predict_audio(
                client,
                ref_audio_path,
                gen_text,
                speed,
                gradio_file,
            )

        elapsed = time.time() - t0
        if DEBUG_DISPATCHER:
            _log("API_DONE", f"{label} ({proto_name}) | 耗时 {elapsed:.2f}s")

        path = _extract_audio_path(result)
        is_valid, err_msg = _validate_returned_audio(path)
        if is_valid:
            # 1) 先私有化：后续所有后处理只操作这份独占副本。
            private_path = materialize_private_audio(path, task_id)
            if not private_path:
                _log("API_ERR", f"{label} ({proto_name}) | 返回音频无法私有化，计入配音重试。")
                return None, _FAIL_API

            # 2) 再做内容指纹校验：内容与另一个任务相同但文本不同 = 服务端串台。
            duplicated, owner_task = _PRODUCT_REGISTRY.check(task_id, gen_text, private_path)
            if duplicated:
                _log(
                    "DUP",
                    f"{label} | Task {task_id} 返回的音频与 Task {owner_task} 字节完全相同，"
                    f"但文本不同，判定为并发串台，丢弃并重新入队。",
                )
                discard_private_audio(private_path)
                return None, _FAIL_API

            return private_path, _FAIL_NONE

        _log("API_ERR", f"{label} ({proto_name}) | 云端返回音频无效 ({err_msg})，计入配音重试。")
        return None, _FAIL_API

    except Exception as e:
        elapsed = time.time() - t0
        fail_kind = _FAIL_NETWORK if _is_network_error(e) else _FAIL_API
        if fail_kind == _FAIL_NETWORK:
            # 网络抖动时只记录关键节点，避免每个任务每次重试都刷两行日志。
            if DEBUG_DISPATCHER or network_attempt in (1, 3, MAX_NETWORK_ATTEMPTS_PER_TASK):
                _log(
                    "NET_ERR",
                    f"{label} | 网络尝试 {network_attempt}/{MAX_NETWORK_ATTEMPTS_PER_TASK} 失败 "
                    f"({elapsed:.2f}s): {e}，重置连接。"
                )
        else:
            _log("API_ERR", f"{label} | API/推理失败 ({elapsed:.2f}s): {e}，重置连接；计入任务失败次数。")
        invalidate_client(url, error=True)  # 仍计入服务器熔断评估，避免坏节点无限占用任务
        return None, fail_kind

    finally:
        _stop_hang_watcher.set()


# ──────────────────────────────────────────────
# 任务数据结构
# ──────────────────────────────────────────────
class _GpuTask:
    __slots__ = ('task_id', 'ref_audio_path', 'gen_text', 'speed',
                 'post_process_fn', 'done_callback', 'retry_count',
                 'network_retry_count', 'enqueue_time', 'is_requeued')

    def __init__(self, task_id, ref_audio_path, gen_text, speed,
                 post_process_fn, done_callback, retry_count=0,
                 network_retry_count=0, is_requeued=False):
        self.task_id         = task_id
        self.ref_audio_path  = ref_audio_path
        self.gen_text        = gen_text
        self.speed           = speed
        self.post_process_fn = post_process_fn
        self.done_callback   = done_callback
        self.retry_count     = retry_count
        self.network_retry_count = network_retry_count
        self.enqueue_time    = time.time()
        self.is_requeued     = is_requeued

    def __lt__(self, other):
        """PriorityQueue 排序：重试任务优先，同级内按入队时间升序"""
        if not isinstance(other, _GpuTask):
            return False  # 哨兵等非任务对象始终排在前面
        sk = (0 if self.is_requeued else 1, self.enqueue_time)
        ok = (0 if other.is_requeued else 1, other.enqueue_time)
        return sk < ok


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


class _Sentinel:
    """哨兵对象，始终排在 PriorityQueue 最前面，用于通知 Worker 退出。"""
    __slots__ = ()
    def __lt__(self, other):
        return not isinstance(other, _Sentinel)

_SENTINEL = _Sentinel()


# ──────────────────────────────────────────────
# URL / 线程数归一化 + 运行时活跃池句柄
# ──────────────────────────────────────────────
def normalize_url(url: str) -> str:
    return str(url or "").strip()


def sanitize_threads(value, allowed=(1, 2), default=1) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value in allowed else default


_active_pool_lock = threading.Lock()
_active_pool: Optional["MultiServerDispatcher"] = None


def set_active_dispatcher(dispatcher) -> None:
    global _active_pool
    with _active_pool_lock:
        _active_pool = dispatcher


def clear_active_dispatcher(dispatcher=None) -> None:
    global _active_pool
    with _active_pool_lock:
        if dispatcher is None or _active_pool is dispatcher:
            _active_pool = None


def get_active_dispatcher():
    with _active_pool_lock:
        return _active_pool


def runtime_add_server(url, protocol=None, threads=None) -> bool:
    """供 GUI/控制接口调用：把节点加入当前正在运行的池。未运行时返回 False。"""
    if not LIVE_POOL_ENABLED:
        return False
    pool = get_active_dispatcher()
    return bool(pool and pool.add_server(url, protocol, threads))


def runtime_remove_server(url) -> bool:
    """供 GUI/控制接口调用：从当前正在运行的池优雅摘除节点。"""
    if not LIVE_POOL_ENABLED:
        return False
    pool = get_active_dispatcher()
    return bool(pool and pool.remove_server(url))


# ──────────────────────────────────────────────
# 核心分发器
# ──────────────────────────────────────────────
class MultiServerDispatcher:
    def __init__(self, server_configs: Dict[str, int],
                 max_retries: int = 3,
                 cpu_workers: int = 0,
                 queue_depth_mul: int = 2,
                 on_server_down: Optional[Callable] = None,
                 on_all_down: Optional[Callable] = None,
                 circuit_breaker_threshold: int = 3,
                 server_protocols: Optional[Dict[str, str]] = None,
                 on_pool_changed: Optional[Callable] = None):
        reset_circuit_breaker_states()  # 确保每轮新分发器启动时熔断状态干净

        self.server_configs = {
            normalize_url(u): sanitize_threads(n)
            for u, n in server_configs.items() if normalize_url(u)
        }
        self.server_protocols = {
            normalize_url(u): str(p).strip()
            for u, p in (server_protocols or {}).items() if normalize_url(u)
        }
        self.max_retries = max(1, max_retries)
        global _circuit_breaker_threshold
        # -1 表示禁用熔断；其他值至少为 1
        _circuit_breaker_threshold = circuit_breaker_threshold if circuit_breaker_threshold < 0 else max(1, circuit_breaker_threshold)
        total_gpu_threads = sum(self.server_configs.values())
        self.cpu_workers = cpu_workers if cpu_workers > 0 else max(2, os.cpu_count() or 4)
        self._on_server_down  = on_server_down
        self._on_all_down     = on_all_down
        self._on_pool_changed = on_pool_changed

        self._queue_depth_mul = max(1, int(queue_depth_mul))
        maxsize = max(total_gpu_threads * self._queue_depth_mul, 4)
        self._gpu_queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=maxsize)
        self._cpu_queue: queue.Queue = queue.Queue()

        # 每个 URL 一组 worker + 一个下线信号；支持运行中增删。
        self._gpu_workers: Dict[str, List[threading.Thread]] = {}
        self._worker_stop: Dict[str, threading.Event] = {}
        self._cpu_worker_threads: List[threading.Thread] = []

        self._server_lock = threading.RLock()
        self._spawn_lock  = threading.RLock()
        self._active_servers = set(self.server_configs.keys())
        self._pool_state = "RUNNING" if self._active_servers else "PAUSED_NO_SERVERS"
        self._pool_available = threading.Event()
        if self._active_servers:
            self._pool_available.set()
        self._pause_logged = False
        self._shutdown = False

        # 握手闸门：线程立刻创建，握手按 CONNECT_SPACING_S 串行错峰，避免 SSL 风暴。
        self._connect_gate = threading.Lock()
        self._last_connect_at = 0.0

        self._total_submitted = 0
        self._total_done      = 0
        self._done_lock       = threading.Lock()
        self._all_done_event  = threading.Event()
        self._abort_event     = threading.Event()

    # ── 池状态辅助 ──────────────────────────────
    def _all_gpu_threads(self) -> List[threading.Thread]:
        with self._spawn_lock:
            groups = list(self._gpu_workers.values())
        threads: List[threading.Thread] = []
        for group in groups:
            threads.extend(group)
        return threads

    def _has_active_servers(self) -> bool:
        with self._server_lock:
            return bool(self._active_servers)

    def _pool_paused(self) -> bool:
        with self._server_lock:
            return self._pool_state == "PAUSED_NO_SERVERS" and not self._abort_event.is_set()

    def _ensure_stop_event(self, url: str) -> threading.Event:
        ev = self._worker_stop.get(url)
        if ev is None:
            ev = threading.Event()
            self._worker_stop[url] = ev
        return ev

    def _connect_with_spacing(self, url: str):
        """按固定间隔串行开始握手；实际连接在闸门外执行。"""
        with self._connect_gate:
            wait_s = CONNECT_SPACING_S - (time.time() - self._last_connect_at)
            if wait_s > 0:
                time.sleep(wait_s)
            self._last_connect_at = time.time()
        return _get_client(url)

    def _resize_queue_capacity(self) -> None:
        try:
            with self._server_lock:
                total = sum(int(n) for n in self.server_configs.values())
            self._gpu_queue.maxsize = max(4, total * self._queue_depth_mul)
        except Exception:
            pass

    def _notify_pool_changed(self) -> None:
        callback = self._on_pool_changed
        if callback is None:
            return
        try:
            callback(self.pool_snapshot())
        except Exception as e:
            _log("POOL_WARN", f"服务器池变更回调异常: {e}")

    def _log_pause_if_needed(self) -> None:
        if self._pause_logged:
            return
        with self._server_lock:
            paused = not self._active_servers
        if paused:
            self._pause_logged = True
            _log("POOL_PAUSED",
                 f"无可用服务器，{self._gpu_queue.qsize()} 个任务挂起；加入节点后自动继续。")

    def _requeue_task(self, task: _GpuTask, reason: str = "") -> bool:
        task.is_requeued = True
        task.enqueue_time = time.time()
        try:
            self._gpu_queue.put(task, timeout=2.0)
            return True
        except Exception:
            _log("WARN", f"Task {task.task_id} 重新入队失败"
                         f"{(' (' + reason + ')') if reason else ''}，保持未完成状态。")
            return False

    def get_protocol(self, url: str) -> str:
        with self._server_lock:
            return self.server_protocols.get(url, "voxcpm2")

    def pool_snapshot(self) -> dict:
        workers = len(self._all_gpu_threads())
        with self._server_lock:
            active = sorted(self._active_servers)
            state = self._pool_state
            protocols = dict(self.server_protocols)
            configs = dict(self.server_configs)
        return {
            "state": state,
            "servers": active,
            "server_count": len(active),
            "threads": workers,
            "queue_size": self._gpu_queue.qsize(),
            "server_configs": configs,
            "server_protocols": protocols,
        }

    def start(self):
        # 不再在这里做第二轮“硬连通性探测”。
        # GUI 添加服务器时已经进行过一次软探测并确定协议；真正运行时由每个 Worker 自己建连，
        # 失败后走网络重试和连续失败熔断。这样 Cloudflare 的一次瞬时 TLS/SSL 握手
        # 失败不会把节点永久踢出本轮任务，也不会因为逐台 Client(url) 阻塞整个启动流程。
        _log("INIT", "📡 启动 GPU Worker；节点连接失败将自动重试，并按熔断阈值处理。")

        set_active_dispatcher(self)

        # 线程立即创建，握手由 worker 内部按 CONNECT_SPACING_S 串行错峰，
        # 因此 start() 不再阻塞 0.3s×线程数。
        with self._server_lock:
            pending = [
                (url, n, self.server_protocols.get(url, "voxcpm2"))
                for url, n in self.server_configs.items()
                if url in self._active_servers
            ]

        with self._spawn_lock:
            for url, n, proto in pending:
                self._ensure_stop_event(url)
                self._spawn_server_workers(url, proto, n)

            for i in range(self.cpu_workers):
                t = threading.Thread(
                    target=self._cpu_worker_loop,
                    name=f"CPU-{i}",
                    daemon=True
                )
                t.start()
                self._cpu_worker_threads.append(t)

        total = len(self._all_gpu_threads())
        with self._server_lock:
            cfg = ", ".join(
                f"{u.rstrip('/').split(':')[-1]}[{self.server_protocols.get(u, 'voxcpm2')}]×{n}"
                for u, n in self.server_configs.items()
            )
        _log("INIT", f"启动完成: GPU集群({cfg}) 共{total}线程 | "
                     f"CPU {self.cpu_workers}线程 | 队列容量 {self._gpu_queue.maxsize} | "
                     f"任务最大重试 {self.max_retries} 次 | "
                     f"熔断阈值 {'已禁用' if _circuit_breaker_threshold < 0 else f'{_circuit_breaker_threshold} 次连续重连失败'}")
        self._notify_pool_changed()

    # ── 运行时增删节点 ──────────────────────────
    def _spawn_server_workers(self, url: str, protocol: str, count: int) -> None:
        """创建指定数量的 worker（不阻塞、不做握手）。调用方需持有 _spawn_lock。"""
        with self._server_lock:
            if url not in self._active_servers:
                return
        stop_event = self._ensure_stop_event(url)
        tag = url.rstrip('/').split(':')[-1]
        for i in range(count):
            if self._shutdown or stop_event.is_set():
                break
            t = threading.Thread(
                target=self._gpu_worker_loop,
                args=(url, protocol, stop_event),
                name=f"GPU-{tag}-{i}",
                daemon=True,
            )
            t.start()
            self._gpu_workers.setdefault(url, []).append(t)

    def add_server(self, url, protocol=None, threads=None) -> bool:
        """把节点加入运行中的池；已存在或池已关闭时返回 False。"""
        url = normalize_url(url)
        if not url:
            return False

        if not LIVE_POOL_ENABLED:
            return False

        with self._spawn_lock:
            if self._shutdown:
                return False

            proto = str(protocol or self.server_protocols.get(url) or "voxcpm2").strip()
            n = sanitize_threads(
                threads if threads is not None else self.server_configs.get(url, 1)
            )

            with self._server_lock:
                if url in self._active_servers:
                    return False
                self.server_configs[url] = n
                self.server_protocols[url] = proto
                self._active_servers.add(url)
                self._pool_state = "RUNNING"
                self._pool_available.set()
                self._pause_logged = False

            # 重加同一 URL 必须清掉旧熔断计数，否则刚上线就可能被判死。
            with _cb_lock:
                _server_states.pop(url, None)

            self._ensure_stop_event(url)
            self._resize_queue_capacity()
            self._spawn_server_workers(url, proto, n)

        with self._server_lock:
            server_count = len(self.server_configs)
            thread_count = sum(self.server_configs.values())
        _log("POOL_ADD", f"{url} [{proto}]×{n} 已加入运行中的服务器池 "
                         f"（当前 {server_count} 台 / {thread_count} 线程）")
        self._notify_pool_changed()
        return True

    def remove_server(self, url) -> bool:
        """从运行中的池优雅摘除节点：不再派新任务，在途任务完成后退出。"""
        url = normalize_url(url)
        if not url:
            return False

        with self._spawn_lock:
            with self._server_lock:
                if url not in self._active_servers:
                    return False
                self._active_servers.discard(url)
                self.server_configs.pop(url, None)
                if not self._active_servers:
                    self._pool_state = "PAUSED_NO_SERVERS"
                    self._pool_available.clear()

            stop_event = self._worker_stop.get(url)
            if stop_event is not None:
                stop_event.set()
            threads = self._gpu_workers.pop(url, [])
            self._resize_queue_capacity()

        if threads:
            threading.Thread(
                target=self._join_workers,
                args=(url, threads, stop_event),
                name=f"POOL-DRAIN-{url.rstrip('/').split(':')[-1]}",
                daemon=True,
            ).start()

        _log("POOL_REMOVE", f"{url} 已从服务器池摘除：不再派新任务，在途任务完成后退出。")
        self._notify_pool_changed()
        return True

    def _join_workers(self, url: str, threads: List[threading.Thread], stop_event=None,
                      timeout: float = 600.0) -> None:
        deadline = time.time() + timeout
        for t in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if t.is_alive():
                t.join(remaining)
        alive = [t for t in threads if t.is_alive()]
        with self._spawn_lock:
            if stop_event is not None and self._worker_stop.get(url) is stop_event:
                self._worker_stop.pop(url, None)
        if alive:
            _log("POOL_WARN", f"{url} 仍有 {len(alive)} 个 worker 未退出（在途任务可能仍在运行）。")
        else:
            _log("POOL_REMOVED", f"{url} 的 worker 已全部退出。")

    def run_feeder(self,
                   task_iter: Iterable[Tuple],
                   post_process_fn: Callable,
                   done_callback: Callable,
                   abort_flag_fn: Callable[[], bool]):
        with self._done_lock:
            self._total_done = 0          # 重置已完成计数
            self._all_done_event.clear()  # 清除上一个文件的完成事件信号
            self._total_submitted = float('inf')
            self._pause_logged = False

        # 每个文件开始新一批任务时，清空产物指纹表，避免跨文件误判。
        _PRODUCT_REGISTRY.reset()
        cleanup_stale_raw_files()

        def _abort_requested() -> bool:
            return abort_flag_fn() or self._abort_event.is_set() or self._shutdown

        submitted = 0
        for task_id, ref_audio_path, gen_text, speed in task_iter:
            if _abort_requested():
                break

            task = _GpuTask(task_id, ref_audio_path, gen_text, speed,
                            post_process_fn, done_callback)

            # 避免阻塞式 put 导致在 abort/熔断时死锁
            while not _abort_requested():
                try:
                    self._gpu_queue.put(task, timeout=0.5)
                    submitted += 1
                    break
                except queue.Full:
                    self._log_pause_if_needed()
                    continue

        with self._done_lock:
            self._total_submitted = submitted
            if submitted == 0 or self._total_done >= submitted or _abort_requested():
                self._all_done_event.set()
                return

        while not _abort_requested():
            if self._all_done_event.wait(timeout=0.5):
                return

            # 池被用户清空：挂起等待，不当作故障终止，也不丢队列里的任务。
            if not self._has_active_servers():
                self._log_pause_if_needed()
                self._pool_available.wait(0.5)
                continue

            # 防御性检查：仍有 active 节点，但所有 GPU worker 线程均已退出 → 异常终止
            threads = self._all_gpu_threads()
            if threads and all(not t.is_alive() for t in threads):
                _log("DISPATCHER_WARN", "所有 GPU Worker 线程均已退出，结束当前批次等待。")
                self._abort_event.set()
                self._all_done_event.set()
                return

    def join(self):
        self._all_done_event.wait()

    def stop(self):
        self._shutdown = True
        self._abort_event.set()
        self._all_done_event.set()
        self._pool_available.set()

        with self._spawn_lock:
            for ev in self._worker_stop.values():
                ev.set()
            gpu_threads = self._all_gpu_threads()
            cpu_threads = list(self._cpu_worker_threads)

        for _ in gpu_threads:
            try:
                self._gpu_queue.put_nowait(_SENTINEL)
            except queue.Full:
                pass

        # 全局截止时间，避免逐线程 3s 串行等待导致退出被拖到几十秒。
        deadline = time.time() + 5.0
        for t in gpu_threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if t.is_alive():
                t.join(remaining)

        abandoned = 0
        try:
            while True:
                task = self._gpu_queue.get_nowait()
                self._gpu_queue.task_done()
                if task is _SENTINEL: continue
                abandoned += 1
        except queue.Empty:
            pass
        if abandoned:
            _log("STOP", f"调度器已停止，队列中 {abandoned} 个未完成任务已安全释放（保持未完成状态，供下次断点恢复）。")

        for _ in cpu_threads:
            try:
                self._cpu_queue.put_nowait(_SENTINEL)
            except queue.Full:
                pass
        cpu_deadline = time.time() + 3.0
        for t in cpu_threads:
            remaining = cpu_deadline - time.time()
            if remaining <= 0:
                break
            if t.is_alive():
                t.join(remaining)

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

        with self._server_lock:
            if self._pool_state != "ABORTED":
                self._pool_state = "STOPPED"
        with self._spawn_lock:
            self._gpu_workers.clear()
            self._worker_stop.clear()
        clear_active_dispatcher(self)

    @property
    def all_servers_down(self) -> bool:
        return self._abort_event.is_set()

    def _mark_server_down(self, url: str, reason: str):
        all_down = False
        stop_event = None
        threads: List[threading.Thread] = []
        with self._server_lock:
            if url not in self._active_servers:
                return
            self._active_servers.discard(url)
            self.server_configs.pop(url, None)  # 不再参与容量/启动日志；协议保留给在途后处理
            stop_event = self._worker_stop.get(url)
            if stop_event is not None:
                stop_event.set()
            if not self._active_servers:
                self._pool_available.clear()
                if self._pool_state != "PAUSED_NO_SERVERS":
                    self._pool_state = "ABORTED"
                    all_down = True

        with self._spawn_lock:
            threads = self._gpu_workers.pop(url, [])
            self._resize_queue_capacity()

        _log("SERVER_DOWN", f"!! 服务器下线: {url} | {reason}")
        if self._on_server_down:
            try: self._on_server_down(url, reason)
            except Exception: pass

        if threads:
            threading.Thread(
                target=self._join_workers,
                args=(url, threads, stop_event),
                name=f"POOL-DRAIN-{url.rstrip('/').split(':')[-1]}",
                daemon=True,
            ).start()

        if all_down:
            _log("ALL_DOWN", "!! 所有服务器均已下线，停止程序！")
            self._abort_event.set()
            self._all_done_event.set()
            if self._on_all_down:
                try: self._on_all_down()
                except Exception: pass

        self._notify_pool_changed()

    def _gpu_worker_loop(self, url: str, protocol: str, stop_event: threading.Event):
        try:
            self._gpu_worker_body(url, protocol, stop_event)
        finally:
            _close_thread_clients(url)

    def _gpu_worker_body(self, url: str, protocol: str, stop_event: threading.Event):
        worker_name = threading.current_thread().name

        # ✨ Worker 线程启动时立即建立本线程的独立连接，避免首批任务集体等待建连。
        # 握手按全局闸门错峰，start()/add_server() 不再被 sleep 阻塞。
        try:
            self._connect_with_spacing(url)
            if DEBUG_DISPATCHER:
                _log("WORKER_READY", f"{worker_name} 连接预热完成: {url} [{protocol}]")
        except Exception as e:
            _log("WORKER_INIT_ERR", f"{worker_name} 连接预热失败: {url} ({e})")
            # 首次连接失败：标记错误重连并立即评估
            invalidate_client(url, error=True)
            if _evaluate_reconnect(url):
                state = _get_server_state(url)
                with _cb_lock:
                    cf = state.consecutive_failures
                reason = f"启动时连续 {cf} 次连接失败，服务器熔断"
                _log("CIRCUIT_BREAK", f"!! {url} | {reason}")
                self._mark_server_down(url, reason)

        last_success_time = time.time()
        local_retry_task = None
        idle_since = None

        while True:
            if self._abort_event.is_set() or self._shutdown or stop_event.is_set():
                break
            with self._server_lock:
                is_down = url not in self._active_servers
            if is_down:
                break

            task_from_queue = local_retry_task is None
            if local_retry_task is not None:
                task = local_retry_task
                local_retry_task = None
            else:
                try:
                    # 0.5s 轮询：让“运行中摘除节点”最多半秒内被感知
                    task = self._gpu_queue.get(timeout=0.5)
                except queue.Empty:
                    now = time.time()
                    if idle_since is None:
                        idle_since = now
                    elif now - idle_since >= IDLE_WARN_THRESHOLD_S:
                        if DEBUG_DISPATCHER:
                            _log("WORKER_IDLE", f"{worker_name} idle >{IDLE_WARN_THRESHOLD_S}s")
                        idle_since = now
                    continue
                idle_since = None

            if task is _SENTINEL:
                self._gpu_queue.task_done()
                break

            # 节点可能在上面的 get() 阻塞期间被摘除：把刚取到的任务放回队列，不再处理。
            if stop_event.is_set() or self._abort_event.is_set() or self._shutdown:
                if task_from_queue:
                    self._gpu_queue.task_done()
                if not (self._abort_event.is_set() or self._shutdown):
                    self._requeue_task(task, "节点已摘除")
                local_retry_task = None
                break

            if DEBUG_DISPATCHER:
                queue_wait_s = time.time() - task.enqueue_time
                if queue_wait_s > 2.0:
                    _log("QUEUE_WAIT", f"Task {task.task_id} 排队等待了 {queue_wait_s:.2f}s")
                _log("WORKER_PICK", f"{worker_name} 取出 Task {task.task_id} (retry={task.retry_count})")

            # 连接正常时跨任务批次复用；只有长时间空闲才做一次预防性重连。
            idle_s = time.time() - last_success_time
            if idle_s > RECONNECT_IDLE_S:
                if DEBUG_DISPATCHER:
                    _log("RECONNECT", f"{worker_name} 空闲 {idle_s:.1f}s，取任务前重连 {url}")
                invalidate_client(url, error=False)  # 空闲重连，不计入熔断
                try:
                    _get_client(url)
                    last_success_time = time.time()
                except Exception:
                    pass  # 连接失败交给后续 _call_api_on_server 的重试机制处理

            raw_path = None
            success  = False
            fail_kind = _FAIL_API
            try:
                q_depth = self._gpu_queue.qsize()

                # ✨ 静态协议驱动分发：根据当前 Worker 绑定的协议直接调用，避免每次请求重复判断
                raw_path, fail_kind = _call_api_on_server(
                    url, task.ref_audio_path, task.gen_text, task.speed,
                    task_id=task.task_id, queue_size=q_depth,
                    network_attempt=task.network_retry_count + 1,
                    protocol=protocol,
                )
                success = raw_path is not None
            except Exception as e:
                _log("WORKER_ERR", f"{worker_name} Task {task.task_id} 异常: {e}")
                success = False
                fail_kind = _FAIL_API
            finally:
                if success:
                    task.network_retry_count = 0
                    last_success_time = time.time()
                    _record_api_success(url)  # 熔断追踪：记录成功产出
                    self._cpu_queue.put(_CpuTask(
                        task.task_id, raw_path, url,
                        task.post_process_fn, task.done_callback
                    ))
                    if task_from_queue:
                        self._gpu_queue.task_done()
                else:
                    # ── 熔断评估：失败时立即评估，不依赖下次 _get_client ──
                    if _evaluate_reconnect(url):
                        state = _get_server_state(url)
                        with _cb_lock:
                            cf = state.consecutive_failures
                        reason = f"连续 {cf} 次重连无成功产出，服务器熔断"
                        _log("CIRCUIT_BREAK", f"!! {url} | {reason}")
                        self._mark_server_down(url, reason)

                        if self._abort_event.is_set():
                            # 已终止/全部熔断：保留未完成状态，严禁转静音占位！
                            _log("CIRCUIT_DROP", f"无可用活跃服务器，Task {task.task_id} 保持未完成状态（供下次恢复）。")
                        elif not self._has_active_servers():
                            if self._pool_paused():
                                _log("POOL_HOLD",
                                     f"Task {task.task_id} 在节点 {url} 失败；池已暂停，任务留在队列等待新节点。")
                                task.network_retry_count = 0
                                self._requeue_task(task, "池已暂停")
                            else:
                                _log("CIRCUIT_DROP", f"无可用活跃服务器，Task {task.task_id} 保持未完成状态（供下次恢复）。")
                        else:
                            _log("RETRY_CIRCUIT", f"Task {task.task_id} 因节点 {url} 熔断重新入队，由其他可用节点接替处理。")
                            task.network_retry_count = 0  # 重置网络尝试计数，交给其他可用节点接替
                            self._requeue_task(task, "节点熔断")
                        if task_from_queue:
                            self._gpu_queue.task_done()
                        break

                    # 网络/连接错误：不消耗配音失败次数，但必须有独立上限，避免无限卡死。
                    if fail_kind == _FAIL_NETWORK:
                        if self._abort_event.is_set():
                            if task_from_queue:
                                self._gpu_queue.task_done()
                            break

                        task.network_retry_count += 1
                        if task.network_retry_count >= MAX_NETWORK_ATTEMPTS_PER_TASK:
                            with self._server_lock:
                                other_servers = [s for s in self._active_servers if s != url]
                            if other_servers and not self._abort_event.is_set():
                                _log(
                                    "NET_MIGRATE",
                                    f"Task {task.task_id} 在节点 {url} 连续网络失败 {MAX_NETWORK_ATTEMPTS_PER_TASK} 次，"
                                    f"重新入队调度至其他存活节点。"
                                )
                                task.is_requeued = True
                                task.network_retry_count = 0
                                task.enqueue_time = time.time()
                                try:
                                    self._gpu_queue.put(task, timeout=2.0)
                                except Exception:
                                    _log("WARN", f"Task {task.task_id} 跨节点重新入队超时，保持未完成状态。")
                            elif self._pool_paused():
                                _log(
                                    "NET_HOLD_PAUSED",
                                    f"Task {task.task_id} 网络重试达到上限；池已暂停，任务留在队列等待新节点。"
                                )
                                task.network_retry_count = 0
                                self._requeue_task(task, "池已暂停")
                            else:
                                _log(
                                    "NET_HOLD",
                                    f"Task {task.task_id} 网络重试达到上限且无其他可用节点，保持未完成状态（不写入静音占位）。"
                                )
                            if task_from_queue:
                                self._gpu_queue.task_done()
                            continue

                        backoff_s = min(NETWORK_RETRY_BACKOFF_MAX_S, 0.8 * (2 ** min(task.network_retry_count - 1, 5)))
                        task.is_requeued = True
                        task.enqueue_time = time.time() + backoff_s
                        if DEBUG_DISPATCHER or task.network_retry_count in (1, 3):
                            _log(
                                "RETRY_NET",
                                f"Task {task.task_id} 将在 {backoff_s:.1f}s 后重试网络连接。"
                            )

                        # PriorityQueue 没有延迟队列，这里由当前 worker 小睡后再放回，降低 SSL 重连风暴。
                        time.sleep(backoff_s)
                        if stop_event.is_set() or self._abort_event.is_set() or self._shutdown:
                            self._requeue_task(task, "节点已摘除")
                            if task_from_queue:
                                self._gpu_queue.task_done()
                            break
                        try:
                            self._gpu_queue.put_nowait(task)
                        except queue.Full:
                            local_retry_task = task
                            _log(
                                "RETRY_LOCAL",
                                f"Task {task.task_id} GPU queue full; {worker_name} will retry it locally to avoid deadlock"
                            )
                        if task_from_queue:
                            self._gpu_queue.task_done()

                    # API/推理/返回空：这才算“配音任务失败”，消耗 retry_count
                    else:
                        task.retry_count += 1
                        task_exhausted = task.retry_count > self.max_retries

                        if not task_exhausted and not self._abort_event.is_set():
                            task.is_requeued = True
                            task.enqueue_time = time.time()
                            _log("RETRY", f"Task {task.task_id} 配音失败，第 {task.retry_count}/{self.max_retries} 次重试，重新入队（优先）…")
                            try:
                                self._gpu_queue.put_nowait(task)
                            except queue.Full:
                                local_retry_task = task
                                _log(
                                    "RETRY_LOCAL",
                                    f"Task {task.task_id} GPU queue full; {worker_name} will retry it locally to avoid deadlock"
                                )
                            if task_from_queue:
                                self._gpu_queue.task_done()
                        else:
                            if self._abort_event.is_set():
                                if task_from_queue:
                                    self._gpu_queue.task_done()
                                break
                            _log("API_DROP", f"Task {task.task_id} 配音重试超限 ({self.max_retries} 次)，降级为静音占位。")
                            self._cpu_queue.put(_CpuTask(
                                task.task_id, None, url,
                                task.post_process_fn, task.done_callback
                            ))
                            if task_from_queue:
                                self._gpu_queue.task_done()

        # 退出循环后兜底回收未处理的 local_retry_task
        if local_retry_task is not None:
            if self._abort_event.is_set() or self._shutdown:
                _log("WORKER_EXIT", f"节点退出，Task {local_retry_task.task_id} 保持未完成状态。")
            else:
                # 池暂停时也放回队列：节点重新加入后可以直接续跑
                local_retry_task.is_requeued = True
                local_retry_task.enqueue_time = time.time()
                try:
                    self._gpu_queue.put(local_retry_task, timeout=1.0)
                except Exception:
                    _log("WARN", f"Task {local_retry_task.task_id} 退出兜底入队超时，保持未完成状态。")
            local_retry_task = None

    def _cpu_worker_loop(self):
        worker_name = threading.current_thread().name
        while True:
            try:
                task = self._cpu_queue.get(timeout=1.0)
            except queue.Empty:
                if self._abort_event.is_set():
                    break
                continue
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
