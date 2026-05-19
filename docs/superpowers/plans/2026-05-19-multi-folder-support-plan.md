# Multi-Folder Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace single-folder input with a folder list widget, allowing batch processing of SRT files across multiple directories.

**Architecture:** GUI gains a `QListWidget` folder list with per-item remove buttons, backed by `srt_paths` in settings. `main.py`'s `process_srt_files` accepts a list and loops over folders with a cumulative progress counter.

**Tech Stack:** PyQt6, Python 3.x

---

### Task 1: Update settings defaults and migration

**Files:**
- Modify: `gui.pyw:27-31` (DEFAULT_SETTINGS)
- Modify: `gui.pyw:40-49` (load_settings)
- Modify: `gui.pyw:52-57` (save_settings, read to verify)

- [ ] **Step 1: Change default settings and add migration in load_settings**

In `gui.pyw`, change `DEFAULT_SETTINGS` — replace `"srt_path": ""` with `"srt_paths": []`:

```python
DEFAULT_SETTINGS = {
    "srt_paths": [],       # changed from "srt_path": ""
    "line_index": 2,
    "total_threads": 6,
    "max_retries": 3,
    "webhook_url": "https://sctapi.ftqq.com/SCT124090TODYAymp8nuHDeqleLu8oRDAS.send",
    "servers": [],
    "api_public_key": "",
    "api_private_key": "",
    "api_hostid": ""
}
```

In `load_settings`, add backward-compat migration after the `setdefault` loop:

```python
def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            # migrate old single-path setting
            if not data.get("srt_paths") and data.get("srt_path"):
                data["srt_paths"] = [data["srt_path"]]
            return data
    except Exception:
        return dict(DEFAULT_SETTINGS)
```

- [ ] **Step 2: Update `__init__` to use list**

In `TTSApp.__init__`, change `self.current_srt_path` to `self.srt_paths`:

```python
# old: self.current_srt_path = self._settings.get("srt_path", "")
self.srt_paths: list = self._settings.get("srt_paths", [])
```

- [ ] **Step 3: Commit**

```bash
git add gui.pyw
git commit -m "feat: migrate settings from srt_path to srt_paths with backward compat"
```

---

### Task 2: Replace path input with folder list widget in GUI

**Files:**
- Modify: `gui.pyw`

- [ ] **Step 1: Add FolderEntryWidget class**

Add this class before `TTSApp` (after `ServerEntryWidget`, around line 384):

```python
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
```

- [ ] **Step 2: Replace the SRT Folder input with folder list in `initUI`**

Replace lines 464–479 (the `# SRT Folder` section) with:

```python
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
```

- [ ] **Step 3: Add `add_folder` method and remove `select_folder`**

Replace `select_folder` (lines 851–858) with:

```python
    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Directory",
            self.srt_paths[-1] if self.srt_paths else os.path.expanduser("~")
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
```

- [ ] **Step 4: Add `_add_folder_row`, `_remove_folder_item`, and update `_save_current_settings`**

Add these methods to `TTSApp`:

```python
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
```

Update `_save_current_settings` (line 649–652):

```python
    def _save_current_settings(self):
        self._settings["srt_paths"] = list(self._folder_widgets.keys())
        self._settings["servers"] = list(self._server_widgets.keys())
        save_settings(self._settings)
```

- [ ] **Step 5: Restore folder list on startup**

In `TTSApp.__init__`, after restoring servers (line 409–411), add folder restoration:

```python
        # 恢复上次的文件夹列表
        for path in self._settings.get("srt_paths", []):
            if path and os.path.isdir(path):
                self._add_folder_row(path)
```

- [ ] **Step 6: Update `_update_run_btn` to check folder list**

Update `_update_run_btn` (lines 712–718):

```python
    def _update_run_btn(self):
        if self._get_server_configs() and self._folder_widgets:
            self.run_btn.setEnabled(True)
            self.run_btn.setToolTip("")
        else:
            self.run_btn.setEnabled(False)
            if not self._folder_widgets:
                self.run_btn.setToolTip("Please add at least one SRT folder.")
            else:
                self.run_btn.setToolTip("Please connect to at least one API server first.")
```

- [ ] **Step 7: Commit**

```bash
git add gui.pyw
git commit -m "feat: replace single path input with folder list widget"
```

---

### Task 3: Update WorkerThread and start_processing for multi-folder

**Files:**
- Modify: `gui.pyw`

- [ ] **Step 1: Update WorkerThread to accept list**

Change `WorkerThread.__init__` (lines 297–304):

