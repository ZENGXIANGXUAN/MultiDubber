"""
crash_guard.py — 兜底崩溃日志

解决“程序闪退但普通 logger 捕捉不到”的几类情况：
1. 主线程未捕获异常
2. 后台线程未捕获异常
3. __del__/atexit 等不可抛出异常
4. Python 层 fatal error / C 扩展崩溃（尽量通过 faulthandler 写入）

注意：如果进程被系统强杀、断电、任务管理器结束、OS OOM Killer 直接杀死，任何 Python 日志都来不及写。
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import tempfile
import threading
import traceback
from datetime import datetime

_installed = False
_crash_file = None
_crash_path = None
_lock = threading.RLock()


def _default_log_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "logs"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "MultTTS", "logs") if os.environ.get("LOCALAPPDATA") else "",
        os.path.join(tempfile.gettempdir(), "MultTTS", "logs"),
    ]
    for path in candidates:
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except Exception:
            continue
    return tempfile.gettempdir()


def _direct_write(title: str, body: str = "") -> None:
    """不依赖 logger 的最低层写入，避免 logger 本身异常时丢日志。"""
    global _crash_file
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    text = f"\n[{ts}] {title}\n{body}\n"
    with _lock:
        try:
            if _crash_file:
                _crash_file.write(text)
                _crash_file.flush()
        except Exception:
            pass


def _log_via_main_logger(tag: str, msg: str) -> None:
    try:
        from logger import log
        log(tag, msg)
    except Exception:
        # 不能让崩溃日志系统再次制造崩溃。
        pass


def _handle_exception(exc_type, exc, tb) -> None:
    body = "".join(traceback.format_exception(exc_type, exc, tb))
    _direct_write("UNCAUGHT MAIN THREAD EXCEPTION", body)
    _log_via_main_logger("FATAL", body)


def _handle_thread_exception(args) -> None:
    body = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    thread_name = getattr(args.thread, "name", "<unknown>")
    _direct_write(f"UNCAUGHT THREAD EXCEPTION | {thread_name}", body)
    _log_via_main_logger("THREAD_FATAL", f"[{thread_name}]\n{body}")


def _handle_unraisable(unraisable) -> None:
    exc = unraisable.exc_value
    tb = unraisable.exc_traceback
    obj = getattr(unraisable, "object", None)
    body = f"object={obj!r}\n" + "".join(traceback.format_exception(type(exc), exc, tb))
    _direct_write("UNRAISABLE EXCEPTION", body)
    _log_via_main_logger("UNRAISABLE", body)


def install_crash_guard(app_name: str = "MultTTS", log_dir: str | None = None) -> str:
    """安装全局崩溃捕捉器，返回 crash 日志路径。"""
    global _installed, _crash_file, _crash_path
    if _installed:
        return _crash_path or ""

    if log_dir is None:
        log_dir = _default_log_dir()
    else:
        os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _crash_path = os.path.join(log_dir, f"{app_name}_CRASH_{timestamp}.log")
    _crash_file = open(_crash_path, "a", encoding="utf-8", buffering=1)

    _direct_write("CRASH GUARD INSTALLED", f"pid={os.getpid()}\npython={sys.version}\nexecutable={sys.executable}")

    try:
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception as e:
        _direct_write("FAULTHANDLER ENABLE FAILED", repr(e))

    sys.excepthook = _handle_exception
    if hasattr(threading, "excepthook"):
        threading.excepthook = _handle_thread_exception
    if hasattr(sys, "unraisablehook"):
        sys.unraisablehook = _handle_unraisable

    def _on_exit():
        _direct_write("PROCESS EXIT", f"pid={os.getpid()}")
        try:
            if _crash_file:
                _crash_file.flush()
        except Exception:
            pass

    atexit.register(_on_exit)
    _installed = True
    return _crash_path


def get_crash_log_path() -> str | None:
    return _crash_path
