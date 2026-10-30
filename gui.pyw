import sys
import os
import faulthandler
import traceback
import json
import threading
import urllib.request
import urllib.parse

# ── 闪退捕获：在任何其他 import 之前安装三层安全网 ───────────
# 1) faulthandler — 捕获 C 级崩溃（segfault / access violation），写入文件
_CRASH_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_CRASH_LOG_DIR, exist_ok=True)
_CRASH_LOG_PATH = os.path.join(_CRASH_LOG_DIR, "crash.log")
_crash_log_file = open(_CRASH_LOG_PATH, "a", encoding="utf-8")
faulthandler.enable(file=_crash_log_file)

# 2) sys.excepthook — 捕获主线程未处理异常
def _global_excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        _crash_log_file.write(f"\n{'='*60}\n[UNCAUGHT EXCEPTION]\n{msg}\n")
        _crash_log_file.flush()
    except Exception:
        pass
    # 也尝试写入 logger 文件（如果已初始化）
    try:
        from logger import error as _log_error
        _log_error(f"UNCAUGHT: {msg}")
    except Exception:
        pass
sys.excepthook = _global_excepthook

# 3) threading.excepthook — 捕获非主线程未处理异常（Python 3.8+）
def _threading_excepthook(args):
    msg = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_tb))
    try:
        _crash_log_file.write(f"\n{'='*60}\n[THREAD EXCEPTION] thread={args.thread}\n{msg}\n")
        _crash_log_file.flush()
    except Exception:
        pass
    try:
        from logger import error as _log_error
        _log_error(f"THREAD EXCEPTION: {msg}")
    except Exception:
        pass
threading.excepthook = _threading_excepthook
# ──────────────────────────────────────────────────────────────

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton,
                             QTextEdit, QFileDialog, QLabel, QLineEdit, QHBoxLayout,
                             QGroupBox, QProgressBar, QStyleFactory, QListWidget,
                             QListWidgetItem, QAbstractItemView, QSystemTrayIcon, QMenu,
                             QDialog, QFormLayout, QDialogButtonBox, QSpinBox, QMessageBox,
                             QCheckBox)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QObject, QTimer
from PyQt6.QtGui import QPalette, QColor, QIcon, QPixmap, QPainter, QBrush, QCursor
import config
from main import process_srt_files
from api_client import test_connection
from dispatcher import runtime_add_server, runtime_remove_server
from logger import init_logger, set_gui_callback, log as file_log, get_log_path

# ── 本地设置持久化 ──────────────────────────────────────────
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_settings.json")

DEFAULT_SETTINGS = {
    "srt_paths": [],
    "line_index": 2,
    "max_retries": 5,
    "circuit_breaker_threshold": 6,
    "wxpusher_app_token": "",
    "wxpusher_uid": "",
    "servers": [],
    "server_protocols": {},  # url -> "indextts_15" / "voxcpm2"
    "server_threads_per_server": 1,  # 每台服务器并发线程数
    "merge_max_chars": 30,  # 字幕合并字数上限，0=禁用
    "background_merge": False,  # 后台合并音视频
}

_REMOVED_SETTINGS_KEYS = ("api_public_key", "api_private_key", "api_hostid")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 补全缺失的键
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            # 旧版本按总线程数均分服务器；现在每台在线服务器固定 2 线程
            data.pop("total_threads", None)
            # 云主机自动关机功能已移除，同时清理旧版本遗留的明文凭据。
            had_removed_settings = any(key in data for key in _REMOVED_SETTINGS_KEYS)
            for key in _REMOVED_SETTINGS_KEYS:
                data.pop(key, None)
            # migrate old single-path setting
            if not data.get("srt_paths") and data.get("srt_path"):
                data["srt_paths"] = [data["srt_path"]]
            if had_removed_settings:
                save_settings(data)
            return data
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(data: dict):
    try:
        cleaned_data = {k: v for k, v in data.items() if k not in _REMOVED_SETTINGS_KEYS}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        file_log("SETTINGS", f"保存失败: {e}")


# ── WxPusher 通知 ──────────────────────────────────────────
_WXPUSHER_API_URL = "https://wxpusher.zjiecode.com/api/send/message"