```python
    def __init__(self, srt_paths, transformers_line, adapter, server_configs: dict,
                 max_retries: int = 3):
        super().__init__()
        self.srt_paths = srt_paths          # now a list
        self.transformers_line = transformers_line
        self.adapter = adapter
        self.server_configs = server_configs
        self.max_retries = max_retries
```

And `WorkerThread.run` (lines 306–322):

```python
    def run(self):
        config.USE_TQDM_PROGRESS_BAR = False
        config.ABORT_ALL = False
        try:
            process_srt_files(
                srt_paths=self.srt_paths,
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
```

- [ ] **Step 2: Update `start_processing`**

Change `start_processing` (lines 860–889):

```python
    def start_processing(self):
        line_idx = self._settings.get("line_index", 2)
        server_configs = self._get_server_configs()
        if not server_configs:
            self.append_log("Error: No online servers available.")
            return

        srt_paths = list(self._folder_widgets.keys())
        if not srt_paths:
            self.append_log("Error: No folders added.")
            return

        self.run_btn.setEnabled(False)
        self.run_btn.setText("PROCESSING...")
        self.add_folder_btn.setEnabled(False)
        self.add_btn.setEnabled(False)
        self.settings_btn.setEnabled(False)
        self.log_output.clear()
        self.bar_total_progress.setValue(0)
        self.bar_task_progress.setValue(0)
        self.lbl_task_progress.setText("Current File Tasks: Initializing...")

        self.append_log(f"> 共 {len(srt_paths)} 个文件夹，使用 {len(server_configs)} 台服务器")
        for p in srt_paths:
            self.append_log(f"  · {p}")
        for u, n in server_configs.items():
            self.append_log(f"  · {u}  线程数: {n}")

        self.worker = WorkerThread(
            srt_paths=srt_paths,
            transformers_line=line_idx,
            adapter=self.adapter,
            server_configs=server_configs,
            max_retries=self._settings.get("max_retries", 3),
        )
        self.worker.finished_signal.connect(self.process_finished)
        self.worker.start()
```

- [ ] **Step 3: Update `process_finished` to enable correct buttons**

In `process_finished` (lines 918–922), change `self.path_btn` to `self.add_folder_btn`:

```python
    def process_finished(self, completed: bool):
        self.run_btn.setEnabled(True)
        self.run_btn.setText("START DUBBING")
        self.add_folder_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.settings_btn.setEnabled(True)
        self.lbl_task_progress.setText("Status: Idle")
```

- [ ] **Step 4: Commit**

```bash
git add gui.pyw
git commit -m "feat: wire WorkerThread and UI for multi-folder processing"
```

---

### Task 4: Update process_srt_files for multi-folder loop

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Change `process_srt_files` signature and add outer loop**

Change the function signature (line 244):

```python
def process_srt_files(srt_paths, transformers_line: int = TRANSFORMERS_LINE,
                      max_workers: int = 2,
                      output_path: str = None, ref_audio_path: str = None,
                      progress_callback=None,
                      server_configs: Dict[str, int] = None,
                      on_server_down=None,
                      on_all_down=None,
                      max_retries: int = None):
    """
    主处理函数。srt_paths 可以是单个字符串（兼容旧调用）或字符串列表。
    """
```

Add path normalization at the top (after the log function definition, around line 259):

```python
    # ── 兼容旧调用：单个字符串转为列表 ──
    if isinstance(srt_paths, str):
        srt_paths = [srt_paths]
    srt_paths = [os.path.normpath(p) for p in srt_paths]
```

- [ ] **Step 2: Pre-scan all folders and report total file count**

After normalizing paths and before the main loop, pre-count files. Replace the existing single-folder file listing logic (lines 282–311) with a pre-scan:

```python
    # ── 预扫描所有文件夹，统计 SRT 文件总数 ──
    all_folder_files: list = []  # [(srt_path, srt_file), ...]
    valid_extensions = {".srt", ".txt"}
    for srt_path in srt_paths:
        try:
            files = [
                f for f in os.listdir(srt_path)
                if os.path.splitext(f)[1].lower() in valid_extensions
                   and not f.endswith("zh.srt") and not f.endswith("en.srt")
            ]
        except FileNotFoundError:
            log(f"错误: 路径不存在 {srt_path}")
            continue
        try:
            files = sorted(files, key=lambda x: int(re.findall(r'\d+', x)[0]))
        except (IndexError, ValueError):
            files.sort()
        seen = set()
        for f in files:
            base = os.path.splitext(f)[0]
            if base not in seen:
                seen.add(base)
                all_folder_files.append((srt_path, f))

    total_files = len(all_folder_files)
    if progress_callback:
        progress_callback.set_total_files(total_files)
```

