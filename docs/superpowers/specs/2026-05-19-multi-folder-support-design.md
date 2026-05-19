# Multi-Folder Support Design

## Summary

Replace single-folder input with a folder list, allowing batch processing of SRT files
across multiple directories. Each folder is processed independently with its own output.

## Motivation

Current `process_srt_files` accepts a single `srt_path`. Users with multiple projects
must manually re-run the app for each folder. This adds a folder list so all projects
can be queued and processed in one batch.

## Design

### GUI (`gui.pyw`)

- Replace the single `QLineEdit` path input with a `QListWidget` folder list.
- Each item shows the folder path with a ✕ remove button (reuse the `ServerEntryWidget`
  pattern — create a lightweight `FolderEntryWidget` or use standard list items with
  an item widget).
- "Browse" button → opens `QFileDialog.getExistingDirectory()`, adds selected folder
  if not already in the list.
- Run validation: button enabled only when at least one folder exists AND at least one
  server is online.
- `load_settings` / `save_settings`: migrate `srt_path` (str) → `srt_paths` (list).
  On load, if old `srt_path` string is found, auto-convert to single-element list.

### Main processing (`main.py`)

- `process_srt_files` signature changes: `srt_path: str` → `srt_paths: List[str]`.
- Outer loop iterates over `srt_paths`. For each path:
  - List SRT files, process, output to `<path>/中配/`.
  - On abort, break out of both loops.
- `total_files` = sum of SRT files across all folders (reported once upfront).

### Config (`app_settings.json`)

- New key `srt_paths` (list of strings).
- Backward compat: if `srt_path` (string) exists and `srt_paths` doesn't, convert on
  load.

### Files Changed

| File | Change |
|------|--------|
| `gui.pyw` | Replace path input with folder list widget; update save/load |
| `main.py` | `process_srt_files` accepts list, loops over folders |
| `config.py` | No changes needed (paths are passed explicitly) |

## Non-Goals

- No recursive subdirectory scanning
- No merged output across folders (each folder produces its own `中配/`)
- No per-folder server/thread config (all folders share the same settings)