def send_webhook(app_token: str, uid: str, title: str, content: str):
    """在后台线程发送 WxPusher 通知"""
    if not app_token or not uid:
        return

    def _send():
        try:
            import json as _json
            data = _json.dumps({
                "appToken": app_token,
                "content": content,
                "summary": title[:100],
                "contentType": 1,
                "uids": [uid],
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                _WXPUSHER_API_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                result = _json.loads(body)
                if result.get("code") == 1000:
                    file_log("WEBHOOK", f"WxPusher 发送成功: {title}")
                else:
                    file_log("WEBHOOK", f"WxPusher 发送失败: {result.get('msg')}")
        except Exception as e:
            file_log("WEBHOOK", f"WxPusher 发送异常: {e}")

    threading.Thread(target=_send, daemon=True).start()


# ── 图标 & 声音工具 ──────────────────────────────────────────
def _make_icon(color: str) -> QIcon:
    px = QPixmap(64, 64)
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(QColor(color)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, 56, 56)
    painter.end()
    return QIcon(px)


def _play_sound(sound_type: str):
    try:
        if os.name == 'nt':
            import winsound
            if sound_type == 'finish':
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        else:
            QApplication.beep()
    except Exception:
        QApplication.beep()


# ══════════════════════════════════════════════════════════════
# 设置对话框
# ══════════════════════════════════════════════════════════════
class SettingsDialog(QDialog):
    def __init__(self, parent=None,
                 line_index: int = 2,
                 max_retries: int = 5,
                 circuit_breaker_threshold: int = 6,
                 wxpusher_app_token: str = "",
                 wxpusher_uid: str = "",
                 merge_max_chars: int = 30,
                 background_merge: bool = False,
                 server_threads_per_server: int = 1):
        super().__init__(parent)
        self.setWindowTitle("⚙️  Settings")
        self.setMinimumWidth(500)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(12)

        # Line Index
        self.spin_line = QSpinBox()
        self.spin_line.setRange(0, 20)
        self.spin_line.setValue(line_index)
        self.spin_line.setMinimumHeight(32)
        self.spin_line.setToolTip("字幕文件中翻译文本所在的行号（从0开始）")
        form.addRow("Line Index:", self.spin_line)

        # Server Threads Per Server
        self.spin_server_threads = QSpinBox()
        self.spin_server_threads.setRange(1, 2)
        self.spin_server_threads.setValue(server_threads_per_server)
        self.spin_server_threads.setMinimumHeight(32)
        self.spin_server_threads.setToolTip(
            "每台在线服务器的生成并发线程数（推荐 1 或 2）。\n"
            "支持 IndexTTS 1.5 与 VoxCPM 2.0 节点自动负载均衡。"
        )
        form.addRow("每服务器并发数:", self.spin_server_threads)

        # Merge Max Chars
        self.spin_merge_chars = QSpinBox()
        self.spin_merge_chars.setRange(0, 200)
        self.spin_merge_chars.setValue(merge_max_chars)
        self.spin_merge_chars.setMinimumHeight(32)
        self.spin_merge_chars.setToolTip(
            "合并后文本的最大字符数。\n"
            "连续字幕在不超此限制时会合并为一条，减少 API 调用。\n"
            "设为 0 禁用合并功能。"
        )
        form.addRow("合并字数上限:", self.spin_merge_chars)

        # Background Merge
        self.chk_background_merge = QCheckBox("后台合并音视频")
        self.chk_background_merge.setChecked(background_merge)
        self.chk_background_merge.setMinimumHeight(32)
        self.chk_background_merge.setToolTip(
            "勾选后，音频合成完成后将在后台线程执行视频合并，\n"
            "不阻塞主界面，可继续添加新任务。"
        )
        form.addRow("后台合并:", self.chk_background_merge)

        # Max Retries
        self.spin_retries = QSpinBox()
        self.spin_retries.setMinimum(1)
        self.spin_retries.setMaximum(2147483647)
        self.spin_retries.setValue(max_retries)
        self.spin_retries.setMinimumHeight(32)
        self.spin_retries.setToolTip(
            "单个任务失败后的最大重试次数。\n"
            "同时：某台服务器连续失败达到此次数后将自动下线。"
        )
        form.addRow("最大重试次数:", self.spin_retries)

        # Circuit Breaker Threshold
        self.spin_cb_threshold = QSpinBox()
        self.spin_cb_threshold.setRange(-1, 50)
        self.spin_cb_threshold.setValue(circuit_breaker_threshold)
        self.spin_cb_threshold.setMinimumHeight(32)
        self.spin_cb_threshold.setToolTip(
            "服务器熔断阈值：连续无效重连达到此次数后，服务器自动下线。\n"
            "设为 -1 表示禁用熔断（服务器永远不会被自动下线）。\n"
            "Cloudflare 穿透环境下建议设为 6+，避免网络抖动导致误判。"
        )
        form.addRow("熔断阈值:", self.spin_cb_threshold)

        # WxPusher 通知
        self.wx_app_token_input = QLineEdit(wxpusher_app_token)
        self.wx_app_token_input.setMinimumHeight(32)
        self.wx_app_token_input.setPlaceholderText("AT_xxx（在 WxPusher 后台获取）")
        self.wx_app_token_input.setToolTip("WxPusher 应用 Token，留空则不发送通知")
        form.addRow("APP Token:", self.wx_app_token_input)

        self.wx_uid_input = QLineEdit(wxpusher_uid)
        self.wx_uid_input.setMinimumHeight(32)
        self.wx_uid_input.setPlaceholderText("UID_xxx（关注公众号后获取）")
        self.wx_uid_input.setToolTip("WxPusher 用户 UID，留空则不发送通知")
        form.addRow("UID:", self.wx_uid_input)

        layout.addLayout(form)

        # 操作按钮区：测试 WxPusher & 清理本地缓存
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        test_btn = QPushButton("📡 测试 WxPusher")
        test_btn.setObjectName("checkBtn")
        test_btn.setMinimumHeight(32)
        test_btn.clicked.connect(self._test_wxpusher)
        btn_layout.addWidget(test_btn)

        cache_btn = QPushButton("🧹 清理本地缓存")
        cache_btn.setObjectName("checkBtn")
        cache_btn.setMinimumHeight(32)
        cache_btn.setToolTip("清理云盘骨架缓存与临时工作区（%TEMP%\\MultTTS_Cache）")
        cache_btn.clicked.connect(self._clear_cache)
        btn_layout.addWidget(cache_btn)

        layout.addLayout(btn_layout)

        # 说明文字
        hint = QLabel(
            "WxPusher 微信推送通知。<br>"
            "APP Token 和 UID 在 <a href='https://wxpusher.zjiecode.com'>WxPusher 后台</a>获取。<br>"
            "服务器熔断或任务完成时自动推送通知到你的微信。"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setOpenExternalLinks(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # OK / Cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _test_wxpusher(self):
        token = self.wx_app_token_input.text().strip()
        uid = self.wx_uid_input.text().strip()
        if not token or not uid:
            QMessageBox.warning(self, "提示", "请先填写 APP Token 和 UID")
            return
        send_webhook(token, uid, "Auto Dubbing Studio 测试", "WxPusher 测试消息，连接正常 ✅")
        QMessageBox.information(self, "已发送", "测试通知已在后台发送，请查看微信。")

    def _clear_cache(self):
        import tempfile
        import shutil
        cache_dir = getattr(config, 'LOCAL_CACHE_DIR', None)
        if not cache_dir:
            cache_dir = os.path.join(tempfile.gettempdir(), "MultTTS_Cache")

        if not os.path.exists(cache_dir):
            QMessageBox.information(self, "缓存清理", "当前没有本地缓存需要清理。")
            return

        reply = QMessageBox.question(
            self, "确认清理",
            f"确定要清理本地缓存目录吗？\n\n路径: {cache_dir}\n\n清理后下次处理云盘文件时将重新同步骨架。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            total_bytes = 0
            for root, dirs, files in os.walk(cache_dir):
                for f in files:
                    try:
                        total_bytes += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            size_mb = total_bytes / 1024 / 1024
            shutil.rmtree(cache_dir)
            file_log("CACHE", f"用户手动清理缓存: {cache_dir} (释放 {size_mb:.1f} MB)")
            QMessageBox.information(self, "清理成功", f"本地缓存已清理完毕！\n释放空间: {size_mb:.1f} MB")
        except Exception as e:
            file_log("CACHE", f"手动清理缓存失败: {e}")
            QMessageBox.critical(self, "清理失败", f"清理缓存时发生错误: {e}")

    @property
    def line_index(self) -> int:
        return self.spin_line.value()

    @property
    def max_retries(self) -> int:
        return self.spin_retries.value()

    @property
    def circuit_breaker_threshold(self) -> int:
        return self.spin_cb_threshold.value()

    @property
    def wxpusher_app_token(self) -> str:
        return self.wx_app_token_input.text().strip()

    @property
    def wxpusher_uid(self) -> str:
        return self.wx_uid_input.text().strip()

    @property
    def merge_max_chars(self) -> int:
        return self.spin_merge_chars.value()

    @property
    def background_merge(self) -> bool:
        return self.chk_background_merge.isChecked()

    @property
    def server_threads_per_server(self) -> int:
        return self.spin_server_threads.value()


# ══════════════════════════════════════════════════════════════
# 信号桥接
# ══════════════════════════════════════════════════════════════
class GuiProgressAdapter(QObject):
    log_signal = pyqtSignal(str)
    total_files_signal = pyqtSignal(int)
    current_file_signal = pyqtSignal(int)
    total_tasks_signal = pyqtSignal(int)
    current_task_signal = pyqtSignal(int)
    server_down_signal = pyqtSignal(str, str)  # url, stats
    all_down_signal = pyqtSignal()

    def log(self, message: str):               self.log_signal.emit(message)

    def set_total_files(self, total: int):     self.total_files_signal.emit(total)

    def update_file_progress(self, current):   self.current_file_signal.emit(current)

    def set_current_task_range(self, total):   self.total_tasks_signal.emit(total)

    def update_task_progress(self, current):   self.current_task_signal.emit(current)

    def notify_server_down(self, url, stats):  self.server_down_signal.emit(url, stats)

    def notify_all_down(self):                 self.all_down_signal.emit()


# ══════════════════════════════════════════════════════════════
# API 连接检查线程
# ══════════════════════════════════════════════════════════════
class CheckConnectionThread(QThread):
    result_signal = pyqtSignal(bool, str, str, str)  # (success, protocol, message, url)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        ok, proto, msg = test_connection(self.url)
        self.result_signal.emit(ok, proto, msg, self.url)


# ══════════════════════════════════════════════════════════════
# 配音工作线程
# ══════════════════════════════════════════════════════════════
class WorkerThread(QThread):
    finished_signal = pyqtSignal(bool)

    def __init__(self, srt_paths, transformers_line, adapter, server_configs: dict,
                 server_protocols: dict = None,
                 max_retries: int = 5, circuit_breaker_threshold: int = 6,
                 background_merge: bool = False,
                 server_threads_per_server: int = 1):
        super().__init__()
        self.srt_paths = srt_paths
        self.transformers_line = transformers_line
        self.adapter = adapter
        self.server_configs = server_configs
        self.server_protocols = server_protocols or {}
        self.max_retries = max_retries
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.background_merge = background_merge
        self.server_threads_per_server = server_threads_per_server

    def run(self):
        config.USE_TQDM_PROGRESS_BAR = False
        config.ABORT_ALL = False
        completed = False
        try:
            completed = process_srt_files(
                srt_paths=self.srt_paths,
                transformers_line=self.transformers_line,
                progress_callback=self.adapter,
                server_configs=self.server_configs,
                server_protocols=self.server_protocols,
                on_server_down=self.adapter.notify_server_down,
                on_all_down=self.adapter.notify_all_down,
                max_retries=self.max_retries,
                circuit_breaker_threshold=self.circuit_breaker_threshold,
                background_merge=self.background_merge,
                server_threads_per_server=self.server_threads_per_server,
            )
        except Exception as e:
            self.adapter.log(f"Critical Error in Worker: {e}")
        finally:
            self.finished_signal.emit(bool(completed) and not config.ABORT_ALL)


# ══════════════════════════════════════════════════════════════
# 服务器条目 Widget
# ══════════════════════════════════════════════════════════════
class ServerEntryWidget(QWidget):
    remove_signal = pyqtSignal(QListWidgetItem)
    retry_signal = pyqtSignal(str)

    def __init__(self, url: str, parent_item: QListWidgetItem, protocol: str = "detecting", parent=None):
        super().__init__(parent)
        self.url = url
        self.parent_item = parent_item
        self._status = 'checking'
        self._protocol = protocol

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self.led = QLabel()
        self.led.setFixedSize(12, 12)
        self.led.setStyleSheet("background-color: #f1c40f; border-radius: 6px;")

        self.proto_badge = QLabel(self._format_proto_badge(protocol))
        self._apply_proto_badge_style(protocol)

        self.url_label = QLabel(url)
        self.url_label.setStyleSheet("color: #ccc; font-size: 12px;")

        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedSize(22, 22)
        self.remove_btn.setStyleSheet(
            "QPushButton { background: #555; border-radius: 3px; color: #ccc; font-size: 10px; }"
            "QPushButton:hover { background: #e74c3c; color: white; }"
        )
        self.remove_btn.clicked.connect(lambda: self.remove_signal.emit(self.parent_item))

        layout.addWidget(self.led)
        layout.addWidget(self.proto_badge)
        layout.addWidget(self.url_label, 1)
        layout.addWidget(self.remove_btn)
        self.setLayout(layout)

    @property
    def status(self) -> str:
        return self._status

    @property
    def protocol(self) -> str:
        return self._protocol

    def set_status(self, status: str):
        self._status = status
        colors = {'checking': "#f1c40f", 'online': "#2ecc71", 'offline': "#e74c3c"}
        self.led.setStyleSheet(
            f"background-color: {colors.get(status, '#666')}; border-radius: 6px;"
        )

    def set_protocol(self, protocol: str):
        self._protocol = protocol
        self.proto_badge.setText(self._format_proto_badge(protocol))
        self._apply_proto_badge_style(protocol)

    @staticmethod
    def _format_proto_badge(proto: str) -> str:
        if proto == "indextts_15":
            return "IndexTTS 1.5"
        if proto == "voxcpm2":
            return "VoxCPM 2.0"
        if proto == "detecting":
            return "Detecting..."
        return "Unknown"

    def _apply_proto_badge_style(self, proto: str):
        if proto == "indextts_15":
            self.proto_badge.setStyleSheet(
                "background: #1b3a4b; color: #48cae4; border: 1px solid #0077b6; "
                "border-radius: 3px; padding: 1px 5px; font-size: 11px; font-weight: bold;"
            )
        elif proto == "voxcpm2":
            self.proto_badge.setStyleSheet(
                "background: #2d1b4e; color: #c77dff; border: 1px solid #7b2cbf; "
                "border-radius: 3px; padding: 1px 5px; font-size: 11px; font-weight: bold;"
            )
        else:
            self.proto_badge.setStyleSheet(
                "background: #333; color: #888; border: 1px solid #555; "
                "border-radius: 3px; padding: 1px 5px; font-size: 11px;"
            )

    def mouseDoubleClickEvent(self, event):
        self.retry_signal.emit(self.url)
        super().mouseDoubleClickEvent(event)

class FolderEntryWidget(QWidget):
    remove_signal = pyqtSignal(QListWidgetItem)

    def __init__(self, path: str, parent_item: QListWidgetItem, parent=None):
        super().__init__(parent)
        self.path = path
        self.parent_item = parent_item

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self.path_label = QLabel(path)
        self.path_label.setStyleSheet("color: #ccc; font-size: 12px;")

        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedSize(22, 22)
        self.remove_btn.setStyleSheet(
            "QPushButton { background: #555; border-radius: 3px; color: #ccc; font-size: 10px; }"
            "QPushButton:hover { background: #e74c3c; color: white; }"
        )
        self.remove_btn.clicked.connect(lambda: self.remove_signal.emit(self.parent_item))

        layout.addWidget(self.path_label, 1)
        layout.addWidget(self.remove_btn)
        self.setLayout(layout)


# ══════════════════════════════════════════════════════════════
# 主窗口
# ══════════════════════════════════════════════════════════════
class TTSApp(QWidget):
    def __init__(self):
        super().__init__()
        self._server_widgets: dict = {}
        self._check_threads: list = []
        self._server_down_webhook_generation = 0
        self._all_down_webhook_sent = False
        self._processing = False  # 任务运行中：锁定 START 按钮，防止动态增删服务器把它重新点亮

        # 加载本地设置
        self._settings = load_settings()
        self.srt_paths: list = self._settings.get("srt_paths", [])

        self.initUI()
        self.apply_styles()

        # 初始化日志文件系统（在 initUI 之后，确保 adapter 已创建）
        log_path = init_logger()
        set_gui_callback(self._on_log_line)
        self.append_log(f"> 日志文件: {log_path}")

        # 启动控制服务器（延迟导入，避免 main.py 依赖问题阻塞 GUI 启动）
        try:
            import control_server
            control_server.set_process_runner(self._restart_pipeline)
            control_server.start_control_server()
        except Exception as e:
            self.append_log(f"> 控制服务器启动失败: {e}")

        # 恢复上次的服务器列表
        saved_protos = self._settings.get("server_protocols", {})
        for url in self._settings.get("servers", []):
            if url:
                proto = saved_protos.get(url, "detecting")
                self._restore_server(url, initial_proto=proto)

        # 恢复上次的文件夹列表
        for path in self._settings.get("srt_paths", []):
            if path and os.path.isdir(path):
                self._add_folder_row(path)

        # 系统托盘
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_make_icon("#3498db"))
        tray_menu = QMenu()
        tray_menu.addAction("显示窗口", self.showNormal)
        tray_menu.addAction("退出", lambda: self._force_exit("tray menu exit"))
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showNormal()

    def _restart_pipeline(self):
        """重启上次的处理流程（供 control_server 调用）。"""
        from main import restart_last_pipeline
        restart_last_pipeline()

    # ── UI 构建 ────────────────────────────────
    def initUI(self):
        self.setWindowTitle('Auto Dubbing Studio Pro')
        self.resize(1000, 900)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # 标题行（标题 + 设置按钮）
        title_row = QHBoxLayout()
        title_label = QLabel("Auto Dubbing Studio")
        title_label.setObjectName("titleLabel")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.settings_btn = QPushButton("⚙️  Settings")
        self.settings_btn.setObjectName("settingsBtn")
        self.settings_btn.setFixedSize(110, 36)
        self.settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.settings_btn.clicked.connect(self.open_settings)

        title_row.addStretch()
        title_row.addWidget(title_label)
        title_row.addStretch()
        title_row.addWidget(self.settings_btn)
        main_layout.addLayout(title_row)

        # ── Project Settings ───────────────────
        settings_group = QGroupBox("Project Settings")
        settings_layout = QVBoxLayout()
        settings_layout.setSpacing(10)

        # SRT Folders
        path_layout = QHBoxLayout()
        path_label = QLabel("SRT Folders:")
        path_label.setFixedWidth(90)

        self.folder_list = QListWidget()
        self.folder_list.setFixedHeight(110)
        self.folder_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.folder_list.setStyleSheet(
            "QListWidget { background: #1e1e1e; border: 1px solid #444; border-radius: 4px; }"
            "QListWidget::item { border-bottom: 1px solid #2a2a2a; }"
        )

        self.add_folder_btn = QPushButton('Browse')
        self.add_folder_btn.setFixedWidth(90)
        self.add_folder_btn.setMinimumHeight(32)
        self.add_folder_btn.clicked.connect(self.add_folder)

        path_layout.addWidget(path_label)
        path_layout.addWidget(self.folder_list, 1)
        path_layout.addSpacing(5)
        path_layout.addWidget(self.add_folder_btn)
        settings_layout.addLayout(path_layout)

        self._folder_widgets: dict = {}

        # 只读摘要行（点 Settings 才能改）
        summary_layout = QHBoxLayout()
        self.lbl_line_summary = QLabel(f"Line Index: {self._settings['line_index']}")
        self.lbl_retry_summary = QLabel(f"最大重试: {self._settings.get('max_retries', 5)}")
        _cb_val = self._settings.get('circuit_breaker_threshold', 6)
        self.lbl_cb_summary = QLabel(f"熔断阈值: {'已禁用' if _cb_val < 0 else _cb_val}")
        self.lbl_merge_summary = QLabel(f"合并字数: {self._settings.get('merge_max_chars', 30)}")
        self.lbl_threads_summary = QLabel(f"每服务器并发: {self._settings.get('server_threads_per_server', 1)}")
        self.lbl_bg_merge_summary = QLabel()
        self.lbl_webhook_summary = QLabel()
        self._refresh_summary_labels()

        for lbl in (self.lbl_line_summary, self.lbl_retry_summary,
                    self.lbl_cb_summary,
                    self.lbl_merge_summary, self.lbl_threads_summary,
                    self.lbl_bg_merge_summary,
                    self.lbl_webhook_summary):
            lbl.setStyleSheet("color: #aaa; font-size: 12px;")

        hint_btn = QPushButton("⚙️ 修改")
        hint_btn.setFixedSize(70, 26)
        hint_btn.setStyleSheet(
            "QPushButton { background: #3c3c3c; border:1px solid #555; border-radius:4px; "
            "color:#aaa; font-size:11px; } QPushButton:hover { color:#fff; }"
        )
        hint_btn.clicked.connect(self.open_settings)

        summary_layout.addWidget(self.lbl_line_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_retry_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_cb_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_merge_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_threads_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_bg_merge_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_webhook_summary)
        summary_layout.addStretch()
        summary_layout.addWidget(hint_btn)
        settings_layout.addLayout(summary_layout)

        settings_group.setLayout(settings_layout)
        main_layout.addWidget(settings_group)

        # ── API Servers ────────────────────────
        server_group = QGroupBox("API Servers  (支持多台，动态分发；运行中可增删节点)")
        server_layout = QVBoxLayout()
        server_layout.setSpacing(8)

        add_row = QHBoxLayout()
        self.new_api_input = QLineEdit()
        self.new_api_input.setPlaceholderText(
            "输入 TTS 服务地址 (支持 IndexTTS 1.5 / VoxCPM 2.0，可用逗号或 ~N 分隔)"
        )
        self.new_api_input.setMinimumHeight(32)
        self.new_api_input.returnPressed.connect(self.add_server)

        self.add_btn = QPushButton("Add & Connect")
        self.add_btn.setFixedWidth(120)
        self.add_btn.setMinimumHeight(32)
        self.add_btn.setObjectName("checkBtn")
        self.add_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.add_btn.clicked.connect(self.add_server)

        add_row.addWidget(QLabel("Add URL:"))
        add_row.addWidget(self.new_api_input, 1)
        add_row.addSpacing(5)
        add_row.addWidget(self.add_btn)
        server_layout.addLayout(add_row)

        self.server_list = QListWidget()
        self.server_list.setFixedHeight(110)
        self.server_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.server_list.setStyleSheet(
            "QListWidget { background: #1e1e1e; border: 1px solid #444; border-radius: 4px; }"
            "QListWidget::item { border-bottom: 1px solid #2a2a2a; }"
        )
        server_layout.addWidget(self.server_list)
        server_group.setLayout(server_layout)
        main_layout.addWidget(server_group)

        # ── Progress ───────────────────────────
        progress_group = QGroupBox("Processing Status")
        progress_layout = QVBoxLayout()

        self.lbl_total_progress = QLabel("Total Files Progress: 0/0")
        self.bar_total_progress = QProgressBar()
        self.bar_total_progress.setStyleSheet("QProgressBar::chunk { background-color: #2ecc71; }")
        progress_layout.addWidget(self.lbl_total_progress)
        progress_layout.addWidget(self.bar_total_progress)

        self.lbl_task_progress = QLabel("Current File Tasks: Waiting...")
        self.bar_task_progress = QProgressBar()
        self.bar_task_progress.setStyleSheet("QProgressBar::chunk { background-color: #3498db; }")
        progress_layout.addWidget(self.lbl_task_progress)
        progress_layout.addWidget(self.bar_task_progress)

        progress_group.setLayout(progress_layout)
        main_layout.addWidget(progress_group)

        # ── Run Button ─────────────────────────
        self.run_btn = QPushButton('START DUBBING')
        self.run_btn.setObjectName("runBtn")
        self.run_btn.setMinimumHeight(60)
        self.run_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.run_btn.clicked.connect(self.start_processing)
        self.run_btn.setEnabled(False)
        self.run_btn.setToolTip("Please connect to at least one API server first.")
        main_layout.addWidget(self.run_btn)

        # ── Logs ───────────────────────────────
        log_group = QGroupBox("Execution Logs")
        log_layout = QVBoxLayout()
        self.log_output = QTextEdit()
        self.log_output.setObjectName("logOutput")
        self.log_output.setReadOnly(True)
        # 长跑时限制保留行数，避免 QTextEdit 无限增长拖慢 UI。
        self.log_output.document().setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_output)
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group)

        self.setLayout(main_layout)

        # 信号适配器
        self.adapter = GuiProgressAdapter()
        self.adapter.log_signal.connect(self.append_log)
        self.adapter.total_files_signal.connect(self.init_total_bar)
        self.adapter.current_file_signal.connect(self.update_total_bar)
        self.adapter.total_tasks_signal.connect(self.init_task_bar)
        self.adapter.current_task_signal.connect(self.update_task_bar)
        self.adapter.server_down_signal.connect(self._on_server_down)
        self.adapter.all_down_signal.connect(self._on_all_servers_down)

    # ── 设置对话框 ──────────────────────────────
    def open_settings(self):
        dlg = SettingsDialog(
            parent=self,
            line_index=self._settings["line_index"],
            max_retries=self._settings.get("max_retries", 5),
            circuit_breaker_threshold=self._settings.get("circuit_breaker_threshold", 6),
            wxpusher_app_token=self._settings.get("wxpusher_app_token", ""),
            wxpusher_uid=self._settings.get("wxpusher_uid", ""),
            merge_max_chars=self._settings.get("merge_max_chars", 30),
            background_merge=self._settings.get("background_merge", False),
            server_threads_per_server=self._settings.get("server_threads_per_server", 1),
        )
        # 应用暗色样式到对话框
        dlg.setStyleSheet(self.styleSheet())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._settings["line_index"] = dlg.line_index
            self._settings["max_retries"] = dlg.max_retries
            self._settings["circuit_breaker_threshold"] = dlg.circuit_breaker_threshold
            self._settings["wxpusher_app_token"] = dlg.wxpusher_app_token
            self._settings["wxpusher_uid"] = dlg.wxpusher_uid

            self._settings["merge_max_chars"] = dlg.merge_max_chars
            self._settings["background_merge"] = dlg.background_merge
            self._settings["server_threads_per_server"] = dlg.server_threads_per_server

            # 同步到 config 模块
            config.MERGE_MAX_CHARS = dlg.merge_max_chars
            config.CIRCUIT_BREAKER_THRESHOLD = dlg.circuit_breaker_threshold

            self._save_current_settings()
            self._refresh_summary_labels()
            self._update_thread_hints()
            self.append_log(
                f"> Settings saved — Line Index: {dlg.line_index}, "
                f"每服务器并发数: {dlg.server_threads_per_server}, "
                f"最大重试: {dlg.max_retries}, "
                f"熔断阈值: {dlg.circuit_breaker_threshold}, "
                f"合并字数上限: {dlg.merge_max_chars}, "
                f"后台合并: {'开' if dlg.background_merge else '关'}, "
                f"WxPusher: {'已配置' if dlg.wxpusher_app_token and dlg.wxpusher_uid else '未配置'}"
            )

    def _refresh_summary_labels(self):
        self.lbl_line_summary.setText(f"Line Index: {self._settings['line_index']}")
        self.lbl_retry_summary.setText(f"最大重试: {self._settings.get('max_retries', 5)}")
        _cb_val = self._settings.get('circuit_breaker_threshold', 6)
        self.lbl_cb_summary.setText(f"熔断阈值: {'已禁用' if _cb_val < 0 else _cb_val}")
        self.lbl_merge_summary.setText(f"合并字数: {self._settings.get('merge_max_chars', 30)}")
        self.lbl_threads_summary.setText(f"每服务器并发: {self._settings.get('server_threads_per_server', 1)}")
        bg = self._settings.get("background_merge", False)
        self.lbl_bg_merge_summary.setText(f"后台合并: {'开' if bg else '关'}")
        wx_token = self._settings.get("wxpusher_app_token", "")
        wx_uid = self._settings.get("wxpusher_uid", "")
        self.lbl_webhook_summary.setText(f"WxPusher: {'已配置' if wx_token and wx_uid else '未配置'}")

    # ── 持久化 ──────────────────────────────────
    def _save_current_settings(self):
        self._settings["srt_paths"] = list(self._folder_widgets.keys())
        self._settings["servers"] = list(self._server_widgets.keys())
        self._settings["server_protocols"] = {
            url: entry.protocol for url, (_, entry) in self._server_widgets.items()
            if entry.protocol not in ("detecting", "unknown")
        }
        save_settings(self._settings)

    # ── 服务器管理 ────────────────────────────────
    def _restore_server(self, url: str, initial_proto: str = "detecting"):
        """静默恢复服务器（不弹出连接日志，直接检测）"""
        if url in self._server_widgets:
            return
        self._add_server_row(url, protocol=initial_proto)
        _, entry = self._server_widgets[url]
        entry.set_status('checking')
        self._start_check_thread(url)

    def _start_check_thread(self, url: str):
        """启动一次连接探测，并在结束后从列表中移除线程对象。"""
        thread = CheckConnectionThread(url)
        thread.result_signal.connect(self._on_server_check_done)
        thread.finished.connect(lambda t=thread: self._discard_check_thread(t))
        self._check_threads.append(thread)
        thread.start()
        return thread

    def _discard_check_thread(self, thread):
        try:
            self._check_threads.remove(thread)
        except ValueError:
            pass

    def _add_server_row(self, url: str, protocol: str = "detecting"):
        if url in self._server_widgets:
            return
        item = QListWidgetItem(self.server_list)
        entry = ServerEntryWidget(url, item, protocol=protocol, parent=self)
        entry.remove_signal.connect(self._remove_server_item)
        entry.retry_signal.connect(self._retry_server_connection)
        item.setSizeHint(entry.sizeHint())
        self.server_list.addItem(item)
        self.server_list.setItemWidget(item, entry)
        self._server_widgets[url] = (item, entry)

    def _update_thread_hints(self):
        pass  # 并发数现在由设置窗口统一管理

    def _get_server_configs(self) -> dict:
        threads = self._settings.get("server_threads_per_server", 1)
        # 只把已经完成软探测且状态为 online 的节点交给主流程。
        # checking 状态不再提前进入任务，避免连接检查尚未结束时重复建连。
        online = [url for url, (_, entry) in self._server_widgets.items()
                  if entry.status == 'online']
        return {url: threads for url in online}

    def _get_server_protocols(self) -> dict:
        return {
            url: entry.protocol for url, (_, entry) in self._server_widgets.items()
            if entry.status == 'online'
        }

    def _update_run_btn(self):
        if self._processing:
            # 任务运行中：保持 PROCESSING... 禁用态。
            # 添加/移除服务器、文件夹都会调用本方法，不能因此把按钮重新点亮。
            self.run_btn.setEnabled(False)
            self.run_btn.setToolTip("Task is running. Please wait for it to finish.")
            return
        if self._get_server_configs() and self._folder_widgets:
            self.run_btn.setEnabled(True)
            self.run_btn.setToolTip("")
        else:
            self.run_btn.setEnabled(False)
            if not self._folder_widgets:
                self.run_btn.setToolTip("Please add at least one SRT folder.")
            else:
                self.run_btn.setToolTip("Please connect to at least one API server first.")

    def _retry_server_connection(self, url: str):
        if url not in self._server_widgets:
            return
        _, entry = self._server_widgets[url]
        entry.set_status('checking')
        self.append_log(f"> 重新连接: {url} ...")
        self._start_check_thread(url)

    def _parse_server_urls(self, raw: str) -> list[str]:
        """
        解析输入，支持逗号分隔多链接 + 每段独立的 ~N 批量语法：
          http://a:7860/, http://b:7860/       →  2个
          http://a:7860~2/, http://b:7860~1/   →  5个 (3+2)
        """
        parts = [p.strip() for p in raw.split(',') if p.strip()]
        all_urls = []
        for part in parts:
            all_urls.extend(self._parse_single_url(part))
        return all_urls

    def _parse_single_url(self, raw: str) -> list[str]:
        """
        解析单个 URL，支持批量语法 ~N：
          http://127.0.0.1:7860~2/  →  7860, 7861, 7862 共3个
          http://127.0.0.1:7860/    →  仅 7860
        ~N 中的 N 表示额外再添加几个（总数 = N+1）。
        """
        import re
        m = re.search(r'~(\d+)', raw)
        if not m:
            url = raw if raw.endswith('/') else raw + '/'
            return [url]

        extra = int(m.group(1))
        base = raw[:m.start()] + raw[m.end():]
        if not base.endswith('/'):
            base += '/'

        port_m = re.search(r':(\d+)/', base)
        if not port_m:
            self.append_log("⚠️ 无法解析端口号，请确保 URL 格式为 http://host:port/")
            return [base]

        base_port = int(port_m.group(1))
        prefix = base[:port_m.start(1)]
        suffix = base[port_m.end(1):]

        urls = []
        for i in range(extra + 1):
            urls.append(f"{prefix}{base_port + i}{suffix}")
        return urls

    def add_server(self):
        raw = self.new_api_input.text().strip()
        if not raw:
            self.append_log("Please enter a server URL.")
            return

        urls = self._parse_server_urls(raw)
        self.new_api_input.clear()

        added = 0
        for url in urls:
            if url in self._server_widgets:
                self.append_log(f"> 已存在，跳过: {url}")
                continue
            self._add_server_row(url)
            self.append_log(f"> Connecting to {url} ...")
            _, entry = self._server_widgets[url]
            entry.set_status('checking')
            self._start_check_thread(url)
            added += 1

        if added > 1:
            self.append_log(f"> 共批量添加 {added} 台服务器")

        self._save_current_settings()

    def _on_server_check_done(self, success, protocol, message, url):
        if url not in self._server_widgets:
            return
        _, entry = self._server_widgets[url]
        entry.set_protocol(protocol)
        if success:
            entry.set_status('online')
            proto_name = "IndexTTS 1.5" if protocol == "indextts_15" else ("VoxCPM 2.0" if protocol == "voxcpm2" else protocol)
            self.append_log(f"> Connected: {url} [{proto_name}]")
            # 流程运行中：直接加入活跃服务器池，无需重启。
            try:
                if runtime_add_server(url, protocol,
                                     self._settings.get("server_threads_per_server", 1)):
                    self.append_log(f"> 已动态加入运行中的服务器池: {url}")
            except Exception as e:
                self.append_log(f"> 动态加入服务器池失败: {e}")
        else:
            entry.set_status('offline')
            self.append_log(f"> Failed: {url} — {message}")
        self._update_thread_hints()
        self._update_run_btn()
        self._save_current_settings()

    def _remove_server_item(self, item: QListWidgetItem):
        widget = self.server_list.itemWidget(item)
        url = widget.url if widget else None
        row = self.server_list.row(item)
        self.server_list.takeItem(row)
        if url and url in self._server_widgets:
            del self._server_widgets[url]
        if url:
            try:
                if runtime_remove_server(url):
                    self.append_log(f"> 已从运行中的服务器池摘除: {url}（在途任务完成后退出）")
            except Exception as e:
                self.append_log(f"> 动态摘除服务器失败: {e}")
        self._update_thread_hints()
        self._update_run_btn()
        self._save_current_settings()

    def _add_folder_row(self, path: str):
        if path in self._folder_widgets:
            return
        item = QListWidgetItem(self.folder_list)
        entry = FolderEntryWidget(path, item, self)
        entry.remove_signal.connect(self._remove_folder_item)
        item.setSizeHint(entry.sizeHint())
        self.folder_list.addItem(item)
        self.folder_list.setItemWidget(item, entry)
        self._folder_widgets[path] = (item, entry)

    def _remove_folder_item(self, item: QListWidgetItem):
        widget = self.folder_list.itemWidget(item)
        path = widget.path if widget else None
        row = self.folder_list.row(item)
        self.folder_list.takeItem(row)
        if path and path in self._folder_widgets:
            del self._folder_widgets[path]
        self._save_current_settings()
        self._update_run_btn()

    # ── 熔断事件 ───────────────────────────────
    def _flush_server_down_webhook(self, generation: int, msg: str):
        """短暂延迟单节点通知；若随后全部熔断，则由总通知取代。"""
        if generation != self._server_down_webhook_generation or self._all_down_webhook_sent:
            return
        send_webhook(
            self._settings.get("wxpusher_app_token", ""),
            self._settings.get("wxpusher_uid", ""),
            "⚠️ 配音服务器熔断", msg
        )

    def _on_server_down(self, url: str, stats: str):
        """某台服务器触发熔断时调用"""
        if url in self._server_widgets:
            self._server_widgets[url][1].set_status('offline')
        msg = f"⚠️ 服务器熔断: {url}\n统计: {stats}"
        self.append_log(f"\n>>> {msg}")
        _play_sound("abort")
        self._tray.showMessage(
            "Auto Dubbing Studio — 服务器熔断",
            msg,
            QSystemTrayIcon.MessageIcon.Warning,
            6000
        )
        self._server_down_webhook_generation += 1
        generation = self._server_down_webhook_generation
        QTimer.singleShot(800, lambda: self._flush_server_down_webhook(generation, msg))

    def _on_all_servers_down(self):
        """所有服务器均熔断时调用"""
        self._server_down_webhook_generation += 1  # 取消尚未发送的单节点通知
        self._all_down_webhook_sent = True
        msg = "🔴 所有服务器均已熔断，任务已停止！请检查 API 服务状态。"
        self.append_log(f"\n>>> {msg}")
        _play_sound("abort")
        self._tray.setIcon(_make_icon("#e74c3c"))
        self._tray.showMessage(
            "Auto Dubbing Studio — 全部熔断",
            msg,
            QSystemTrayIcon.MessageIcon.Critical,
            8000
        )
        send_webhook(
            self._settings.get("wxpusher_app_token", ""),
            self._settings.get("wxpusher_uid", ""),
            "🔴 配音全部服务器熔断", msg
        )
        QTimer.singleShot(8000, lambda: self._tray.setIcon(_make_icon("#3498db")))

    # ── 其他控件 ───────────────────────────────
    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Directory",
            list(self._folder_widgets.keys())[-1] if self._folder_widgets else os.path.expanduser("~")
        )
        if not folder:
            return
        folder = os.path.normpath(folder)
        if folder in self._folder_widgets:
            self.append_log(f"> 文件夹已存在: {folder}")
            return
        self._add_folder_row(folder)
        self._save_current_settings()
        self._update_run_btn()

    def start_processing(self):
        if self._processing:
            # 防御性重入保护：任务运行中不允许再起一条流水线
            self.append_log("> 任务已在运行中，忽略重复启动。")
            return
        line_idx = self._settings.get("line_index", 2)
        config.MERGE_MAX_CHARS = self._settings.get("merge_max_chars", 30)
        server_configs = self._get_server_configs()
        server_protocols = self._get_server_protocols()
        if not server_configs:
            msg = (
                "未配置可用的 TTS 服务地址。\n\n"
                "请先在 API Servers 中点击 Add & Connect 添加 IndexTTS 1.5 或 VoxCPM 2.0 服务地址，"
                "连接成功后再开始配音。"
            )
            self.append_log("> " + msg.replace("\n", " "))
            QMessageBox.warning(self, "缺少 TTS 服务地址", msg)
            return

        srt_paths = list(self._folder_widgets.keys())
        if not srt_paths:
            self.append_log("Error: No folders added.")
            return

        self._server_down_webhook_generation += 1
        self._all_down_webhook_sent = False

        self._processing = True
        self.run_btn.setEnabled(False)
        self.run_btn.setText("PROCESSING...")
        self.add_folder_btn.setEnabled(False)
        # Add & Connect 在运行中保持可用：探测成功后会把节点热加入当前池。
        self.settings_btn.setEnabled(False)
        self.log_output.clear()
        self.bar_total_progress.setValue(0)
        self.bar_task_progress.setValue(0)
        self.lbl_task_progress.setText("Current File Tasks: Initializing...")

        self.append_log(f"> 共 {len(srt_paths)} 个文件夹，使用 {len(server_configs)} 台服务器")
        for p in srt_paths:
            self.append_log(f"  · {p}")
        for u, n in server_configs.items():
            proto = server_protocols.get(u, 'voxcpm2')
            proto_label = "IndexTTS 1.5" if proto == "indextts_15" else ("VoxCPM 2.0" if proto == "voxcpm2" else proto)
            self.append_log(f"  · {u} [{proto_label}]  线程数: {n}")

        self.worker = WorkerThread(
            srt_paths=srt_paths,
            transformers_line=line_idx,
            adapter=self.adapter,
            server_configs=server_configs,
            server_protocols=server_protocols,
            max_retries=self._settings.get("max_retries", 5),
            circuit_breaker_threshold=self._settings.get("circuit_breaker_threshold", 6),
            background_merge=self._settings.get("background_merge", False),
            server_threads_per_server=self._settings.get("server_threads_per_server", 1),
        )
        self.worker.finished_signal.connect(self.process_finished)
        self.worker.start()

    def append_log(self, text):
        self.log_output.append(text)

    def _on_log_line(self, line: str):
        """日志回调（来自任意线程），通过 Qt 信号安全地投递到 GUI 线程。"""
        self.adapter.log_signal.emit(line)

    def init_total_bar(self, total):
        self.bar_total_progress.setMaximum(total)
        self.bar_total_progress.setValue(0)
        self.lbl_total_progress.setText(f"Total Files Progress: 0/{total}")

    def update_total_bar(self, current):
        self.bar_total_progress.setValue(current)
        total = self.bar_total_progress.maximum()
        self.lbl_total_progress.setText(f"Total Files Progress: {current}/{total}")

    def init_task_bar(self, total):
        self.bar_task_progress.setMaximum(total)
        self.bar_task_progress.setValue(0)
        self.lbl_task_progress.setText(f"Current File Tasks: 0/{total}")

    def update_task_bar(self, current):
        self.bar_task_progress.setValue(current)
        total = self.bar_task_progress.maximum()
        self.lbl_task_progress.setText(f"Current File Tasks: {current}/{total}")

    def process_finished(self, completed: bool):
        self._processing = False
        self.run_btn.setText("START DUBBING")
        self._update_run_btn()
        self.add_folder_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.settings_btn.setEnabled(True)
        self.lbl_task_progress.setText("Status: Idle")

        if completed:
            msg = "✅ 所有配音任务已完成！"
            detail = "全部文件处理完毕，可以查看输出目录。"
            icon = QSystemTrayIcon.MessageIcon.Information
            tray_icon = _make_icon("#2ecc71")
            log_msg = "\n>>> ✅ All tasks completed."
            sound = "finish"
        elif config.ABORT_ALL:
            msg = "⚠️ 任务已中止"
            detail = "配音流程被停止，已完成部分已保存。"
            icon = QSystemTrayIcon.MessageIcon.Warning
            tray_icon = _make_icon("#e67e22")
            log_msg = "\n>>> ⚠️ Workflow aborted."
            sound = "abort"
        else:
            msg = "❌ 任务未完全完成"
            detail = "至少一个文件处理失败；本地缓存已保留，请查看日志中的失败原因。"
            icon = QSystemTrayIcon.MessageIcon.Warning
            tray_icon = _make_icon("#e67e22")
            log_msg = "\n>>> ❌ Workflow finished with errors."
            sound = "abort"

        self.append_log(log_msg)
        _play_sound(sound)
        self._tray.setIcon(tray_icon)
        self._tray.showMessage("Auto Dubbing Studio", f"{msg}\n{detail}", icon, 6000)

        # WxPusher 完成通知
        srt_paths = list(self._folder_widgets.keys())
        path_str = ", ".join(srt_paths) if srt_paths else "无"
        if not self._all_down_webhook_sent:
            send_webhook(
                self._settings.get("wxpusher_app_token", ""),
                self._settings.get("wxpusher_uid", ""),
                msg, f"{detail}\n路径: {path_str}"
            )
        try:
            self._tray.messageClicked.disconnect()
        except TypeError:
            pass
        self._tray.messageClicked.connect(self._bring_to_front)
        QTimer.singleShot(5000, lambda: self._tray.setIcon(_make_icon("#3498db")))

    def apply_styles(self):
        QApplication.setStyle(QStyleFactory.create('Fusion'))
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
        dark_palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
        QApplication.setPalette(dark_palette)

        self.setStyleSheet("""
            QWidget { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 13px; }
            QGroupBox {
                border: 1px solid #444; border-radius: 6px; margin-top: 12px;
                padding-top: 15px; font-weight: bold; color: #ddd; background-color: #2b2b2b;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLabel#titleLabel {
                font-size: 28px; font-weight: 800; color: #3498db;
                margin-bottom: 10px; letter-spacing: 1px;
            }
            QLineEdit {
                background-color: #333; border: 1px solid #555;
                border-radius: 4px; padding: 4px 8px; color: #fff;
            }
            QLineEdit:focus { border: 1px solid #3498db; }
            QLineEdit:disabled { background-color: #2a2a2a; color: #888; }
            QSpinBox {
                background-color: #333; border: 1px solid #555;
                border-radius: 4px; padding: 4px 8px; color: #fff;
            }
            QSpinBox:focus { border: 1px solid #3498db; }
            QPushButton {
                background-color: #3c3c3c; border: 1px solid #555;
                border-radius: 4px; color: white; font-weight: bold;
            }
            QPushButton:hover { background-color: #4c4c4c; border-color: #777; }
            QPushButton:pressed { background-color: #222; }
            QPushButton#runBtn {
                background-color: #27ae60; border: none; border-radius: 6px;
                font-size: 16px; margin-top: 5px;
            }
            QPushButton#runBtn:hover { background-color: #2ecc71; }
            QPushButton#runBtn:disabled { background-color: #444; color: #888; border: 1px solid #555; }
            QPushButton#checkBtn { background-color: #2980b9; border: none; }
            QPushButton#checkBtn:hover { background-color: #3498db; }
            QPushButton#settingsBtn {
                background-color: #3c3c3c; border: 1px solid #666;
                border-radius: 6px; font-size: 13px;
            }
            QPushButton#settingsBtn:hover { background-color: #555; border-color: #888; }
            QTextEdit#logOutput {
                background-color: #1e1e1e; color: #2ecc71;
                font-family: Consolas, 'Courier New', Monospace;
                font-size: 12px; border: 1px solid #444; border-radius: 4px; padding: 5px;
            }
            QProgressBar {
                border: 1px solid #444; border-radius: 4px; text-align: center;
                height: 22px; background-color: #222; color: white;
            }
            QDialog { background-color: #2d2d2d; }
            QDialogButtonBox QPushButton { min-width: 80px; min-height: 28px; }
        """)

    def _bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _force_exit(self, reason: str):
        try:
            self._save_current_settings()
        except Exception as e:
            try:
                file_log("GUI_EXIT", f"保存设置失败: {e}")
            except Exception:
                pass

        config.ABORT_ALL = True
        try:
            file_log("GUI_EXIT", f"强制退出 GUI: {reason}")
        except Exception:
            pass

        try:
            self._tray.hide()
        except Exception:
            pass
        os._exit(0)

    def closeEvent(self, event):
        self._force_exit("window closeEvent")


if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = TTSApp()
    ex.show()
    sys.exit(app.exec())
