# Background Merge Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a boolean setting `background_merge` to the Settings dialog that controls whether audio/video merge runs in a background thread (async) or inline in the main processing loop (sync). Default: off (synchronous).

**Architecture:** A `QCheckBox` in `SettingsDialog` stores the preference in `app_settings.json`. The value flows through `WorkerThread` → `process_srt_files`, where it conditionally submits the finalize job to the background executor or calls `_finalize_audio_video_job` directly.

**Tech Stack:** Python, PyQt6, existing project structure (gui.pyw, main.py)

## Global Constraints

- Default value: `background_merge: False` (synchronous by default)
- Setting persisted in `app_settings.json` alongside existing settings
- No new files created — all changes in existing `gui.pyw` and `main.py`

---

### Task 1: Add `QCheckBox` import to gui.pyw

**Files:**
- Modify: `gui.pyw:7-11` (PyQt6 imports)

**Interfaces:**
- Produces: `QCheckBox` available for use in `SettingsDialog`

- [ ] **Step 1: Add QCheckBox to the import line**

In `gui.pyw:7-11`, add `QCheckBox` to the PyQt6.QtWidgets import group:

```python
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton,
                             QTextEdit, QFileDialog, QLabel, QLineEdit, QHBoxLayout,
                             QGroupBox, QProgressBar, QStyleFactory, QListWidget,
                             QListWidgetItem, QAbstractItemView, QSystemTrayIcon, QMenu,
                             QDialog, QFormLayout, QDialogButtonBox, QSpinBox, QMessageBox,
                             QCheckBox)
```

- [ ] **Step 2: Commit**

```bash
git add gui.pyw
git commit -m "feat: add QCheckBox import for background merge toggle"
```

---

### Task 2: Add `background_merge` to DEFAULT_SETTINGS

**Files:**
- Modify: `gui.pyw:27-38` (DEFAULT_SETTINGS dict)

**Interfaces:**
- Produces: `DEFAULT_SETTINGS["background_merge"]` key available for `load_settings`

- [ ] **Step 1: Add the new key**

In `gui.pyw:27-38`, add `"background_merge": False` to `DEFAULT_SETTINGS`:

```python
DEFAULT_SETTINGS = {
    "srt_paths": [],
    "line_index": 2,
    "max_retries": 5,
    "circuit_breaker_threshold": 6,
    "webhook_url": "",
    "servers": [],
    "api_public_key": "",
    "api_private_key": "",
    "api_hostid": "",
    "merge_max_chars": 30,
    "background_merge": False,  # 后台合并音视频，默认关闭
}
```

- [ ] **Step 2: Commit**

```bash
git add gui.pyw
git commit -m "feat: add background_merge to DEFAULT_SETTINGS"
```

---

### Task 3: Add checkbox to SettingsDialog

**Files:**
- Modify: `gui.pyw:116-271` (SettingsDialog class)

**Interfaces:**
- Consumes: `background_merge: bool` constructor parameter
- Produces: `self.chk_bg_merge` QCheckBox widget
- Produces: `self.background_merge` property returning `self.chk_bg_merge.isChecked()`

- [ ] **Step 1: Add constructor parameter**

In `gui.pyw:116-125`, add `background_merge: bool = False` to `SettingsDialog.__init__`:

```python
class SettingsDialog(QDialog):
    def __init__(self, parent=None,
                 line_index: int = 2,
                 max_retries: int = 5,
                 circuit_breaker_threshold: int = 6,
                 webhook_url: str = "",
                 api_public_key: str = "",
                 api_private_key: str = "",
                 api_hostid: str = "",
                 merge_max_chars: int = 30,
                 background_merge: bool = False):
```

- [ ] **Step 2: Add QCheckBox to form layout**

In `gui.pyw:147-157`, after the `spin_merge_chars` form row, add the checkbox:

```python
        # Background Merge Toggle
        self.chk_bg_merge = QCheckBox("后台合并音视频")
        self.chk_bg_merge.setChecked(background_merge)
        self.chk_bg_merge.setMinimumHeight(32)
        self.chk_bg_merge.setToolTip(
            "开启后，配音完成的文件将在后台线程合并音频和视频，\n"
            "主流程不等待直接处理下一个文件。\n"
            "关闭时，合并在主线程同步执行。"
        )
        form.addRow("", self.chk_bg_merge)
```

