# Background Finalize Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move final WAV merge and MP4 muxing into a single-worker background queue so the next TTS file can start without waiting for final artifact creation.

**Architecture:** Keep finalization in `main.py` because the current pipeline logic already lives there. Extract the current synchronous merge/export/video-mux block into `_finalize_file_outputs(...)`, submit it through `ThreadPoolExecutor(max_workers=1)`, and wait for all finalize futures before reporting overall completion. UI logs continue through the existing `log(...)` callback, which reaches Qt through `ProgressAdapter.log_signal`.

**Tech Stack:** Python, `concurrent.futures.ThreadPoolExecutor`, pydub `AudioSegment`, existing `pytest` and `monkeypatch` tests.

---

## File Structure

- Modify: `main.py`
  - Add `_finalize_file_outputs(...)` after `_check_task_skip_or_recover(...)`.
  - Add `_wait_for_finalize_jobs(...)` after `_finalize_file_outputs(...)`.
  - Update `process_srt_files(...)` to create a single-worker finalize executor, submit finalization jobs, and wait for them before returning.
  - Make `process_srt_files(...)` return `True` only when the run is not aborted and all finalize jobs succeeded.
- Modify: `gui.pyw`
  - Update `WorkerThread.run(...)` to use the boolean returned by `process_srt_files(...)` for `finished_signal`.
- Create: `tests/test_background_finalize.py`
  - Focused tests for finalization success, finalization failure cleanup behavior, and finalize future waiting.

---

### Task 1: Add Failing Tests For File Finalization

**Files:**
- Create: `tests/test_background_finalize.py`

- [ ] **Step 1: Create the test file with helper imports**

Create `tests/test_background_finalize.py` with this content:

```python
import os
import sys
from concurrent.futures import Future

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydub import AudioSegment

import main


def _write_silence(path, duration_ms):
    AudioSegment.silent(duration=duration_ms).export(path, format="wav")
```

- [ ] **Step 2: Add the success-path finalization test**

Append this test to `tests/test_background_finalize.py`:

```python
def test_finalize_file_outputs_exports_wav_merges_video_and_cleans_tmp(tmp_path, monkeypatch):
    current_tmp_dir = tmp_path / "tmp" / "abc"
    current_tmp_dir.mkdir(parents=True)
    _write_silence(current_tmp_dir / "output_0.wav", 1000)
    status_file = current_tmp_dir / "status.json"
    status_file.write_text('{"demo.srt": [0]}', encoding="utf-8")

    output_audio_file = tmp_path / "demo.wav"
    output_folder_zh = tmp_path / "zh"
    output_folder_zh.mkdir()
    srt_path = tmp_path
    parsed_subtitles = [["00:00:00,000", "00:00:01,000", "hello", ""]]
    logs = []
    merged_video_calls = []
    train_calls = []

    monkeypatch.setattr(
        main,
        "_find_and_merge_video",
        lambda subtitle_name, audio_path, folder, source_path, log: merged_video_calls.append(
            (subtitle_name, audio_path, folder, source_path)
        ),
    )
    monkeypatch.setattr(main.duration_predictor, "train", lambda: train_calls.append("train"))

    ok = main._finalize_file_outputs(
        subtitle_name="demo",
        parsed_subtitles=parsed_subtitles,
        current_tmp_dir=str(current_tmp_dir),
        output_audio_file=str(output_audio_file),
        output_folder_zh=str(output_folder_zh),
        srt_path=str(srt_path),
        local_status_file=str(status_file),
        log=logs.append,
    )

    assert ok is True
    assert output_audio_file.exists()
    assert not current_tmp_dir.exists()
    assert merged_video_calls == [("demo", str(output_audio_file), str(output_folder_zh), str(srt_path))]
    assert train_calls == ["train"]
    assert any("[后台合并] 开始" in line for line in logs)
    assert any("[后台合并] 完成" in line for line in logs)
```

- [ ] **Step 3: Add the failure-path finalization test**

Append this test to `tests/test_background_finalize.py`:

