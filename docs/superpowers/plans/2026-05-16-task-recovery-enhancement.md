# Task Completion Detection & Recovery Enhancement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the 3-case task recovery logic so that re-running the process correctly handles all leftover states: (1) video exists → skip, (2) WAV exists but no video → merge only, (3) neither → full TTS pipeline.

**Architecture:** The 3-case check already exists in `process_srt_files()` at lines 273-281 of `main.py`. This plan adds validity checks on recovered WAVs, clearer recovery logging, and a dedicated `_recover_or_skip()` helper to keep the main loop clean.

**Tech Stack:** Python, pydub, ffmpeg

---

## Current State Analysis

The core 3-case logic already exists in `main.py:273-281`:

```python
# Case 1: Video in 中配 → done
if os.path.exists(target_video_output):
    log(f"  -> 最终视频已存在，跳过。")
    continue

# Case 2: WAV exists → merge video only
if os.path.exists(output_audio_file):
    log(f"文件 {output_audio_file} 已存在，跳过生成。")
    _find_and_merge_video(subtitle_name, output_audio_file)
    continue

# Case 3: Neither → full pipeline (extract audio → TTS → merge WAV → merge video)
```

**Gap identified:** Case 2 recovers by calling `_find_and_merge_video`, but doesn't validate the WAV file is healthy (non-zero, playable). A corrupted 0-byte WAV left by a crash would trigger recovery and then fail silently at ffmpeg merge. We should add a validity check and fall through to Case 3 if the WAV is broken.

---

### Task 1: Extract recovery check into a helper and add WAV validity validation

**Files:**
- Modify: `D:\Users\xuan\Mycode\MultTTS\main.py`

- [ ] **Step 1: Add WAV validity check and consolidate recovery logic**

Replace lines 270-281 of `main.py` (from `output_audio_file = ...` through the `continue` after `_find_and_merge_video`) with a call to a new helper, then define the helper above `process_srt_files`.

The current block:
```python
        output_audio_file = os.path.join(output_path, f"{subtitle_name}.wav")

        # 最终中配视频已存在 → 彻底完成，直接跳过
        target_video_output = os.path.join(output_folder_zh, f"{subtitle_name}.mp4")
        if os.path.exists(target_video_output):
            log(f"  -> 最终视频已存在，跳过。")
            continue

        if os.path.exists(output_audio_file):
            log(f"文件 {output_audio_file} 已存在，跳过生成。")
            _find_and_merge_video(subtitle_name, output_audio_file)
            continue
```

Replace with:
```python
        output_audio_file = os.path.join(output_path, f"{subtitle_name}.wav")

        skip_result = _check_task_skip_or_recover(
            subtitle_name, output_folder_zh, output_audio_file, srt_path, log
        )
        if skip_result == "skip":
            continue
        # skip_result == "process" means full pipeline needed
```

- [ ] **Step 2: Define `_check_task_skip_or_recover()` above `process_srt_files`**

Insert before `def process_srt_files`:

```python
def _check_task_skip_or_recover(subtitle_name, output_folder_zh, output_audio_file,
                                 srt_path, log):
    """
    判断任务完成状态，返回 "skip" 或 "process"。

    三级判断：
      1. 中配文件夹下已有视频 → 任务完成，返回 "skip"
      2. 无视频但已有合并好的 WAV（且 WAV 健康）→ 合成视频，返回 "skip"
      3. 无视频也无 WAV → 返回 "process"，开始完整流程
    """
    target_video_output = os.path.join(output_folder_zh, f"{subtitle_name}.mp4")

    # ── 第 1 级：视频已存在 ──
    if os.path.exists(target_video_output):
        log(f"  ✅ [完成] 最终视频已存在: {os.path.basename(target_video_output)}，跳过。")
        return "skip"

    # ── 第 2 级：WAV 存在 → 恢复合并 ──
    if os.path.exists(output_audio_file):
        wav_size = os.path.getsize(output_audio_file)
        if wav_size < 1000:
            log(f"  ⚠️ [恢复] WAV 文件异常 ({wav_size} bytes)，视为无效，重新生成。")
            return "process"

        # 额外验证：确保 WAV 可被 pydub 读取
        try:
            test_audio = AudioSegment.from_file(output_audio_file)
            if len(test_audio) < 100:  # < 0.1 秒，视为无效占位
                log(f"  ⚠️ [恢复] WAV 时长异常 ({len(test_audio)}ms)，重新生成。")
                return "process"
        except Exception:
            log(f"  ⚠️ [恢复] WAV 文件损坏无法读取，重新生成。")
            return "process"

        log(f"  🔄 [恢复] 已找到配音 WAV ({wav_size / 1024:.0f} KB)，跳过生成，直接合成视频…")
        _find_and_merge_video(subtitle_name, output_audio_file, output_folder_zh, srt_path, log)
        return "skip"

    # ── 第 3 级：无视频无 WAV，开始全新流程 ──
    log(f"  🆕 [新建] 未检测到任何产物，开始完整流程。")
    return "process"
```

- [ ] **Step 3: Update `_find_and_merge_video` signature to accept explicit parameters**

The current `_find_and_merge_video` is defined inside `process_srt_files` and captures `output_folder_zh`, `srt_path`, and `log` from closure. Extract it to module level so the new helper can call it too.

