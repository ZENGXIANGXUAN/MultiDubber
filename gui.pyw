import sys
import os
import json
import threading
import urllib.request
import urllib.parse
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton,
                             QTextEdit, QFileDialog, QLabel, QLineEdit, QHBoxLayout,
                             QGroupBox, QProgressBar, QStyleFactory, QListWidget,
                             QListWidgetItem, QAbstractItemView, QSystemTrayIcon, QMenu,
                             QDialog, QFormLayout, QDialogButtonBox, QSpinBox, QMessageBox)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QObject, QTimer
from PyQt6.QtGui import QPalette, QColor, QIcon, QPixmap, QPainter, QBrush, QCursor
import config
from main import process_srt_files
from api_client import test_connection



# ── 本地设置持久化 ──────────────────────────────────────────
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_settings.json")

DEFAULT_SETTINGS = {
    "srt_path": "",
    "line_index": 2,
    "total_threads": 6,
    "max_retries": 3,
    "webhook_url": "https://sctapi.ftqq.com/SCT124090TODYAymp8nuHDeqleLu8oRDAS.send",
    "servers": [],
}


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 补全缺失的键
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            return data
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Settings] 保存失败: {e}")


# ── Webhook 通知 ────────────────────────────────────────────
def send_webhook(webhook_url: str, title: str, content: str):
    """在后台线程发送 Server 酱 Webhook 通知"""
    if not webhook_url:
        return

    def _send():
        try:
            params = urllib.parse.urlencode({"title": title, "desp": content})
            url = f"{webhook_url.rstrip('?')}?{params}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                print(f"[Webhook] 发送成功: {resp.status}  {body[:80]}")
        except Exception as e:
            print(f"[Webhook] 发送失败: {e}")

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
                 total_threads: int = 6,
                 max_retries: int = 3,
                 webhook_url: str = ""):
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

        # Total Threads
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 128)
        self.spin_threads.setValue(total_threads)
        self.spin_threads.setMinimumHeight(32)
        self.spin_threads.setToolTip("所有服务器的线程总数，均分给各服务器")
        form.addRow("总线程数:", self.spin_threads)

        # Max Retries
        self.spin_retries = QSpinBox()
        self.spin_retries.setRange(1, 20)
        self.spin_retries.setValue(max_retries)
        self.spin_retries.setMinimumHeight(32)
        self.spin_retries.setToolTip(
            "单个任务失败后的最大重试次数。\n"
            "同时：某台服务器连续失败达到此次数后将自动下线。"
        )
        form.addRow("最大重试次数:", self.spin_retries)

        # Webhook URL
        self.webhook_input = QLineEdit(webhook_url)
        self.webhook_input.setMinimumHeight(32)
        self.webhook_input.setPlaceholderText("https://sctapi.ftqq.com/YOUR_KEY.send")
        self.webhook_input.setToolTip("Server 酱 Webhook，服务器熔断或任务完成时发送通知")
        form.addRow("Webhook URL:", self.webhook_input)

        layout.addLayout(form)

        # 测试 Webhook 按钮
        test_btn = QPushButton("📡 测试 Webhook")
        test_btn.setObjectName("checkBtn")
        test_btn.setMinimumHeight(32)
        test_btn.clicked.connect(self._test_webhook)
        layout.addWidget(test_btn)

        # 说明文字
        hint = QLabel(
            "Webhook 格式: <code>https://sctapi.ftqq.com/&lt;KEY&gt;.send</code><br>"
            "服务器熔断时会自动推送通知到你的微信。"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
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

    def _test_webhook(self):
        url = self.webhook_input.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先填写 Webhook URL")
            return
        send_webhook(url, "Auto Dubbing Studio 测试", "Webhook 测试消息，连接正常 ✅")
        QMessageBox.information(self, "已发送", "测试通知已在后台发送，请查看微信。")

    @property
    def line_index(self) -> int:
        return self.spin_line.value()

    @property
    def total_threads(self) -> int:
        return self.spin_threads.value()

    @property
    def max_retries(self) -> int:
        return self.spin_retries.value()

    @property
    def webhook_url(self) -> str:
        return self.webhook_input.text().strip()


# ══════════════════════════════════════════════════════════════
# 信号桥接
# ══════════════════════════════════════════════════════════════
class GuiProgressAdapter(QObject):
    log_signal          = pyqtSignal(str)
    total_files_signal  = pyqtSignal(int)
    current_file_signal = pyqtSignal(int)
    total_tasks_signal  = pyqtSignal(int)
    current_task_signal = pyqtSignal(int)
    server_down_signal  = pyqtSignal(str, str)   # url, stats
    all_down_signal     = pyqtSignal()

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
    result_signal = pyqtSignal(bool, str, str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        if test_connection(self.url):
            self.result_signal.emit(True, "Connection Successful!", self.url)
        else:
            self.result_signal.emit(False, "Connection Failed.", self.url)


# ══════════════════════════════════════════════════════════════
# 配音工作线程
# ══════════════════════════════════════════════════════════════
class WorkerThread(QThread):
    finished_signal = pyqtSignal(bool)

    def __init__(self, srt_path, transformers_line, adapter, server_configs: dict,
                 max_retries: int = 3):
        super().__init__()
        self.srt_path = srt_path
        self.transformers_line = transformers_line
        self.adapter = adapter
        self.server_configs = server_configs
        self.max_retries = max_retries

    def run(self):
        config.USE_TQDM_PROGRESS_BAR = False
        config.ABORT_ALL = False
        try:
            process_srt_files(
                srt_path=self.srt_path,
                transformers_line=self.transformers_line,
                progress_callback=self.adapter,
                server_configs=self.server_configs,
                on_server_down=self.adapter.notify_server_down,
                on_all_down=self.adapter.notify_all_down,
                max_retries=self.max_retries,
            )
        except Exception as e:
            self.adapter.log(f"Critical Error in Worker: {e}")
        finally:
            self.finished_signal.emit(not config.ABORT_ALL)


# ══════════════════════════════════════════════════════════════
# 服务器条目 Widget
# ══════════════════════════════════════════════════════════════
class ServerEntryWidget(QWidget):
    remove_signal = pyqtSignal(QListWidgetItem)
    retry_signal  = pyqtSignal(str)

    def __init__(self, url: str, parent_item: QListWidgetItem, parent=None):
        super().__init__(parent)
        self.url = url
        self.parent_item = parent_item

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self.led = QLabel()
        self.led.setFixedSize(12, 12)
        self.led.setStyleSheet("background-color: #f1c40f; border-radius: 6px;")

        self.url_label = QLabel(url)
        self.url_label.setStyleSheet("color: #ccc; font-size: 12px;")

        self.thread_label = QLabel("")
        self.thread_label.setFixedWidth(60)
        self.thread_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.thread_label.setStyleSheet("color: #888; font-size: 11px;")

        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedSize(22, 22)
        self.remove_btn.setStyleSheet(
            "QPushButton { background: #555; border-radius: 3px; color: #ccc; font-size: 10px; }"
            "QPushButton:hover { background: #e74c3c; color: white; }"
        )
        self.remove_btn.clicked.connect(lambda: self.remove_signal.emit(self.parent_item))

        layout.addWidget(self.led)
        layout.addWidget(self.url_label, 1)
        layout.addWidget(self.thread_label)
        layout.addWidget(self.remove_btn)
        self.setLayout(layout)

    def set_status(self, status: str):
        colors = {'checking': "#f1c40f", 'online': "#2ecc71", 'offline': "#e74c3c"}
        self.led.setStyleSheet(
            f"background-color: {colors.get(status, '#666')}; border-radius: 6px;"
        )

    def set_thread_hint(self, n: int):
        self.thread_label.setText(f"{n} 线程" if n > 0 else "")

    def mouseDoubleClickEvent(self, event):
        self.retry_signal.emit(self.url)
        super().mouseDoubleClickEvent(event)


# ══════════════════════════════════════════════════════════════
# 主窗口
# ══════════════════════════════════════════════════════════════
class TTSApp(QWidget):
    def __init__(self):
        super().__init__()
        self._server_widgets: dict = {}
        self._check_threads: list = []

        # 加载本地设置
        self._settings = load_settings()
        self.current_srt_path = self._settings.get("srt_path", "")

        self.initUI()
        self.apply_styles()

        # 恢复上次的服务器列表
        for url in self._settings.get("servers", []):
            if url:
                self._restore_server(url)

        # 若没有保存的服务器，用 config 默认值
        if not self._settings.get("servers") and config.GRADIO_URL:
            self.new_api_input.setText(config.GRADIO_URL)
            self.add_server()

        # 系统托盘
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_make_icon("#3498db"))
        tray_menu = QMenu()
        tray_menu.addAction("显示窗口", self.showNormal)
        tray_menu.addAction("退出", lambda: os._exit(0))
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showNormal()

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

        # SRT Folder
        path_layout = QHBoxLayout()
        path_label = QLabel("SRT Folder:")
        path_label.setFixedWidth(90)
        self.path_input = QLineEdit(self.current_srt_path)
        self.path_input.setReadOnly(True)
        self.path_input.setMinimumHeight(32)
        self.path_btn = QPushButton('Browse')
        self.path_btn.setFixedWidth(90)
        self.path_btn.setMinimumHeight(32)
        self.path_btn.clicked.connect(self.select_folder)
        path_layout.addWidget(path_label)
        path_layout.addWidget(self.path_input)
        path_layout.addSpacing(5)
        path_layout.addWidget(self.path_btn)
        settings_layout.addLayout(path_layout)

        # 只读摘要行（Line Index & 总线程数，点 Settings 才能改）
        summary_layout = QHBoxLayout()
        self.lbl_line_summary    = QLabel(f"Line Index: {self._settings['line_index']}")
        self.lbl_thread_summary  = QLabel(f"总线程数: {self._settings['total_threads']}")
        self.lbl_retry_summary   = QLabel(f"最大重试: {self._settings.get('max_retries', 3)}")
        self.lbl_webhook_summary = QLabel()
        self._refresh_summary_labels()

        for lbl in (self.lbl_line_summary, self.lbl_thread_summary,
                    self.lbl_retry_summary, self.lbl_webhook_summary):
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
        summary_layout.addWidget(self.lbl_thread_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_retry_summary)
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_webhook_summary)
        summary_layout.addStretch()
        summary_layout.addWidget(hint_btn)
        settings_layout.addLayout(summary_layout)

        settings_group.setLayout(settings_layout)
        main_layout.addWidget(settings_group)

        # ── API Servers ────────────────────────
        server_group = QGroupBox("API Servers  (支持多台，动态分发任务)")
        server_layout = QVBoxLayout()
        server_layout.setSpacing(8)

        add_row = QHBoxLayout()
        self.new_api_input = QLineEdit()
        self.new_api_input.setPlaceholderText("e.g., http://127.0.0.1:7860~2/  （~2 表示连续添加7860~7862）")
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
            total_threads=self._settings["total_threads"],
            max_retries=self._settings.get("max_retries", 3),
            webhook_url=self._settings.get("webhook_url", ""),
        )
        # 应用暗色样式到对话框
        dlg.setStyleSheet(self.styleSheet())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._settings["line_index"]    = dlg.line_index
            self._settings["total_threads"] = dlg.total_threads
            self._settings["max_retries"]   = dlg.max_retries
            self._settings["webhook_url"]   = dlg.webhook_url
            self._save_current_settings()
            self._refresh_summary_labels()
            self._update_thread_hints()
            self.append_log(
                f"> Settings saved — Line Index: {dlg.line_index}, "
                f"总线程数: {dlg.total_threads}, "
                f"最大重试: {dlg.max_retries}, "
                f"Webhook: {'已设置' if dlg.webhook_url else '未设置'}"
            )

    def _refresh_summary_labels(self):
        self.lbl_line_summary.setText(f"Line Index: {self._settings['line_index']}")
        self.lbl_thread_summary.setText(f"总线程数: {self._settings['total_threads']}")
        self.lbl_retry_summary.setText(f"最大重试: {self._settings.get('max_retries', 3)}")
        wh = self._settings.get("webhook_url", "")
        if wh:
            # 只显示域名部分，避免泄露完整 key
            short = wh.split("//")[-1].split("/")[0]
            self.lbl_webhook_summary.setText(f"Webhook: {short}/…")
        else:
            self.lbl_webhook_summary.setText("Webhook: 未设置")

    # ── 持久化 ──────────────────────────────────
    def _save_current_settings(self):
        self._settings["srt_path"] = self.current_srt_path
        self._settings["servers"]  = list(self._server_widgets.keys())
        save_settings(self._settings)

    # ── 服务器管理 ────────────────────────────────
    def _restore_server(self, url: str):
        """静默恢复服务器（不弹出连接日志，直接检测）"""
        if url in self._server_widgets:
            return
        self._add_server_row(url)
        _, entry = self._server_widgets[url]
        entry.set_status('checking')
        thread = CheckConnectionThread(url)
        thread.result_signal.connect(self._on_server_check_done)
        self._check_threads.append(thread)
        thread.start()

    def _add_server_row(self, url: str):
        if url in self._server_widgets:
            return
        item = QListWidgetItem(self.server_list)
        entry = ServerEntryWidget(url, item, self)
        entry.remove_signal.connect(self._remove_server_item)
        entry.retry_signal.connect(self._retry_server_connection)
        item.setSizeHint(entry.sizeHint())
        self.server_list.addItem(item)
        self.server_list.setItemWidget(item, entry)
        self._server_widgets[url] = (item, entry)
        self._update_thread_hints()

    def _update_thread_hints(self):
        try:
            total = max(1, self._settings["total_threads"])
        except (KeyError, TypeError):
            return
        online = [(url, entry) for url, (_, entry) in self._server_widgets.items()
                  if "#e74c3c" not in entry.led.styleSheet()]
        n = len(online)
        if n == 0:
            return
        base = total // n
        remainder = total % n
        for i, (url, entry) in enumerate(online):
            entry.set_thread_hint(max(1, base + (remainder if i == 0 else 0)))
        for url, (_, entry) in self._server_widgets.items():
            if "#e74c3c" in entry.led.styleSheet():
                entry.set_thread_hint(0)

    def _get_server_configs(self) -> dict:
        total = max(1, self._settings.get("total_threads", 6))
        online = [(url, entry) for url, (_, entry) in self._server_widgets.items()
                  if "#e74c3c" not in entry.led.styleSheet()]
        n = len(online)
        if n == 0:
            return {}
        base = total // n
        remainder = total % n
        return {
            url: max(1, base + (remainder if i == 0 else 0))
            for i, (url, entry) in enumerate(online)
        }

    def _update_run_btn(self):
        if self._get_server_configs():
            self.run_btn.setEnabled(True)
            self.run_btn.setToolTip("")
        else:
            self.run_btn.setEnabled(False)
            self.run_btn.setToolTip("Please connect to at least one API server first.")

    def _retry_server_connection(self, url: str):
        if url not in self._server_widgets:
            return
        _, entry = self._server_widgets[url]
        entry.set_status('checking')
        self.append_log(f"> 重新连接: {url} ...")
        thread = CheckConnectionThread(url)
        thread.result_signal.connect(self._on_server_check_done)
        self._check_threads.append(thread)
        thread.start()

    def _parse_server_urls(self, raw: str) -> list[str]:
        """
        解析输入，支持批量语法 ~N：
          http://127.0.0.1:7860~2/  →  7860, 7861, 7862 共3个
          http://127.0.0.1:7860/    →  仅 7860
        ~N 中的 N 表示额外再添加几个（总数 = N+1）。
        """
        import re
        # 匹配 ~数字，允许出现在端口后、路径前任意位置
        m = re.search(r'~(\d+)', raw)
        if not m:
            url = raw if raw.endswith('/') else raw + '/'
            return [url]

        extra = int(m.group(1))
        base  = raw[:m.start()] + raw[m.end():]   # 去掉 ~N 部分
        if not base.endswith('/'):
            base += '/'

        # 提取端口号
        port_m = re.search(r':(\d+)/', base)
        if not port_m:
            self.append_log("⚠️ 无法解析端口号，请确保 URL 格式为 http://host:port/")
            return [base]

        base_port = int(port_m.group(1))
        prefix    = base[:port_m.start(1)]   # "http://127.0.0.1:"
        suffix    = base[port_m.end(1):]     # "/"

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
            thread = CheckConnectionThread(url)
            thread.result_signal.connect(self._on_server_check_done)
            self._check_threads.append(thread)
            thread.start()
            added += 1

        if added > 1:
            self.append_log(f"> 共批量添加 {added} 台服务器")

        self._save_current_settings()

    def _on_server_check_done(self, success, message, url):
        if url not in self._server_widgets:
            return
        _, entry = self._server_widgets[url]
        if success:
            entry.set_status('online')
            self.append_log(f"> Connected: {url}")
        else:
            entry.set_status('offline')
            self.append_log(f"> Failed: {url} — {message}")
        self._update_thread_hints()
        self._update_run_btn()

    def _remove_server_item(self, item: QListWidgetItem):
        widget = self.server_list.itemWidget(item)
        url = widget.url if widget else None
        row = self.server_list.row(item)
        self.server_list.takeItem(row)
        if url and url in self._server_widgets:
            del self._server_widgets[url]
        self._update_thread_hints()
        self._update_run_btn()
        self._save_current_settings()

    # ── 熔断事件 ───────────────────────────────
    def _on_server_down(self, url: str, stats: str):
        """某台服务器触发熔断时调用"""
        msg = f"⚠️ 服务器熔断: {url}\n统计: {stats}"
        self.append_log(f"\n>>> {msg}")
        _play_sound("abort")
        self._tray.showMessage(
            "Auto Dubbing Studio — 服务器熔断",
            msg,
            QSystemTrayIcon.MessageIcon.Warning,
            6000
        )
        # Webhook 通知
        webhook = self._settings.get("webhook_url", "")
        send_webhook(webhook, "⚠️ 配音服务器熔断", msg)

    def _on_all_servers_down(self):
        """所有服务器均熔断时调用"""
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
        webhook = self._settings.get("webhook_url", "")
        send_webhook(webhook, "🔴 配音全部服务器熔断", msg)
        QTimer.singleShot(8000, lambda: self._tray.setIcon(_make_icon("#3498db")))

    # ── 其他控件 ───────────────────────────────
    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Directory", self.current_srt_path or "/"
        )
        if folder:
            self.current_srt_path = folder
            self.path_input.setText(folder)
            self._save_current_settings()

    def start_processing(self):
        line_idx = self._settings.get("line_index", 2)
        server_configs = self._get_server_configs()
        if not server_configs:
            self.append_log("Error: No online servers available.")
            return

        self.run_btn.setEnabled(False)
        self.run_btn.setText("PROCESSING...")
        self.path_btn.setEnabled(False)
        self.add_btn.setEnabled(False)
        self.settings_btn.setEnabled(False)
        self.log_output.clear()
        self.bar_total_progress.setValue(0)
        self.bar_task_progress.setValue(0)
        self.lbl_task_progress.setText("Current File Tasks: Initializing...")

        self.append_log(f"> 使用 {len(server_configs)} 台服务器（单队列竞争）")
        for u, n in server_configs.items():
            self.append_log(f"  · {u}  线程数: {n}")

        self.worker = WorkerThread(
            srt_path=self.current_srt_path,
            transformers_line=line_idx,
            adapter=self.adapter,
            server_configs=server_configs,
            max_retries=self._settings.get("max_retries", 3),
        )
        self.worker.finished_signal.connect(self.process_finished)
        self.worker.start()

    def append_log(self, text):
        cursor = self.log_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_output.setTextCursor(cursor)
        self.log_output.insertPlainText(text + "\n")
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_output.setTextCursor(cursor)

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
        self.run_btn.setEnabled(True)
        self.run_btn.setText("START DUBBING")
        self.path_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.settings_btn.setEnabled(True)
        self.lbl_task_progress.setText("Status: Idle")

        if completed:
            msg    = "✅ 所有配音任务已完成！"
            detail = "全部文件处理完毕，可以查看输出目录。"
            icon   = QSystemTrayIcon.MessageIcon.Information
            tray_icon = _make_icon("#2ecc71")
            log_msg   = "\n>>> ✅ All tasks completed."
            sound     = "finish"
        else:
            msg    = "⚠️ 任务已中止"
            detail = "配音流程被停止，已完成部分已保存。"
            icon   = QSystemTrayIcon.MessageIcon.Warning
            tray_icon = _make_icon("#e67e22")
            log_msg   = "\n>>> ⚠️ Workflow aborted."
            sound     = "abort"

        self.append_log(log_msg)
        _play_sound(sound)
        self._tray.setIcon(tray_icon)
        self._tray.showMessage("Auto Dubbing Studio", f"{msg}\n{detail}", icon, 6000)

        # Webhook 完成通知
        webhook = self._settings.get("webhook_url", "")
        send_webhook(webhook, msg, f"{detail}\n路径: {self.current_srt_path}")
        try:
            self._tray.messageClicked.disconnect()
        except Exception:
            pass
        self._tray.messageClicked.connect(self._bring_to_front)
        QTimer.singleShot(5000, lambda: self._tray.setIcon(_make_icon("#3498db")))

    def apply_styles(self):
        QApplication.setStyle(QStyleFactory.create('Fusion'))
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.ColorRole.Window,          QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.WindowText,      Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Base,            QColor(30, 30, 30))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase,     Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.ToolTipText,     Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Text,            Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Button,          QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ButtonText,      Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.BrightText,      Qt.GlobalColor.red)
        dark_palette.setColor(QPalette.ColorRole.Link,            QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.Highlight,       QColor(42, 130, 218))
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

    def closeEvent(self, event):
        self._save_current_settings()
        config.ABORT_ALL = True
        self._tray.hide()
        os._exit(0)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = TTSApp()
    ex.show()
    sys.exit(app.exec())