```python
def test_finalize_file_outputs_failure_preserves_tmp_and_status(tmp_path, monkeypatch):
    current_tmp_dir = tmp_path / "tmp" / "abc"
    current_tmp_dir.mkdir(parents=True)
    status_file = current_tmp_dir / "status.json"
    status_file.write_text('{"demo.srt": [0]}', encoding="utf-8")

    output_audio_file = tmp_path / "demo.wav"
    output_folder_zh = tmp_path / "zh"
    output_folder_zh.mkdir()
    parsed_subtitles = [["00:00:00,000", "00:00:01,000", "hello", ""]]
    logs = []

    def raise_merge_error(subtitles, tmp_dir):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(main, "merge_audio", raise_merge_error)

    ok = main._finalize_file_outputs(
        subtitle_name="demo",
        parsed_subtitles=parsed_subtitles,
        current_tmp_dir=str(current_tmp_dir),
        output_audio_file=str(output_audio_file),
        output_folder_zh=str(output_folder_zh),
        srt_path=str(tmp_path),
        local_status_file=str(status_file),
        log=logs.append,
    )

    assert ok is False
    assert current_tmp_dir.exists()
    assert status_file.exists()
    assert not output_audio_file.exists()
    assert any("[后台合并] 失败" in line for line in logs)
    assert any("merge exploded" in line for line in logs)
```

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_background_finalize.py -v
```

Expected: both tests fail with `AttributeError: module 'main' has no attribute '_finalize_file_outputs'`.

- [ ] **Step 5: Commit the failing tests**

```bash
git add tests/test_background_finalize.py
git commit -m "test: cover background file finalization"
```

---

### Task 2: Implement `_finalize_file_outputs`

**Files:**
- Modify: `main.py`, after `_check_task_skip_or_recover(...)`
- Test: `tests/test_background_finalize.py`

- [ ] **Step 1: Insert the finalization helper**

Add this function immediately after `_check_task_skip_or_recover(...)` in `main.py`:

```python
def _finalize_file_outputs(subtitle_name: str,
                           parsed_subtitles: List[List],
                           current_tmp_dir: str,
                           output_audio_file: str,
                           output_folder_zh: str,
                           srt_path: str,
                           local_status_file: str,
                           log) -> bool:
    """Merge generated segments, mux video, and clean temp files for one subtitle."""
    log(f"--- [后台合并] 开始: {subtitle_name} ---")
    try:
        segment_issues = inspect_audio_segments(parsed_subtitles, current_tmp_dir)
        if segment_issues:
            hard_types = {"missing", "too_small", "stat_error", "unreadable", "empty_audio"}
            hard_issues = [issue for issue in segment_issues if issue["type"] in hard_types]
            log(f"!! [MERGE_CHECK] Found {len(segment_issues)} segment issue(s) before merge; "
                f"hard={len(hard_issues)}")
            for issue in segment_issues[:20]:
                actual = issue.get("actual_ms", "-")
                delta = issue.get("delta_ms", "-")
                size = issue.get("size_bytes", "-")
                log(
                    f"!! [MERGE_CHECK] idx={issue['index']} type={issue['type']} "
                    f"target={issue['target_ms']}ms actual={actual}ms delta={delta}ms "
                    f"size={size} file={os.path.basename(issue['path'])}"
                )
            if len(segment_issues) > 20:
                log(f"!! [MERGE_CHECK] ... {len(segment_issues) - 20} more issue(s) omitted")
            if hard_issues:
                log(f"!! [MERGE_CHECK] {len(hard_issues)} missing/corrupt segment(s) "
                    "will be filled with silence to keep batch output moving")

        log(f"--- [后台合并] 合并音频: {subtitle_name} ---")
        merged_audio = merge_audio(parsed_subtitles, current_tmp_dir)
        merged_audio.export(output_audio_file, format="wav")
        log(f"[后台合并] 输出音频: {os.path.basename(output_audio_file)}")

        clear_status(local_status_file)
        duration_predictor.train()

        log(f"--- [后台合并] 准备合并音视频: {subtitle_name} ---")
        _find_and_merge_video(subtitle_name, output_audio_file, output_folder_zh, srt_path, log)

        if os.path.exists(current_tmp_dir):
            shutil.rmtree(current_tmp_dir)

        log(f"--- [后台合并] 完成: {subtitle_name} ---")
        return True
    except Exception as e:
        log(f"!! [后台合并] 失败: {subtitle_name} | {e}")
        import traceback
        log(traceback.format_exc())
        return False
```

- [ ] **Step 2: Run the finalization tests**

Run:

```bash
python -m pytest tests/test_background_finalize.py -v
```

Expected: both tests pass.

- [ ] **Step 3: Commit the helper implementation**

```bash
git add main.py tests/test_background_finalize.py
git commit -m "feat: extract file finalization helper"
```

---

### Task 3: Add And Implement Finalize Future Waiting

**Files:**
- Modify: `tests/test_background_finalize.py`
- Modify: `main.py`, after `_finalize_file_outputs(...)`

- [ ] **Step 1: Add tests for future waiting**

Append these tests to `tests/test_background_finalize.py`:

```python
def test_wait_for_finalize_jobs_returns_true_when_all_jobs_succeed():
    future = Future()
    future.set_result(True)
    logs = []

    ok = main._wait_for_finalize_jobs([("demo", future)], logs.append)

    assert ok is True
    assert any("等待后台合并任务完成" in line for line in logs)
    assert any("所有后台合并任务已完成" in line for line in logs)