- [ ] **Step 3: Wrap the per-file processing loop in a folder-level loop**

Replace the per-file loop (from line 313 `processed_basenames = set()` through the end of the per-file processing at line 586) to iterate over `all_folder_files` with a cumulative counter and per-folder output paths. The key change: instead of iterating `srt_files_to_process` from a single path, iterate `all_folder_files` and derive `output_path`, `output_folder_zh`, `ref_audio_path` from each item's `srt_path`:

```python
    global_file_idx = 0
    processed_basenames = set()

    for srt_path, srt_file in all_folder_files:
        if config.ABORT_ALL:
            log("!!! 任务已由用户强制终止 !!!")
            break

        subtitle_name, _ = os.path.splitext(srt_file)
        dedup_key = (srt_path, subtitle_name)
        if dedup_key in processed_basenames:
            continue
        processed_basenames.add(dedup_key)

        # Per-folder output paths
        output_path = srt_path
        output_folder_zh = os.path.join(srt_path, "中配")
        os.makedirs(output_folder_zh, exist_ok=True)
        ref_audio_path = os.path.join(srt_path, "REF_AUDIO_PATH")
        os.makedirs(ref_audio_path, exist_ok=True)

        if progress_callback:
            progress_callback.update_file_progress(global_file_idx)
        log(f"\n[{global_file_idx + 1}/{total_files}] === 检查文件 {srt_file} ===")
        log(f"  目录: {srt_path}")

        output_audio_file = os.path.join(output_path, f"{subtitle_name}.wav")

        skip_result = _check_task_skip_or_recover(
            subtitle_name, output_folder_zh, output_audio_file, srt_path, log
        )
        if skip_result == "skip":
            global_file_idx += 1
            continue

        # ── The rest of the per-file body (main_audio_path extraction, subtitle parsing,
        #     task generation, merge) stays EXACTLY as-is, using the local srt_path ──

        main_audio_path = os.path.join(ref_audio_path, subtitle_name + ".wav")
        found_video_path = _find_video_path(subtitle_name, srt_path)
        audio_extracted_in_this_run = False

        if not os.path.exists(main_audio_path):
            if found_video_path:
                log(f"正在从视频提取参考音频: {os.path.basename(found_video_path)}")
                if extract_single_audio(found_video_path, main_audio_path):
                    audio_extracted_in_this_run = True
                else:
                    log(f"!! ffmpeg 提取音频失败，跳过此文件。")
                    global_file_idx += 1
                    continue
            else:
                log(f"!! 警告: 在 '{srt_path}' 下找不到与 '{subtitle_name}' 匹配的视频文件，跳过。")
                global_file_idx += 1
                continue

        if not os.path.exists(main_audio_path):
            global_file_idx += 1
            continue

        log(f"开始生成: {srt_file}")
        safe_dir_name = hashlib.md5(subtitle_name.encode('utf-8')).hexdigest()
        dynamic_tmp_root = os.path.join(srt_path, "tmp")
        current_tmp_dir = _longpath(os.path.join(dynamic_tmp_root, safe_dir_name))
        os.makedirs(current_tmp_dir, exist_ok=True)
        local_status_file = os.path.join(current_tmp_dir, "status.json")

        try:
            with open(os.path.join(srt_path, srt_file), "r", encoding="utf-8") as file:
                file_content = file.read()
        except Exception as e:
            log(f"读取文件失败: {e}")
            if audio_extracted_in_this_run and os.path.exists(main_audio_path):
                os.remove(main_audio_path)
            global_file_idx += 1
            continue

        parsed_subtitles = parse_subtitles(file_content, transformers_line)
        if not parsed_subtitles:
            log(f"无有效字幕，跳过。")
            if audio_extracted_in_this_run and os.path.exists(main_audio_path):
                os.remove(main_audio_path)
            global_file_idx += 1
            continue

        merged_subtitles = merge_consecutive_subtitles(parsed_subtitles)

        completed_indices = load_status(local_status_file, srt_file)
        all_indices = list(range(len(merged_subtitles)))

        recovered_count = 0
        for idx in all_indices:
            if idx not in completed_indices:
                potential_wav = os.path.join(current_tmp_dir, f"output_{idx}.wav")
                if os.path.exists(potential_wav) and os.path.getsize(potential_wav) > 1000:
                    completed_indices.add(idx)
                    recovered_count += 1
        if recovered_count > 0:
            log(f"  -> [恢复系统] 扫描到 {recovered_count} 个已存在片段，将跳过。")
            save_status(local_status_file, srt_file, completed_indices)

        uncompleted_indices = [idx for idx in all_indices if idx not in completed_indices]
        total_tasks = len(all_indices)

        if progress_callback:
            progress_callback.set_current_task_range(total_tasks)
            progress_callback.update_task_progress(len(completed_indices))

        if not uncompleted_indices:
            log("--- 所有片段均已存在，直接合并 ---")
        else:
            if config.ABORT_ALL:
                break

            if use_multi_server:
                log(f"--- [多服务器] 剩余 {len(uncompleted_indices)} 个任务，需求驱动分发 ---")

                def _on_server_down(url, stats):
                    log(f"⚠️ 服务器下线: {url} | {stats}")
                    if on_server_down:
                        on_server_down(url, stats)

                def _on_all_down():
                    log("🛑 所有服务器均已熔断，停止程序！")
                    setattr(config, 'ABORT_ALL', True)
                    if on_all_down:
                        on_all_down()

                dispatcher = MultiServerDispatcher(
                    server_configs=server_configs,
                    max_retries=max_retries if max_retries is not None else config.MAX_RETRIES,
                    on_server_down=_on_server_down,
                    on_all_down=_on_all_down,
                )
                dispatcher.start()

                completed_count = len(completed_indices)
                file_start_time = time.time()
                session_processed_count = 0

                def post_process_fn(idx, raw_path, server_url):
                    if config.ABORT_ALL:
                        return
                    params = task_params_map.get(idx)
                    if params is None:
                        return
                    post_process_audio_task(
                        idx, raw_path,
                        params["speed"], params["text"],
                        params["target_duration_s"],
                        current_tmp_dir
                    )
                    with status_lock:
                        completed_indices.add(idx)
                        save_status(local_status_file, srt_file, completed_indices)

                def done_callback(idx, server_url):
                    nonlocal completed_count, session_processed_count
                    if config.ABORT_ALL:
                        return
                    with status_lock:
                        completed_count += 1
                        session_processed_count += 1
                        elapsed = time.time() - file_start_time
                        if elapsed > 0 and session_processed_count > 0:
                            spd = session_processed_count / elapsed
                            speed_str = f"{spd:.2f} it/s" if spd >= 1 else f"{1 / spd:.2f} s/it"
                        else:
                            speed_str = "Calc..."
                        from urllib.parse import urlparse
                        _p = urlparse(server_url)
                        server_tag = f"{_p.hostname}:{_p.port}" if _p.port else _p.hostname
                        if progress_callback:
                            progress_callback.update_task_progress(completed_count)
                        log(f"  -> Task {idx} 完成 ({completed_count}/{total_tasks}) "
                            f"[{speed_str}] [{server_tag}]")

                task_params_map = {}

                def task_iterator():
                    last_valid_ref_path = None
                    for index in uncompleted_indices:
                        if config.ABORT_ALL:
                            return
                        params = _prepare_tts_params(
                            index, merged_subtitles[index],
                            main_audio_path, current_tmp_dir,
                            last_valid_ref_path=last_valid_ref_path
                        )
                        if params is None:
                            with status_lock:
                                completed_indices.add(index)
                            continue
                        last_valid_ref_path = params["ref_audio_path"]
                        task_params_map[index] = params
                        yield (index,
                               params["ref_audio_path"],
                               params["text"],
                               params["speed"])

                dispatcher.run_feeder(
                    task_iter=task_iterator(),
                    post_process_fn=post_process_fn,
                    done_callback=done_callback,
                    abort_flag_fn=lambda: config.ABORT_ALL,
                )
                dispatcher.stop()

            else:
                import concurrent.futures
                log(f"--- 剩余 {len(uncompleted_indices)} 个任务 (并发数: {max_workers}) ---")

                pbar = None
                if not progress_callback and USE_TQDM_PROGRESS_BAR:
                    pbar = tqdm(total=len(uncompleted_indices), desc=f"处理 {subtitle_name}")

                completed_count = len(completed_indices)
                file_start_time = time.time()
                session_processed_count = 0

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = set()
                    for index in uncompleted_indices:
                        if config.ABORT_ALL:
                            break
                        subtitle = merged_subtitles[index]
                        gen_future = executor.submit(tts_generation_task, index, subtitle,
                                                     main_audio_path, current_tmp_dir)

                        def process_when_done(fut, idx=index, pbar_instance=pbar):
                            nonlocal completed_count, session_processed_count
                            if config.ABORT_ALL:
                                return
                            try:
                                gen_result = fut.result()
                                post_result = post_process_audio_task(idx, *gen_result, current_tmp_dir)
                                with status_lock:
                                    completed_indices.add(post_result)
                                    save_status(local_status_file, srt_file, completed_indices)
                                    completed_count += 1
                                    session_processed_count += 1
                                    elapsed_time = time.time() - file_start_time
                                    speed_str = ""
                                    if elapsed_time > 0 and session_processed_count > 0:
                                        speed = session_processed_count / elapsed_time
                                        speed_str = f"{speed:.2f} it/s" if speed >= 1 else f"{1 / speed:.2f} s/it"
                                    else:
                                        speed_str = "Calc..."
                                    if progress_callback:
                                        progress_callback.update_task_progress(completed_count)
                                    elif pbar_instance:
                                        pbar_instance.update(1)
                                    log(f"  -> Task {idx} 完成 ({completed_count}/{total_tasks}) [{speed_str}]")
                            except Exception as e:
                                log(f"!! [Task {idx}] 错误: {e}")

                        gen_future.add_done_callback(process_when_done)
                        futures.add(gen_future)
                    concurrent.futures.wait(futures)

                if pbar:
                    pbar.close()

        if config.ABORT_ALL:
            break

        log("--- 字幕片段合并中... ---")
        try:
            merged_audio = merge_audio(merged_subtitles, current_tmp_dir)
            merged_audio.export(output_audio_file, format="wav")
            log(f"输出音频: {os.path.basename(output_audio_file)}")
            clear_status(local_status_file)
            if os.path.exists(current_tmp_dir):
                shutil.rmtree(current_tmp_dir)
            duration_predictor.train()
            _find_and_merge_video(subtitle_name, output_audio_file, output_folder_zh, srt_path, log)
        except Exception as e:
            log(f"!! 合并错误: {e}")
        finally:
            if os.path.exists(main_audio_path):
                try:
                    os.remove(main_audio_path)
                except OSError:
                    pass

        global_file_idx += 1

    if progress_callback:
        progress_callback.update_file_progress(total_files)
    msg = "\n=== 任务已强制停止 ===" if config.ABORT_ALL else "\n=== 所有任务处理完毕 ==="
    log(msg)

    # Clean up empty REF_AUDIO_PATH dirs
    for srt_path in srt_paths:
        ref_ap = os.path.join(srt_path, "REF_AUDIO_PATH")
        if os.path.exists(ref_ap) and not os.listdir(ref_ap):
            try:
                os.rmdir(ref_ap)
            except Exception:
                pass
```