Replace the inner function (lines 236-252) with a module-level function placed before `_check_task_skip_or_recover`:

```python
def _find_and_merge_video(base_name_no_ext, audio_path, output_folder_zh, srt_path, log):
    """查找源视频并与配音 WAV 合并，输出到中配文件夹。"""
    target_video_output = os.path.join(output_folder_zh, f"{base_name_no_ext}.mp4")
    if os.path.exists(target_video_output):
        log(f"  -> 最终视频已存在: {os.path.basename(target_video_output)}，无需重复合并。")
        return

    video_extensions = {".mp4", ".ts", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm"}
    found_video_path = None
    for ext in video_extensions:
        potential_path = os.path.join(srt_path, f"{base_name_no_ext}{ext}")
        if os.path.exists(potential_path):
            found_video_path = potential_path
            break

    if found_video_path:
        log(f"--- 准备合并音视频 ---")
        try:
            if merge_single_audio_video(found_video_path, audio_path, target_video_output):
                log(f"视频合并成功: {os.path.basename(target_video_output)}")
            else:
                log(f"!! 视频合并失败，请检查上方 ffmpeg 错误信息。")
        except Exception as e:
            log(f"!! 合并失败: {e}")
    else:
        log(f"!! 警告: 未能为 '{base_name_no_ext}' 找到匹配的视频文件，跳过合并。")
```

- [ ] **Step 4: Remove the old inner `_find_video_path` and `_find_and_merge_video` from `process_srt_files`**

Delete the inner function `_find_video_path` (lines 228-234) and the inner function `_find_and_merge_video` (lines 236-252) from inside `process_srt_files`, since they're now module-level.

Also remove the now-unused `output_folder_zh` and `srt_path` closure dependencies from the remaining inner functions that used them — actually `output_folder_zh` and `srt_path` are still used elsewhere in `process_srt_files`, so keep those as local variables.

- [ ] **Step 5: Verify the code runs without syntax errors**

Run: `python -c "import main; print('Import OK')"` from the project directory.

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "refactor: extract task recovery check into helper with WAV validation"
```

---

### Task 2: Extract `_find_video_path` to module level

**Files:**
- Modify: `D:\Users\xuan\Mycode\MultTTS\main.py`

- [ ] **Step 1: Move `_find_video_path` from inner function to module level**

Delete the inner function at lines 228-234 of `process_srt_files`:
```python
    def _find_video_path(base_name_no_ext):
        video_extensions = {".mp4", ".ts", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm"}
        for ext in video_extensions:
            potential_path = os.path.join(srt_path, f"{base_name_no_ext}{ext}")
            if os.path.exists(potential_path):
                return potential_path
        return None
```

And add it as a module-level function before `_check_task_skip_or_recover`:
```python
def _find_video_path(base_name_no_ext, srt_path):
    video_extensions = {".mp4", ".ts", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm"}
    for ext in video_extensions:
        potential_path = os.path.join(srt_path, f"{base_name_no_ext}{ext}")
        if os.path.exists(potential_path):
            return potential_path
    return None
```

- [ ] **Step 2: Update all call sites**

The old call `_find_video_path(subtitle_name)` (line 284) becomes `_find_video_path(subtitle_name, srt_path)`.

The `_find_and_merge_video` module-level function already inlines the search, but for consistency update it to call `_find_video_path`:
```python
def _find_and_merge_video(base_name_no_ext, audio_path, output_folder_zh, srt_path, log):
    target_video_output = os.path.join(output_folder_zh, f"{base_name_no_ext}.mp4")
    if os.path.exists(target_video_output):
        log(f"  -> 最终视频已存在: {os.path.basename(target_video_output)}，无需重复合并。")
        return
    found_video_path = _find_video_path(base_name_no_ext, srt_path)
    ...
```

- [ ] **Step 3: Verify syntax**

Run: `python -c "import main; print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "refactor: extract _find_video_path to module level"
```

---

### Task 3: Add reference audio cleanup for recovered tasks

**Files:**
- Modify: `D:\Users\xuan\Mycode\MultTTS\main.py`

- [ ] **Step 1: Clean up stale `ref_audio_path` entries for recovered tasks**

In the recovery path (Case 2), the `main_audio_path` at `REF_AUDIO_PATH/<name>.wav` from a previous run might still exist. Add cleanup after successful merge in `_check_task_skip_or_recover`.

Update the recovery section in `_check_task_skip_or_recover`:

```python
        log(f"  🔄 [恢复] 已找到配音 WAV ({wav_size / 1024:.0f} KB)，跳过生成，直接合成视频…")
        _find_and_merge_video(subtitle_name, output_audio_file, output_folder_zh, srt_path, log)

        # 清理可能残留的参考音频（来自上一次未完成运行）
        ref_audio_dir = os.path.join(srt_path, "REF_AUDIO_PATH")
        stale_ref = os.path.join(ref_audio_dir, f"{subtitle_name}.wav")
        if os.path.exists(stale_ref):
            try:
                os.remove(stale_ref)
            except OSError:
                pass

        return "skip"
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import main; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "fix: clean up stale reference audio after recovery merge"
```