def test_wait_for_finalize_jobs_returns_false_when_a_job_fails():
    good = Future()
    good.set_result(True)
    bad = Future()
    bad.set_result(False)
    logs = []

    ok = main._wait_for_finalize_jobs([("good", good), ("bad", bad)], logs.append)

    assert ok is False
    assert any("后台合并任务失败: bad" in line for line in logs)


def test_wait_for_finalize_jobs_returns_false_when_a_job_raises():
    future = Future()
    future.set_exception(RuntimeError("future exploded"))
    logs = []

    ok = main._wait_for_finalize_jobs([("demo", future)], logs.append)

    assert ok is False
    assert any("后台合并任务异常: demo" in line for line in logs)
    assert any("future exploded" in line for line in logs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_background_finalize.py -v
```

Expected: the three new tests fail with `AttributeError: module 'main' has no attribute '_wait_for_finalize_jobs'`.

- [ ] **Step 3: Add `_wait_for_finalize_jobs`**

Add this function immediately after `_finalize_file_outputs(...)` in `main.py`:

```python
def _wait_for_finalize_jobs(finalize_futures, log) -> bool:
    if not finalize_futures:
        return True

    log(f"--- 等待后台合并任务完成 ({len(finalize_futures)}) ---")
    all_ok = True
    for subtitle_name, future in finalize_futures:
        try:
            ok = future.result()
        except Exception as e:
            all_ok = False
            log(f"!! 后台合并任务异常: {subtitle_name} | {e}")
            import traceback
            log(traceback.format_exc())
            continue

        if not ok:
            all_ok = False
            log(f"!! 后台合并任务失败: {subtitle_name}")

    if all_ok:
        log("--- 所有后台合并任务已完成 ---")
    else:
        log("!! 部分后台合并任务失败，请查看上方日志并保留临时文件用于恢复")
    return all_ok
```

- [ ] **Step 4: Run all background finalize tests**

Run:

```bash
python -m pytest tests/test_background_finalize.py -v
```

Expected: all tests in `tests/test_background_finalize.py` pass.

- [ ] **Step 5: Commit future waiting helper**

```bash
git add main.py tests/test_background_finalize.py
git commit -m "feat: add finalize job waiting"
```

---

### Task 4: Wire Background Finalization Into `process_srt_files`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add finalize executor state before the file loop**

In `process_srt_files(...)`, immediately before `try:` at the current main file loop, add:

```python
    finalize_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    finalize_futures = []
    finalize_all_ok = True
```

Locate the existing `dispatcher.start()` call in the multi-server setup. Insert the three new lines after that setup block and before the existing outer `try:` that starts the `for srt_path, srt_file in all_folder_files:` loop.

- [ ] **Step 2: Replace the synchronous merge block with finalize submission**

Replace the whole current synchronous merge block near the end of the per-file loop. The block starts with:

```python
                # ── 同步合并：处理完一个文件立即合并，再处理下一个 ──
                log("--- 字幕片段合并中... ---")
```

and ends with:

```python
                except Exception as e:
                    log(f"!! 合并错误: {e}")
```

with:

```python
                log(f"--- 提交后台合并任务: {subtitle_name} ---")
                future = finalize_executor.submit(
                    _finalize_file_outputs,
                    subtitle_name=subtitle_name,
                    parsed_subtitles=parsed_subtitles,
                    current_tmp_dir=current_tmp_dir,
                    output_audio_file=output_audio_file,
                    output_folder_zh=output_folder_zh,
                    srt_path=srt_path,
                    local_status_file=local_status_file,
                    log=log,
                )
                finalize_futures.append((subtitle_name, future))
                task_completed_successfully = True
```

- [ ] **Step 3: Update per-file cleanup so it does not delete queued finalize inputs**

Replace the per-file `finally` cleanup block:

```python
                if task_completed_successfully or config.ABORT_ALL:
                    if os.path.exists(current_tmp_dir):
                        try:
                            shutil.rmtree(current_tmp_dir)
                        except Exception as e:
                            log(f"!! 清理临时目录失败: {e}")
```

with:

```python
                if config.ABORT_ALL and not task_completed_successfully:
                    log(f"  -> [中止] 保留临时目录用于恢复: {current_tmp_dir}")
```

This keeps temp files available for the background finalize job after successful TTS generation, and preserves partial artifacts on abort.

- [ ] **Step 4: Wait for finalize jobs before final progress and completion logs**

Immediately before:

```python
        if progress_callback:
            progress_callback.update_file_progress(total_files)
```

insert:

```python
        finalize_all_ok = _wait_for_finalize_jobs(finalize_futures, log)
```

Then replace:

```python
        msg = "\n=== 任务已强制停止 ===" if config.ABORT_ALL else "\n=== 所有任务处理完毕 ==="
        log(msg)
```

with:

```python
        if config.ABORT_ALL:
            msg = "\n=== 任务已强制停止 ==="
        elif finalize_all_ok:
            msg = "\n=== 所有任务处理完毕 ==="
        else:
            msg = "\n=== TTS 已完成，但部分后台合并失败 ==="
        log(msg)
```

- [ ] **Step 5: Shut down the finalize executor and return completion state**

In the outer `finally` block at the end of `process_srt_files(...)`, add finalize executor shutdown before dispatcher shutdown:

```python
        if 'finalize_executor' in locals():
            finalize_executor.shutdown(wait=True)
```

Then add this return at the end of the normal `try` block, after empty `REF_AUDIO_PATH` cleanup and before the outer `except`:

```python
        return (not config.ABORT_ALL) and finalize_all_ok
```

In the outer `except Exception as e:` block, after logging the traceback, add:

```python
        return False
```

- [ ] **Step 6: Run targeted tests**

Run:

```bash
python -m pytest tests/test_background_finalize.py tests/test_audio_merge.py -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit process integration**

```bash
git add main.py tests/test_background_finalize.py
git commit -m "feat: run file finalization in background"
```

---

### Task 5: Use `process_srt_files` Return Value In The GUI Worker

**Files:**
- Modify: `gui.pyw`, `WorkerThread.run(...)`

- [ ] **Step 1: Update worker completion logic**

In `gui.pyw`, replace the body of `WorkerThread.run(...)` with:

```python
    def run(self):
        try:
            completed = process_srt_files(
                self.srt_paths,
                self.transformers_line,
                max_workers=self.max_workers,
                progress_callback=self.adapter,
                server_configs=self.server_configs,
                on_server_down=self.adapter.notify_server_down,
                on_all_down=self.adapter.notify_all_down,
                max_retries=self.max_retries,
                circuit_breaker_threshold=self.circuit_breaker_threshold,
            )
            self.finished_signal.emit(bool(completed) and not config.ABORT_ALL)
        except Exception as e:
            self.adapter.log(f"Error: {e}")
            self.finished_signal.emit(False)
```

- [ ] **Step 2: Run a syntax check**

Run:

```bash
python -m py_compile main.py gui.pyw
```

Expected: command exits with code 0 and prints no syntax errors.

- [ ] **Step 3: Run targeted tests again**

Run:

```bash
python -m pytest tests/test_background_finalize.py tests/test_audio_merge.py -v
```

Expected: all selected tests pass.

- [ ] **Step 4: Commit GUI completion semantics**

```bash
git add gui.pyw main.py tests/test_background_finalize.py
git commit -m "fix: report completion after background finalize"
```

---

### Task 6: Final Verification

**Files:**
- Verify: `main.py`
- Verify: `gui.pyw`
- Verify: `tests/test_background_finalize.py`
- Verify: `tests/test_audio_merge.py`

- [ ] **Step 1: Run focused test suite**

Run:

```bash
python -m pytest tests/test_background_finalize.py tests/test_audio_merge.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run syntax check**

Run:

```bash
python -m py_compile main.py gui.pyw audio_processor.py dispatcher.py
```

Expected: command exits with code 0 and prints no syntax errors.

- [ ] **Step 3: Manually verify UI behavior**

Run the app:

```bash
python gui.pyw
```

Manual expected behavior with at least two files:

1. File A finishes TTS segments.
2. UI log shows `提交后台合并任务: File A`.
3. File B begins TTS before File A's MP4 merge finishes.
4. UI log later shows File A background merge output and video merge result.
5. The final tray/UI completion message appears only after all background finalize jobs finish.

- [ ] **Step 4: Inspect diff**

Run:

```bash
git diff -- main.py gui.pyw tests/test_background_finalize.py
```

Expected: diff only contains background finalization, completion-state, and tests described in this plan.

- [ ] **Step 5: Commit final verification notes if any code changed during verification**

If verification required code fixes, commit them:

```bash
git add main.py gui.pyw tests/test_background_finalize.py
git commit -m "fix: polish background finalize verification"
```

If no files changed during verification, do not create an empty commit.
