# Background Merge Toggle Design

Date: 2026-07-05
Status: Approved

## Problem

The background finalize queue (see `2026-06-27-background-finalize-design.md`) always runs audio/video merge in a background thread. This is optimal for fast disks but can cause I/O contention on slower storage. Users need a way to control whether the merge step runs asynchronously (background) or synchronously (main thread).

## Goal

Add a boolean setting `background_merge` to the Settings dialog that controls whether the finalize job (WAV merge + MP4 muxing) runs in a background thread or inline in the main processing loop. Default: **off** (synchronous).

## Behavior

| `background_merge` | Finalize thread | Main loop | Use case |
|---|---|---|---|
| `True` | `finalize_executor` background thread | Does not wait, starts next file immediately | Fast disk, maximize throughput |
| `False` (default) | Main processing thread | Blocks until merge completes before starting next file | Slow disk, reduce I/O pressure |

## Changes

### 1. Settings storage (`gui.pyw`)

`DEFAULT_SETTINGS` add:

```python
"background_merge": False,
```

### 2. Settings dialog (`gui.pyw` — `SettingsDialog`)

- Add `QCheckBox` parameter `background_merge: bool = False`
- Label: "后台合并音视频"
- Tooltip: "开启后，配音完成的文件将在后台线程合并音频和视频，主流程不等待直接处理下一个文件。关闭时，合并在主线程同步执行。"
- Place after the merge_max_chars spinbox in the form layout
- Expose via `self.chk_bg_merge.isChecked()` property

### 3. Main window (`gui.pyw` — `TTSApp`)

- `open_settings`: pass `background_merge` to dialog, read back on accept
- `_refresh_summary_labels`: add summary label (e.g. "后台合并: 关")
- `start_processing`: pass `background_merge` to `WorkerThread`
- Save to `app_settings.json` via existing `_save_current_settings`

### 4. Worker thread (`gui.pyw` — `WorkerThread`)

- Add `background_merge: bool = False` constructor parameter
- Pass to `process_srt_files`

### 5. Processing logic (`main.py` — `process_srt_files`)

- Add `background_merge: bool = False` parameter
- When `background_merge=True`: keep current behavior — `_submit_finalize_job` to `finalize_executor`
- When `background_merge=False`: call `_finalize_audio_video_job` directly (synchronously), log the blocking wait

Pseudocode for the conditional:

```python
if background_merge:
    _submit_finalize_job(...)  # async, existing behavior
    finalize_submitted = True
else:
    log(f"--- [{subtitle_name}] 同步合并音频/视频... ---")
    ok = _finalize_audio_video_job(...)  # blocking
    if not ok:
        log(f"!! [{subtitle_name}] 同步合并失败")
```

### 6. Summary label

Add `self.lbl_bg_merge_summary` to the summary row in `initUI`, showing "后台合并: 开" or "后台合并: 关".

## Non-Goals

- Do not add per-file granularity (the toggle is global).
- Do not change the finalize executor's `max_workers=1` behavior when background mode is on.
- Do not affect the recovery/skip logic in `_check_task_skip_or_recover`.

## Testing

- Toggle off (default): run a two-file job, verify File B TTS does NOT start until File A merge completes (check log timestamps).
- Toggle on: run a two-file job, verify File B TTS starts while File A merge is still running.
- Verify the setting persists across app restarts (`app_settings.json`).