> **Note:** The key structural changes are:
> 1. Pre-scan `srt_paths` to build `all_folder_files` list (path + filename pairs)
> 2. Each iteration derives `output_path`, `output_folder_zh`, `ref_audio_path` from the current `srt_path`
> 3. `processed_basenames` keys are now `(srt_path, subtitle_name)` tuples
> 4. `global_file_idx` increments at the end of each iteration for cumulative progress

- [ ] **Step 2: Commit**

```bash
git add main.py
git commit -m "feat: support multiple SRT folders in process_srt_files"
```

---

### Task 5: Final cleanup — remove unused `self.path_btn` reference

**Files:**
- Modify: `gui.pyw`

- [ ] **Step 1: Check for any remaining stale references**

Grep `gui.pyw` for `path_btn`, `path_input`, `select_folder`, `current_srt_path` to ensure nothing stale remains:

```bash
grep -n "path_btn\|path_input\|select_folder\|current_srt_path" gui.pyw
```

Expected: only `self.path_btn.setEnabled(False)` in `start_processing` (we intentionally left this as a no-op for safety — but actually we renamed it to `add_folder_btn`, so this line should NOT exist). We need to remove the line `self.path_btn.setEnabled(False)` from `start_processing` since we no longer have `path_btn`.

Actually, in Task 3 Step 2 we already wrote the updated `start_processing` which uses `self.add_folder_btn.setEnabled(False)` — so this is already handled. This grep is just a verification step.

- [ ] **Step 2: Verify no stale references and commit**

```bash
grep -n "\.path_btn\|\.path_input\|\.select_folder\|\.current_srt_path" gui.pyw
```

If clean, skip commit. If there are stale references, fix them and commit:

```bash
git add gui.pyw
git commit -m "chore: remove stale single-folder references from gui"
```
