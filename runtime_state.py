# runtime_state.py
"""
运行时状态模块 —— 记录当前 pipeline 的运行状态。

不处理业务逻辑，只保存状态供 control_server 查询。
"""

import threading
import time

_lock = threading.RLock()

pipeline_running = False
all_servers_down = False
stop_reason = None  # None | "USER_ABORT" | "ALL_SERVERS_DOWN" | "RESTART_REQUESTED"

last_job_context = None

active_servers = set()
down_servers = {}

last_started_at = None
last_stopped_at = None
restart_running = False


def register_job_context(**kwargs):
    """保存本次 process_srt_files 的完整启动参数，供后续重启使用。"""
    global last_job_context
    with _lock:
        last_job_context = dict(kwargs)


def get_last_job_context():
    """获取上次保存的流程启动参数（副本）。"""
    with _lock:
        return dict(last_job_context) if last_job_context else None


def mark_pipeline_started():
    """标记主流程已启动。"""
    global pipeline_running, last_started_at, all_servers_down, stop_reason
    with _lock:
        pipeline_running = True
        all_servers_down = False
        stop_reason = None
        last_started_at = time.time()
        down_servers.clear()


def mark_pipeline_stopped(reason=None):
    """标记主流程已停止。"""
    global pipeline_running, stop_reason, last_stopped_at
    with _lock:
        pipeline_running = False
        stop_reason = reason
        last_stopped_at = time.time()


def mark_all_servers_down(reason="ALL_SERVERS_DOWN"):
    """标记所有服务器已熔断。"""
    global all_servers_down, stop_reason, last_stopped_at
    with _lock:
        all_servers_down = True
        stop_reason = reason
        last_stopped_at = time.time()


def mark_server_down(url, reason):
    """记录单台服务器下线。"""
    with _lock:
        down_servers[url] = str(reason)
        active_servers.discard(url)


def mark_server_active(url):
    """记录单台服务器上线。"""
    with _lock:
        active_servers.add(url)
        down_servers.pop(url, None)


def update_job_servers(server_configs, server_protocols=None):
    """运行中增删节点后，把最新池同步到状态与重启上下文。"""
    global last_job_context, all_servers_down
    with _lock:
        if last_job_context is not None:
            last_job_context["server_configs"] = dict(server_configs or {})
            if server_protocols is not None:
                last_job_context["server_protocols"] = dict(server_protocols)
        active_servers.clear()
        active_servers.update((server_configs or {}).keys())
        for url in list(active_servers):
            down_servers.pop(url, None)
        if active_servers:
            all_servers_down = False


def reset_before_pipeline_restart():
    """重启整条流程前重置状态。"""
    global all_servers_down, stop_reason, restart_running
    with _lock:
        all_servers_down = False
        stop_reason = None
        restart_running = False
        down_servers.clear()


def snapshot():
    """返回当前状态快照（dict）。"""
    with _lock:
        return {
            "pipeline_running": pipeline_running,
            "all_servers_down": all_servers_down,
            "stop_reason": stop_reason,
            "can_restart_pipeline": (
                not pipeline_running
                and last_job_context is not None
            ),
            "active_servers": list(active_servers),
            "down_servers": dict(down_servers),
            "last_started_at": last_started_at,
            "last_stopped_at": last_stopped_at,
            "restart_running": restart_running,
            "has_last_job_context": last_job_context is not None,
        }
