# 字幕合并功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在字幕预处理阶段合并时间戳连续的相邻字幕，减少 TTS API 调用次数。

**Architecture:** 在 `subtitle_parser.py` 新增 `merge_contiguous_subtitles()` 函数，在 `main.py` 的 `parse_subtitles()` 之后调用。`config.py` 新增 `MERGE_MAX_CHARS` 参数，`gui.pyw` 的 SettingsDialog 中暴露配置。

**Tech Stack:** Python 3.12, PyQt6, pytest

---

## 文件结构

| 文件 | 变更类型 | 职责 |
|------|----------|------|
| `tests/test_subtitle_merge.py` | 新建 | 合并函数的单元测试 |
| `subtitle_parser.py:31` | 修改 | 新增 `merge_contiguous_subtitles()` |
| `config.py:41` | 修改 | 新增 `MERGE_MAX_CHARS = 30` |
| `main.py:409-414` | 修改 | 调用合并函数并记录日志 |
| `gui.pyw:27-37` | 修改 | DEFAULT_SETTINGS 新增 key |
| `gui.pyw:114-121` | 修改 | SettingsDialog 构造函数新增参数 |
| `gui.pyw:135-141` | 修改 | 新增 SpinBox 控件 |
| `gui.pyw:646-678` | 修改 | open_settings 读写新参数 |
| `gui.pyw:680-690` | 修改 | _refresh_summary_labels 显示新值 |

---

### Task 1: 写合并函数的单元测试（TDD）

**Files:**
- Create: `tests/test_subtitle_merge.py`

- [ ] **Step 1: 创建 tests 目录和测试文件**

```bash
mkdir -p D:\Users\xuan\Mycode\MultTTS\tests
```

- [ ] **Step 2: 编写全部测试用例**

```python
# tests/test_subtitle_merge.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subtitle_parser import merge_contiguous_subtitles


class TestMergeContiguousSubtitles:
    """merge_contiguous_subtitles 的单元测试"""

    def test_basic_merge(self):
        """连续字幕在字数限制内应合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
            ["00:00:06,000", "00:00:08,000", "再见。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2
        assert result[0] == ["00:00:01,000", "00:00:05,000", "你好，欢迎来到。", ""]
        assert result[1] == ["00:00:06,000", "00:00:08,000", "再见。", ""]

    def test_no_merge_non_contiguous(self):
        """时间戳不连续的字幕不应合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:04,000", "00:00:06,000", "世界。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2

    def test_no_merge_exceeds_max_chars(self):
        """合并后超过字数限制的不应合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "这是一段比较长的字幕内容，", ""],
            ["00:00:03,000", "00:00:05,000", "再加上这一段就会超过限制了。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2

    def test_chain_merge(self):
        """支持链式合并：A+B 合并后继续判断 (A+B)+C"""
        input_data = [
            ["00:00:01,000", "00:00:02,000", "你", ""],
            ["00:00:02,000", "00:00:03,000", "好", ""],
            ["00:00:03,000", "00:00:04,000", "呀", ""],
            ["00:00:05,000", "00:00:06,000", "再见", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2
        assert result[0] == ["00:00:01,000", "00:00:04,000", "你好呀", ""]
        assert result[1] == ["00:00:05,000", "00:00:06,000", "再见", ""]

    def test_disabled_merge_zero(self):
        """max_chars=0 时禁用合并，返回原列表"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=0)
        assert len(result) == 2
        assert result == input_data

    def test_disabled_merge_negative(self):
        """max_chars 为负数时禁用合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=-1)
        assert result == input_data

    def test_empty_list(self):
        """空列表应返回空列表"""
        result = merge_contiguous_subtitles([], max_chars=30)
        assert result == []

    def test_single_item(self):
        """单条字幕应原样返回"""
        input_data = [["00:00:01,000", "00:00:03,000", "你好", ""]]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert result == input_data

    def test_all_mergeable(self):
        """所有字幕都可合并时，合并为 1 条"""
        input_data = [
            ["00:00:01,000", "00:00:02,000", "你", ""],
            ["00:00:02,000", "00:00:03,000", "好", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1
        assert result[0] == ["00:00:01,000", "00:00:03,000", "你好", ""]

    def test_english_text_merged(self):
        """英文文本应随中文一起合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好", "Hello"],
            ["00:00:03,000", "00:00:05,000", "世界", "World"],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1
        assert result[0][3] == "HelloWorld"

    def test_does_not_mutate_input(self):
        """不应修改原始输入数据"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
        ]
        original_end_time = input_data[0][1]
        original_text = input_data[0][2]
        merge_contiguous_subtitles(input_data, max_chars=30)
        assert input_data[0][1] == original_end_time
        assert input_data[0][2] == original_text

    def test_whitespace_in_timestamps(self):
        """时间戳前后有空格时应能正确比较"""
        input_data = [
            ["00:00:01,000", " 00:00:03,000 ", "你好，", ""],
            [" 00:00:03,000 ", "00:00:05,000", "欢迎来到。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1

    def test_empty_text_subtitle(self):
        """text 为空的字幕应正常参与合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "", ""],
            ["00:00:03,000", "00:00:05,000", "内容", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1
        assert result[0][2] == "内容"
```