- [ ] **Step 3: Add property**

In `gui.pyw:269-271`, after the `merge_max_chars` property, add:

```python
    @property
    def background_merge(self) -> bool:
        return self.chk_bg_merge.isChecked()
```

- [ ] **Step 4: Commit**

```bash
git add gui.pyw
git commit -m "feat: add background_merge checkbox to SettingsDialog"
```

---

### Task 4: Wire up TTSApp settings flow

**Files:**
- Modify: `gui.pyw:550-564` (summary labels in initUI)
- Modify: `gui.pyw:676-716` (open_settings method)
- Modify: `gui.pyw:718-729` (_refresh_summary_labels method)
- Modify: `gui.pyw:968-1006` (start_processing method)

**Interfaces:**
- Consumes: `SettingsDialog.background_merge` property
- Produces: `self._settings["background_merge"]` persisted to disk
- Produces: `background_merge` passed to `WorkerThread`

- [ ] **Step 1: Add summary label in initUI**

In `gui.pyw:557`, after `self.lbl_merge_summary`, add:

```python
        self.lbl_bg_merge_summary = QLabel()
```

In `gui.pyw:561-563`, add the new label to the styling tuple:

```python
        for lbl in (self.lbl_line_summary, self.lbl_retry_summary,
                    self.lbl_cb_summary,
                    self.lbl_merge_summary, self.lbl_bg_merge_summary,
                    self.lbl_webhook_summary):
```

In `gui.pyw:574-582`, after `self.lbl_merge_summary` in the summary_layout, add:

```python
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_bg_merge_summary)
```

- [ ] **Step 2: Update open_settings**

In `gui.pyw:677-688`, pass `background_merge` to the dialog:

```python
    def open_settings(self):
        dlg = SettingsDialog(
            parent=self,
            line_index=self._settings["line_index"],
            max_retries=self._settings.get("max_retries", 5),
            circuit_breaker_threshold=self._settings.get("circuit_breaker_threshold", 6),
            webhook_url=self._settings.get("webhook_url", ""),
            api_public_key=self._settings.get("api_public_key", ""),
            api_private_key=self._settings.get("api_private_key", ""),
            api_hostid=self._settings.get("api_hostid", ""),
            merge_max_chars=self._settings.get("merge_max_chars", 30),
            background_merge=self._settings.get("background_merge", False),
        )
```

In `gui.pyw:700-701`, after `self._settings["merge_max_chars"]`, add:

```python
            self._settings["background_merge"] = dlg.background_merge
```

In `gui.pyw:710-716`, add to the log message:

```python
            self.append_log(
                f"> Settings saved — Line Index: {dlg.line_index}, "
                f"最大重试: {dlg.max_retries}, "
                f"熔断阈值: {dlg.circuit_breaker_threshold}, "
                f"合并字数上限: {dlg.merge_max_chars}, "
                f"后台合并: {'开' if dlg.background_merge else '关'}, "
                f"Webhook: {'已设置' if dlg.webhook_url else '未设置'}"
            )
```

- [ ] **Step 3: Update _refresh_summary_labels**

In `gui.pyw:718-729`, after setting `lbl_merge_summary`, add:

```python
        bg = self._settings.get("background_merge", False)
        self.lbl_bg_merge_summary.setText(f"后台合并: {'开' if bg else '关'}")
```

- [ ] **Step 4: Pass to WorkerThread in start_processing**

In `gui.pyw:997-1004`, add `background_merge` to the WorkerThread constructor:

```python
        self.worker = WorkerThread(
            srt_paths=srt_paths,
            transformers_line=line_idx,
            adapter=self.adapter,
            server_configs=server_configs,
            max_retries=self._settings.get("max_retries", 5),
            circuit_breaker_threshold=self._settings.get("circuit_breaker_threshold", 6),
            background_merge=self._settings.get("background_merge", False),
        )
```

- [ ] **Step 5: Commit**

```bash
git add gui.pyw
git commit -m "feat: wire up background_merge setting through TTSApp"
```

---

### Task 5: Update WorkerThread to pass background_merge

**Files:**
- Modify: `gui.pyw:321-352` (WorkerThread class)

**Interfaces:**
- Consumes: `background_merge: bool` constructor parameter
- Produces: `background_merge` passed to `process_srt_files`

