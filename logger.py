"""
logger.py — 本地日志文件系统

所有模块共享同一个 Logger 实例，日志同时写入：
  1. 文件（logs/ 目录下，按启动时间命名）
  2. 控制台（stdout，可用时）
  3. GUI 回调（可选）

重点修复：无控制台 exe / GUI 模式下 sys.stdout 可能为 None，不能让 logger 自己把程序打崩。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime


def _safe_log_dir() -> str:
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


class Logger:
    def __init__(self):
        self._file = None
        self._log_path = None
        self._gui_callback = None
        self._lock = threading.RLock()
        self._initialized = False

    def init(self, log_dir: str | None = None, name: str = "MultTTS") -> str:
        with self._lock:
            if self._initialized:
                return self._log_path

            if log_dir is None:
                log_dir = _safe_log_dir()
            else:
                try:
                    os.makedirs(log_dir, exist_ok=True)
                except Exception:
                    log_dir = _safe_log_dir()

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_path = os.path.join(log_dir, f"{name}_{timestamp}.log")
            try:
                # buffering=1 行缓冲，闪退前更容易落盘。
                self._file = open(self._log_path, "a", encoding="utf-8", buffering=1)
            except Exception:
                fallback = os.path.join(tempfile.gettempdir(), f"{name}_{timestamp}.log")
                self._log_path = fallback
                self._file = open(fallback, "a", encoding="utf-8", buffering=1)

            self._initialized = True

        self.log("INIT", f"日志文件: {self._log_path}")
        return self._log_path

    def set_gui_callback(self, callback) -> None:
        self._gui_callback = callback

    @property
    def log_path(self):
        return self._log_path

    def _ts(self) -> str:
        now = datetime.now()
        return now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"

    def _safe_console_write(self, line: str) -> None:
        stream = getattr(sys, "stdout", None)
        if stream is None:
            return
        try:
            if getattr(stream, "closed", False):
                return
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            pass

    def _write(self, line: str):
        with self._lock:
            try:
                if self._file:
                    self._file.write(line + "\n")
                    self._file.flush()
            except Exception:
                # 日志写文件失败不能导致主程序崩溃。
                pass

        self._safe_console_write(line)

        if self._gui_callback:
            try:
                self._gui_callback(line)
            except Exception:
                pass

    def _ensure_init(self):
        if not self._initialized:
            self.init()

    def log(self, tag: str, msg: str) -> None:
        self._ensure_init()
        self._write(f"[{self._ts()}][{tag}] {msg}")

    def info(self, msg: str) -> None:
        self.log("INFO", msg)

    def warning(self, msg: str) -> None:
        self.log("WARN", msg)

    def error(self, msg: str) -> None:
        self.log("ERROR", msg)

    def close(self) -> None:
        with self._lock:
            try:
                if self._file:
                    self._file.flush()
                    self._file.close()
            except Exception:
                pass
            finally:
                self._file = None
                self._initialized = False


# ── 全局单例 ──────────────────────────────────────────
_logger = Logger()


def init_logger(log_dir: str | None = None, name: str = "MultTTS") -> str:
    """初始化日志系统，返回日志文件路径。幂等，重复调用不会重复初始化。"""
    return _logger.init(log_dir, name)


def set_gui_callback(callback) -> None:
    """设置 GUI 日志回调（将日志行推送到 GUI 文本框）。"""
    _logger.set_gui_callback(callback)


def get_log_path() -> str | None:
    """返回当前日志文件路径，未初始化返回 None。"""
    return _logger.log_path


def log(tag: str, msg: str) -> None:
    """主日志接口：写入文件 + 控制台 + GUI。"""
    _logger.log(tag, msg)


def info(msg: str) -> None:
    _logger.info(msg)


def warning(msg: str) -> None:
    _logger.warning(msg)


def error(msg: str) -> None:
    _logger.error(msg)


def close_logger() -> None:
    _logger.close()