- [ ] **Step 3: 运行测试确认全部失败**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/test_subtitle_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_contiguous_subtitles'`

- [ ] **Step 4: 提交测试文件**

```bash
git add tests/test_subtitle_merge.py
git commit -m "test: add unit tests for merge_contiguous_subtitles (TDD red)"
```

---

### Task 2: 实现合并函数

**Files:**
- Modify: `subtitle_parser.py:30` (在文件末尾添加)

- [ ] **Step 1: 在 subtitle_parser.py 末尾添加函数**

在 `subtitle_parser.py` 第 29 行（`return result` 之后）添加：

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

- [ ] **Step 2: 运行测试确认全部通过**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/test_subtitle_merge.py -v`
Expected: 12 passed

- [ ] **Step 3: 提交实现**

```bash
git add subtitle_parser.py
git commit -m "feat: add merge_contiguous_subtitles function (TDD green)"
```

---

### Task 3: 添加配置参数

**Files:**
- Modify: `config.py:40` (在文件末尾 `ABORT_ALL` 之后添加)

- [ ] **Step 1: 在 config.py 末尾添加配置**

在 `config.py` 第 40 行 `ABORT_ALL = False` 之后添加：

```python

# === 字幕合并配置 ===
MERGE_MAX_CHARS = 30  # 合并后文本最大字符数，0 表示禁用合并
```

- [ ] **Step 2: 验证配置可导入**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -c "from config import MERGE_MAX_CHARS; print(MERGE_MAX_CHARS)"`
Expected: `30`

- [ ] **Step 3: 提交**

```bash
git add config.py
git commit -m "feat: add MERGE_MAX_CHARS config parameter"
```

---

### Task 4: 集成到主处理流水线

**Files:**
- Modify: `main.py:20` (import)
- Modify: `main.py:409-414` (调用点)

- [ ] **Step 1: 添加 import**

在 `main.py` 第 20 行，修改 import 语句：

```python
from subtitle_parser import parse_subtitles, merge_contiguous_subtitles
```

（原来是 `from subtitle_parser import parse_subtitles`）

- [ ] **Step 2: 在 parse_subtitles 之后插入合并调用**

在 `main.py` 第 409-410 行之间插入合并逻辑。当前代码：

```python
        parsed_subtitles = parse_subtitles(file_content, transformers_line)
        if not parsed_subtitles:
```

改为：

```python
        parsed_subtitles = parse_subtitles(file_content, transformers_line)
        if not parsed_subtitles:
            log(f"无有效字幕，跳过。")
            if audio_extracted_in_this_run and os.path.exists(main_audio_path): os.remove(main_audio_path)
            global_file_idx += 1
            continue

        # 合并连续字幕，减少 API 调用次数
        original_count = len(parsed_subtitles)
        parsed_subtitles = merge_contiguous_subtitles(parsed_subtitles, config.MERGE_MAX_CHARS)
        merged_count = len(parsed_subtitles)
        if merged_count < original_count:
            log(f"  字幕合并: {original_count} 条 → {merged_count} 条")
```

注意：原第 410-414 行的 `if not parsed_subtitles:` 块保持不变，只是在它之后插入合并代码。

- [ ] **Step 3: 验证语法正确**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -c "import main"`
Expected: 无报错

- [ ] **Step 4: 提交**

```bash
git add main.py
git commit -m "feat: integrate subtitle merge into processing pipeline"
```

---

### Task 5: GUI 配置集成

**Files:**
- Modify: `gui.pyw:27-37` (DEFAULT_SETTINGS)
- Modify: `gui.pyw:114-121` (SettingsDialog.__init__ 签名)
- Modify: `gui.pyw:135-141` (SpinBox 添加位置)
- Modify: `gui.pyw:646-655` (open_settings 调用)
- Modify: `gui.pyw:659-668` (open_settings 保存)
- Modify: `gui.pyw:673-678` (日志输出)
- Modify: `gui.pyw:680-690` (_refresh_summary_labels)

- [ ] **Step 1: DEFAULT_SETTINGS 新增 key**

在 `gui.pyw` 第 36 行 `"api_hostid": ""` 之后添加：

```python
DEFAULT_SETTINGS = {
    "srt_paths": [],
    "line_index": 2,
    "total_threads": 6,
    "max_retries": 3,
    "webhook_url": "https://sctapi.ftqq.com/SCT124090TODYAymp8nuHDeqleLu8oRDAS.send",
    "servers": [],
    "api_public_key": "",
    "api_private_key": "",
    "api_hostid": "",
    "merge_max_chars": 30,  # 字幕合并字数上限，0=禁用
}
```

- [ ] **Step 2: SettingsDialog 构造函数新增参数**

`gui.pyw` 第 114-121 行，构造函数签名改为：

```python
class SettingsDialog(QDialog):
    def __init__(self, parent=None,
                 line_index: int = 2,
                 total_threads: int = 6,
                 max_retries: int = 3,
                 webhook_url: str = "",
                 api_public_key: str = "",
                 api_private_key: str = "",
                 api_hostid: str = "",
                 merge_max_chars: int = 30):