- [ ] **Step 1: Add parameter to WorkerThread.__init__**

In `gui.pyw:324-332`, add `background_merge` parameter:

```python
class WorkerThread(QThread):
    finished_signal = pyqtSignal(bool)

    def __init__(self, srt_paths, transformers_line, adapter, server_configs: dict,
                 max_retries: int = 5, circuit_breaker_threshold: int = 6,
                 background_merge: bool = False):
        super().__init__()
        self.srt_paths = srt_paths
        self.transformers_line = transformers_line
        self.adapter = adapter
        self.server_configs = server_configs
        self.max_retries = max_retries
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.background_merge = background_merge
```

- [ ] **Step 2: Pass to process_srt_files in run()**

In `gui.pyw:339-348`, add `background_merge` to the call:

```python
            completed = process_srt_files(
                srt_paths=self.srt_paths,
                transformers_line=self.transformers_line,
                progress_callback=self.adapter,
                server_configs=self.server_configs,
                on_server_down=self.adapter.notify_server_down,
                on_all_down=self.adapter.notify_all_down,
                max_retries=self.max_retries,
                circuit_breaker_threshold=self.circuit_breaker_threshold,
                background_merge=self.background_merge,
            )
```

- [ ] **Step 3: Commit**

```bash
git add gui.pyw
git commit -m "feat: pass background_merge through WorkerThread"
```

---

### Task 6: Add conditional finalize logic in main.py

**Files:**
- Modify: `main.py:354-361` (process_srt_files signature)
- Modify: `main.py:791-806` (finalize submission block)

**Interfaces:**
- Consumes: `background_merge: bool` parameter
- Controls: whether `_submit_finalize_job` (async) or `_finalize_audio_video_job` (sync) is called

- [ ] **Step 1: Add parameter to process_srt_files**

In `main.py:354-361`, add `background_merge: bool = False`:

```python
def process_srt_files(srt_paths, transformers_line: int = TRANSFORMERS_LINE,
                      max_workers: int = 2,
                      progress_callback=None,
                      server_configs: Dict[str, int] = None,
                      on_server_down=None,
                      on_all_down=None,
                      max_retries: int = None,
                      circuit_breaker_threshold: int = None,
                      background_merge: bool = False):
```

- [ ] **Step 2: Replace unconditional submit with conditional logic**

In `main.py:791-806`, replace the finalize block:

```python
                if config.ABORT_ALL: break

                # ── 合并：根据设置决定后台异步还是主线程同步 ──
                if background_merge:
                    _submit_finalize_job(
                        subtitle_name=subtitle_name,
                        srt_file=srt_file,
                        parsed_subtitles=parsed_subtitles,
                        current_tmp_dir=current_tmp_dir,
                        output_audio_file=output_audio_file,
                        local_status_file=local_status_file,
                        output_folder_zh=output_folder_zh,
                        srt_path=srt_path,
                        main_audio_path=main_audio_path,
                        audio_extracted_in_this_run=audio_extracted_in_this_run,
                    )
                    finalize_submitted = True
                else:
                    log(f"--- [{subtitle_name}] 同步合并音频/视频... ---")
                    ok = _finalize_audio_video_job(
                        subtitle_name=subtitle_name,
                        srt_file=srt_file,
                        parsed_subtitles=parsed_subtitles,
                        current_tmp_dir=current_tmp_dir,
                        output_audio_file=output_audio_file,
                        local_status_file=local_status_file,
                        output_folder_zh=output_folder_zh,
                        srt_path=srt_path,
                        main_audio_path=main_audio_path,
                        audio_extracted_in_this_run=audio_extracted_in_this_run,
                        log=log,
                    )
                    if not ok:
                        log(f"!! [{subtitle_name}] 同步合并失败")
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: conditional background/sync finalize in process_srt_files"
```

---

### Task 7: Final verification

- [ ] **Step 1: Verify the setting persists**

Run the app, open Settings, toggle the checkbox, save, close and reopen Settings. Verify the checkbox state is preserved.

- [ ] **Step 2: Verify summary label updates**

After saving settings, verify the summary row shows "后台合并: 开" or "后台合并: 关" correctly.

- [ ] **Step 3: Verify log output on start**

Click START DUBBING. Verify the log shows "后台合并: 开/关" in the startup summary.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: background merge toggle setting complete"
```
