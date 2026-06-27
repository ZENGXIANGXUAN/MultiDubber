# Background Finalize Queue Design

Date: 2026-06-27
Status: Approved for planning

## Problem

After one subtitle file finishes TTS segment generation, `process_srt_files` currently performs the final artifact work synchronously:

1. inspect generated segment WAV files
2. merge all segments into the final WAV
3. export the WAV
4. clear recovery status
5. train the duration predictor
6. merge WAV with the source video through ffmpeg
7. clean the temp directory

For large files this can take around 9 minutes. These steps do not need the TTS servers or GPU/API workers, but because they run inline they delay the next dubbing task from starting.

## Goal

Let the next file start TTS generation as soon as the current file's segment generation is complete. Move final WAV merge and MP4 muxing into a background finalize queue while keeping user-visible logs and final completion semantics accurate.

## Recommended Approach

Use a single-worker background finalize queue.

The main processing loop submits one finalize job per completed subtitle file, then immediately advances to the next file. A background `ThreadPoolExecutor(max_workers=1)` runs finalization jobs sequentially:

1. log finalize start
2. inspect segment files
3. merge segment audio
4. export final WAV
5. clear the per-file status file
6. train `duration_predictor`
7. find and merge the matching source video
8. clean temp files after successful finalization
9. log finalize success or failure

Single-worker finalization is preferred over multi-worker finalization because pydub export and ffmpeg are CPU, disk, and memory heavy. Serializing this work gives most of the scheduling benefit without letting several ffmpeg jobs compete with TTS post-processing.

## Architecture

Add a small finalization helper in `main.py`:

- `_finalize_file_outputs(...)`
  - owns the current synchronous merge/export/video merge logic
  - returns success/failure information
  - logs every major step through the existing `log` callback

- finalize executor lifecycle inside `process_srt_files`
  - created before the file loop
  - stores submitted futures in `finalize_futures`
  - waits for all futures after the file loop finishes
  - logs any exceptions from futures
  - shuts down before the worker emits final completion

The existing `_find_and_merge_video(...)` remains the MP4 muxing primitive. The current merge code becomes the body of `_finalize_file_outputs(...)`, so behavior stays familiar and scoped.

## Data Flow

Current flow:

```text
File A TTS segments -> merge WAV -> merge MP4 -> cleanup -> File B TTS starts
```

New flow:

```text
File A TTS segments -> submit finalize(A) -> File B TTS starts
                         |
                         v
                 background: merge WAV -> merge MP4 -> cleanup
```

At the end:

```text
All TTS work submitted/done -> wait for finalize queue -> final UI completion signal
```

## UI Logging

Finalize job logs must appear in the existing UI log.

The background thread must not touch Qt widgets directly. It will call the same `log(...)` function already passed through `process_srt_files`. In GUI mode this reaches `ProgressAdapter.log_signal`, which is a Qt signal and is safe to emit from worker/background threads.

Required log messages:

- background finalize submitted for each file
- audio merge started
- audio merge output path
- video merge started
- video merge success/failure
- finalize failed with exception details
- waiting for background finalize jobs
- all background finalize jobs finished

The final "all tasks completed" UI notification must only fire after the finalize queue has drained. TTS completion alone is not enough.

## Recovery And Cleanup

Temp directory cleanup moves from the per-file `finally` block into finalization success handling for normal runs.

Rules:

- If finalization succeeds, clear status and remove the current temp directory.
- If finalization fails, keep temp and status artifacts for recovery.
- If the user aborts, keep the current conservative behavior: cleanup only where it is already safe, and avoid deleting artifacts needed to resume.
- Existing recovery behavior for already-created WAV files remains valid. `_check_task_skip_or_recover(...)` can still find a WAV and merge video if needed.

## Error Handling

Finalize errors must not crash TTS scheduling for following files. A failed finalize job logs its failure, returns a failed result, and preserves temp/status files.

After all files have been processed, the main thread waits for every finalize future. If any finalization failed, the overall run should be reported as not fully completed so the GUI does not show a false success notification.

## Progress Semantics

Existing file and task progress bars can continue to represent TTS scheduling/generation progress.

The log becomes the source of truth for background finalization progress. No new progress bar is required for the first implementation.

## Testing

Focused verification:

- Unit or narrow integration test for `_finalize_file_outputs(...)` with video merge mocked or skipped.
- Test that temp cleanup happens only after successful finalization.
- Test that finalization failure preserves temp/status artifacts.
- Manual GUI run with at least two input files:
  - File B TTS starts while File A finalize logs are still appearing.
  - UI log receives background merge success/failure messages.
  - final completion notification appears only after background finalization is complete.

## Non-Goals

- Do not parallelize multiple ffmpeg merges in the first version.
- Do not change the TTS dispatch algorithm.
- Do not add new UI controls yet.
- Do not remove existing recovery behavior.