```

- [ ] **Step 3: 在 Line Index SpinBox 之后添加合并 SpinBox**

在 `gui.pyw` 第 141 行 `form.addRow("Line Index:", self.spin_line)` 之后，第 143 行 `# Total Threads` 之前，插入：

```python
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
```

- [ ] **Step 4: 添加 merge_max_chars 属性**

在 `gui.pyw` 第 247 行（`api_hostid` property 之后）添加：

```python
    @property
    def merge_max_chars(self) -> int:
        return self.spin_merge_chars.value()
```

- [ ] **Step 5: 修改 open_settings 调用**

`gui.pyw` 第 647-656 行改为：

```python
    def open_settings(self):
        dlg = SettingsDialog(
            parent=self,
            line_index=self._settings["line_index"],
            total_threads=self._settings["total_threads"],
            max_retries=self._settings.get("max_retries", 3),
            webhook_url=self._settings.get("webhook_url", ""),
            api_public_key=self._settings.get("api_public_key", ""),
            api_private_key=self._settings.get("api_private_key", ""),
            api_hostid=self._settings.get("api_hostid", ""),
            merge_max_chars=self._settings.get("merge_max_chars", 30),
        )
```

- [ ] **Step 6: 修改 open_settings 保存逻辑**

`gui.pyw` 第 659-668 行改为：

```python
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._settings["line_index"] = dlg.line_index
            self._settings["total_threads"] = dlg.total_threads
            self._settings["max_retries"] = dlg.max_retries
            self._settings["webhook_url"] = dlg.webhook_url

            # 接收新参数并保存
            self._settings["api_public_key"] = dlg.api_public_key
            self._settings["api_private_key"] = dlg.api_private_key
            self._settings["api_hostid"] = dlg.api_hostid
            self._settings["merge_max_chars"] = dlg.merge_max_chars

            # 同步到 config 模块
            config.MERGE_MAX_CHARS = dlg.merge_max_chars
```

- [ ] **Step 7: 修改日志输出**

`gui.pyw` 第 673-678 行改为：

```python
            self.append_log(
                f"> Settings saved — Line Index: {dlg.line_index}, "
                f"总线程数: {dlg.total_threads}, "
                f"最大重试: {dlg.max_retries}, "
                f"合并字数上限: {dlg.merge_max_chars}, "
                f"Webhook: {'已设置' if dlg.webhook_url else '未设置'}"
            )
```

- [ ] **Step 8: 修改 _refresh_summary_labels**

`gui.pyw` 第 680-690 行改为：

```python
    def _refresh_summary_labels(self):
        self.lbl_line_summary.setText(f"Line Index: {self._settings['line_index']}")
        self.lbl_thread_summary.setText(f"总线程数: {self._settings['total_threads']}")
        self.lbl_retry_summary.setText(f"最大重试: {self._settings.get('max_retries', 3)}")
        self.lbl_merge_summary.setText(f"合并字数: {self._settings.get('merge_max_chars', 30)}")
        wh = self._settings.get("webhook_url", "")
        if wh:
            short = wh.split("//")[-1].split("/")[0]
            self.lbl_webhook_summary.setText(f"Webhook: {short}/…")
        else:
            self.lbl_webhook_summary.setText("Webhook: 未设置")
```

- [ ] **Step 9: 在 summary_layout 中添加 merge label**

在 `gui.pyw` 第 529 行附近，找到 `self.lbl_retry_summary` 的定义和添加位置。

在 `self.lbl_retry_summary = QLabel(...)` 之后添加：

```python
        self.lbl_merge_summary = QLabel(f"合并字数: {self._settings.get('merge_max_chars', 30)}")
```

在 `summary_layout.addWidget(self.lbl_retry_summary)` 之后添加：

```python
        summary_layout.addSpacing(20)
        summary_layout.addWidget(self.lbl_merge_summary)
```

- [ ] **Step 10: 验证 GUI 可启动**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -c "import gui"`
Expected: 无报错

- [ ] **Step 11: 提交**

```bash
git add gui.pyw
git commit -m "feat: add merge_max_chars to GUI settings dialog"
```

---

### Task 6: 运行全部测试并验证

- [ ] **Step 1: 运行单元测试**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/test_subtitle_merge.py -v`
Expected: 12 passed

- [ ] **Step 2: 验证模块导入链**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -c "from main import process_srt_files; from config import MERGE_MAX_CHARS; print('OK', MERGE_MAX_CHARS)"`
Expected: `OK 30`

- [ ] **Step 3: 提交最终状态**

```bash
git add -A
git commit -m "feat: complete subtitle merge feature — core + config + GUI + tests"
```
