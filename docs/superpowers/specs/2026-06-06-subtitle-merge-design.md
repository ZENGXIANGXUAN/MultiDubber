# 字幕合并功能设计文档

**日期**: 2026-06-06
**状态**: 已批准

---

## 1. 概述

在字幕预处理阶段，将时间戳连续的相邻字幕合并为单条字幕，减少 TTS API 调用次数，提高处理效率。

### 核心规则

- 两条字幕的时间戳必须**严格连续**（前一条的 end_time == 后一条的 start_time，字符串精确比较）
- 合并后的文本总字符数**不得超过**配置的最大字符数阈值（默认 30）
- 支持**链式合并**：A+B 合并后，(A+B)+C 继续判断
- `max_chars <= 0` 时禁用合并功能

---

## 2. 数据结构

每条字幕为一个列表：`[start_time, end_time, text, english_text]`

| 索引 | 字段 | 类型 | 说明 |
|------|------|------|------|
| 0 | start_time | str | 开始时间，格式 `HH:MM:SS,mmm` |
| 1 | end_time | str | 结束时间，格式 `HH:MM:SS,mmm` |
| 2 | text | str | 字幕文本 |
| 3 | english_text | str | 英文文本（可为空） |

---

## 3. 方案选择

**方案 A：放在 `subtitle_parser.py`**（已批准）

- 合并本质是字幕数据变换，与 `parse_subtitles` 同属一个职责域
- 改动最小，语义清晰，不需要新文件

---

## 4. 详细设计

### 4.1 核心函数

**位置**: `subtitle_parser.py`

```python
def merge_contiguous_subtitles(subtitles: list[list[str]], max_chars: int = 30) -> list[list[str]]:
    """
    合并时间戳连续的相邻字幕。

    Args:
        subtitles: 字幕列表，每条格式 [start_time, end_time, text, english_text]
        max_chars: 合并后文本最大字符数，0 表示禁用合并

    Returns:
        合并后的字幕列表
    """
    if max_chars <= 0 or len(subtitles) <= 1:
        return subtitles

    merged = [subtitles[0][:]]  # 深拷贝第一条

    for i in range(1, len(subtitles)):
        prev = merged[-1]
        curr = subtitles[i]

        # 条件1：时间戳严格连续（字符串比较）
        is_contiguous = prev[1].strip() == curr[0].strip()

        # 条件2：合并后字数不超限
        combined_text = prev[2] + curr[2]
        is_within_limit = len(combined_text) <= max_chars

        if is_contiguous and is_within_limit:
            prev[1] = curr[1]           # 更新结束时间
            prev[2] = combined_text     # 合并文本
            prev[3] = prev[3] + curr[3] # 合并英文
        else:
            merged.append(curr[:])      # 深拷贝添加

    return merged
```

### 4.2 配置参数

**位置**: `config.py`

```python
MERGE_MAX_CHARS = 30  # 合并后文本最大字符数，0 表示禁用合并
```

### 4.3 GUI 集成

**位置**: `gui.pyw` — `SettingsDialog`

- 在 `SettingsDialog.__init__` 中添加 `QSpinBox`，范围 0-200，默认 30
- Label: "合并字数上限:"
- Tooltip: "合并后文本的最大字符数，设为 0 禁用合并"
- 放在 "Line Index:" 下方

**持久化**:

- `DEFAULT_SETTINGS` 新增 `"merge_max_chars": 30`
- `open_settings()` 传入 `merge_max_chars` 参数
- 对话框确认后写入 `config.MERGE_MAX_CHARS` 和 `self._settings`
- `_save_current_settings()` 保存到 `app_settings.json`
- `_refresh_summary_labels()` 显示当前值

### 4.4 流水线集成

**位置**: `main.py` — `process_srt_files()` 函数

在 `parse_subtitles()` 调用之后、TTS 生成循环之前插入：

```python
parsed_subtitles = parse_subtitles(file_content, transformers_line)

# 合并连续字幕
original_count = len(parsed_subtitles)
parsed_subtitles = merge_contiguous_subtitles(parsed_subtitles, config.MERGE_MAX_CHARS)
merged_count = len(parsed_subtitles)
if merged_count < original_count:
    log(f"  字幕合并: {original_count} 条 → {merged_count} 条")
```

---

## 5. 对下游的影响分析

| 模块 | 影响 | 说明 |
|------|------|------|
| 进度条 | 无 | `total_tasks` 使用合并后的列表长度，天然适配 |
| 状态恢复 | 无 | `status.json` 基于合并后的索引，与字幕一一对应 |
| `merge_audio()` | 无 | 遍历 `parsed_subtitles`，索引自然连续 |
| 参考音频裁剪 | 正向 | `crop_audio()` 使用合并后的 start/end_time，覆盖更大时间范围 |
| `dispatcher.py` | 无 | 以 `parsed_subtitles` 为输入，上游已合并 |
| `audio_processor.py` | 无需修改 | 所有函数以合并后的数据为输入 |

**不需要修改的文件**: `audio_processor.py`、`dispatcher.py`、`api_client.py`

---

## 6. 边界情况

| 场景 | 处理 |
|------|------|
| 输入为空列表 | 直接返回空列表 |
| 输入只有 1 条 | 直接返回原列表 |
| `max_chars` 设为 0 或负数 | 禁用合并，返回原列表 |
| 时间戳不连续 | 不合并，作为独立条目 |
| 合并后超过字数限制 | 不合并，作为独立条目 |
| 某条字幕 text 为空 | 正常参与合并逻辑 |
| 所有字幕都可合并 | 全部合并为 1 条 |

---

## 7. 日志

仅记录合并摘要：

```
字幕合并: 120 条 → 85 条
```

不记录每次合并的详情，避免日志膨胀。

---

## 8. 测试用例

### 基本合并

```python
input_data = [
    ["00:00:01,000", "00:00:03,000", "你好，", ""],
    ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
    ["00:00:06,000", "00:00:08,000", "再见。", ""]
]
expected = [
    ["00:00:01,000", "00:00:05,000", "你好，欢迎来到。", ""],
    ["00:00:06,000", "00:00:08,000", "再见。", ""]
]
```

### 超过字数限制（不合并）

```python
input_data = [
    ["00:00:01,000", "00:00:03,000", "这是一段比较长的字幕内容，", ""],
    ["00:00:03,000", "00:00:05,000", "再加上这一段就会超过限制了。", ""]
]
# max_chars=30，不合并
```

### 链式合并

```python
input_data = [
    ["00:00:01,000", "00:00:02,000", "你", ""],
    ["00:00:02,000", "00:00:03,000", "好", ""],
    ["00:00:03,000", "00:00:04,000", "呀", ""],
    ["00:00:05,000", "00:00:06,000", "再见", ""]
]
expected = [
    ["00:00:01,000", "00:00:04,000", "你好呀", ""],
    ["00:00:05,000", "00:00:06,000", "再见", ""]
]
```

### 禁用合并

```python
# max_chars=0 时，返回原列表不变
```

---

## 9. 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `subtitle_parser.py` | 修改 | 新增 `merge_contiguous_subtitles()` 函数 |
| `config.py` | 修改 | 新增 `MERGE_MAX_CHARS = 30` |
| `main.py` | 修改 | 在 `parse_subtitles()` 后调用合并函数 |
| `gui.pyw` | 修改 | SettingsDialog 新增 SpinBox，持久化配置 |

---

## 10. 性能

- **时间复杂度**: O(N)，单次遍历
- **空间复杂度**: O(N)，存储合并结果
- **适用场景**: 万级字幕条目无性能问题
