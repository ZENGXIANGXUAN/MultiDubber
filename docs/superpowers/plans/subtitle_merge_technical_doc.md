# 字幕合并功能技术文档

## 1. 功能概述

**目的**：将时间戳连续的相邻字幕条目合并为单条字幕，减少TTS API调用次数，提高处理效率。

**核心规则**：
- 两条字幕的时间戳必须**完全连续**（前一条的结束时间 = 后一条的开始时间）
- 合并后的文本总字数**不得超过**配置的最大字符数阈值（如30字）
- 支持**链式合并**：A+B满足条件合并后，(A+B)+C继续判断是否可以合并

---

## 2. 输入/输出格式

### 2.1 字幕数据结构

每条字幕表示为一个数组/列表，包含以下字段：

```
索引  字段名        类型      说明
[0]   start_time    string    开始时间，格式 "HH:MM:SS,mmm"
[1]   end_time      string    结束时间，格式 "HH:MM:SS,mmm"
[2]   text          string    字幕文本内容
[3]   english_text  string    英文文本（可选，可为空字符串）
```

### 2.2 输入示例

```json
[
  ["00:00:01,000", "00:00:03,500", "你好，欢迎来到", ""],
  ["00:00:03,500", "00:00:06,000", "这个美丽的世界。", ""],
  ["00:00:08,000", "00:00:10,000", "今天天气不错。", ""]
]
```

### 2.3 输出示例

```json
[
  ["00:00:01,000", "00:00:06,000", "你好，欢迎来到这个美丽的世界。", ""],
  ["00:00:08,000", "00:00:10,000", "今天天气不错。", ""]
]
```

---

## 3. 配置参数

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `MERGE_MAX_CHARS` | int | 30 | 合并后文本的最大字符数。设为0表示禁用合并功能。 |

---

## 4. 算法逻辑

### 4.1 伪代码

```
function merge_contiguous_subtitles(subtitles, max_chars):
    // 如果禁用合并或字幕数量不足，直接返回
    if max_chars <= 0 or length(subtitles) <= 1:
        return subtitles

    // 初始化结果列表，深拷贝第一条字幕
    merged = [deep_copy(subtitles[0])]

    // 从第二条开始遍历
    for i from 1 to length(subtitles) - 1:
        prev = merged[last]        // 结果列表中的最后一条
        curr = subtitles[i]        // 当前待处理字幕

        // 条件1：检查时间戳是否连续
        is_contiguous = (prev.end_time == curr.start_time)

        // 条件2：检查合并后字数是否超限
        combined_text = prev.text + curr.text
        is_within_limit = (length(combined_text) <= max_chars)

        // 两个条件都满足则合并
        if is_contiguous AND is_within_limit:
            prev.end_time = curr.end_time          // 更新结束时间
            prev.text = combined_text              // 合并文本
            prev.english_text = prev.english_text + curr.english_text  // 合并英文
        else:
            merged.append(deep_copy(curr))         // 不满足条件，作为新条目

    return merged
```

### 4.2 流程图

```
开始
  │
  ▼
┌─────────────────────────┐
│ max_chars <= 0 ?        │──是──▶ 返回原列表（禁用合并）
└─────────────────────────┘
  │否
  ▼
┌─────────────────────────┐
│ 初始化 merged = [第一条] │
└─────────────────────────┘
  │
  ▼
┌─────────────────────────┐
│ 遍历剩余字幕 i = 1..N   │◀──────────────────────┐
└─────────────────────────┘                        │
  │                                                │
  ▼                                                │
┌─────────────────────────┐                        │
│ prev = merged最后一条    │                        │
│ curr = subtitles[i]     │                        │
└─────────────────────────┘                        │
  │                                                │
  ▼                                                │
┌─────────────────────────┐                        │
│ prev.end_time           │                        │
│   == curr.start_time ?  │──否──▶ 添加为新条目 ────┤
└─────────────────────────┘                        │
  │是                                              │
  ▼                                                │
┌─────────────────────────┐                        │
│ len(prev.text+curr.text)│                        │
│   <= max_chars ?        │──否──▶ 添加为新条目 ────┤
└─────────────────────────┘                        │
  │是                                              │
  ▼                                                │
┌─────────────────────────┐                        │
│ 合并到 prev:            │                        │
│ - end_time = curr.end   │                        │
│ - text = text + text    │────────────────────────┘
│ - english = eng + eng   │
└─────────────────────────┘
  │
  ▼（遍历结束）
返回 merged
```

---

## 5. 关键实现细节

### 5.1 时间戳比较规则

**必须精确匹配**，字符串直接比较：

```python
is_contiguous = prev_end_time.strip() == curr_start_time.strip()
```

**时间格式**：`HH:MM:SS,mmm`（SRT标准格式）

**连续示例**：
```
"00:00:03,500" == "00:00:03,500"  ✅ 连续
"00:00:03,500" == "00:00:03,501"  ❌ 不连续（差1ms）
"00:00:03,500" == "00:00:04,000"  ❌ 不连续
```

> **注意**：如果实际场景中存在微小时间差（如1-2ms），建议增加容差逻辑，如判断差值是否在10ms以内。

### 5.2 字符数计算

- 使用**字符长度**，不是字节长度
- 中文每个汉字计1个字符
- 英文每个字母计1个字符
- 空格和标点也计入

```python
len("你好，欢迎来到")  # 结果: 7
len("Hello World")    # 结果: 11
```

### 5.3 深拷贝要求

必须对字幕数据进行**深拷贝**，避免修改原数据：

```python
# Python示例
merged = [subtitles[0][:]]  # 列表切片实现浅拷贝（对于字符串字段等效于深拷贝）

# 或者使用copy模块
import copy
merged = [copy.deepcopy(subtitles[0])]
```

### 5.4 链式合并

算法天然支持链式合并，无需额外处理：

```
输入: A(连续), B(连续), C(不连续), D
第1步: merged = [A]
第2步: A+B 满足条件 → merged = [AB]
第3步: AB+C 满足条件 → merged = [ABC]
第4步: ABC+D 不满足 → merged = [ABC, D]
```

---

## 6. 边界情况处理

| 场景 | 处理方式 |
|------|----------|
| 输入为空列表 | 直接返回空列表 |
| 输入只有1条字幕 | 直接返回原列表 |
| `max_chars` 设为0或负数 | 禁用合并，返回原列表 |
| 时间戳不连续 | 不合并，作为独立条目 |
| 合并后超过字数限制 | 不合并，作为独立条目 |
| 某条字幕text为空 | 正常参与合并逻辑 |
| 所有字幕都可合并 | 全部合并为1条 |

---

## 7. 完整代码示例

### 7.1 Python实现

```python
def merge_contiguous_subtitles(subtitles: list, max_chars: int = 30) -> list:
    """
    合并时间戳连续的相邻字幕。

    Args:
        subtitles: 字幕列表，每条格式 [start_time, end_time, text, english_text]
        max_chars: 合并后文本最大字符数，0表示禁用合并

    Returns:
        合并后的字幕列表
    """
    if max_chars <= 0 or len(subtitles) <= 1:
        return subtitles

    merged = [subtitles[0][:]]  # 深拷贝第一条

    for i in range(1, len(subtitles)):
        prev = merged[-1]
        curr = subtitles[i]

        # 检查时间戳是否连续
        is_contiguous = prev[1].strip() == curr[0].strip()

        # 检查合并后字数是否超限
        combined_text = prev[2] + curr[2]
        is_within_limit = len(combined_text) <= max_chars

        if is_contiguous and is_within_limit:
            # 合并
            prev[1] = curr[1]           # 更新结束时间
            prev[2] = combined_text     # 合并文本
            prev[3] = prev[3] + curr[3] # 合并英文
        else:
            merged.append(curr[:])      # 添加为新条目

    return merged
```

### 7.2 JavaScript实现

```javascript
/**
 * 合并时间戳连续的相邻字幕
 * @param {Array} subtitles - 字幕数组，每条格式 [start_time, end_time, text, english_text]
 * @param {number} maxChars - 合并后文本最大字符数，0表示禁用合并
 * @returns {Array} 合并后的字幕数组
 */
function mergeContiguousSubtitles(subtitles, maxChars = 30) {
    if (maxChars <= 0 || subtitles.length <= 1) {
        return subtitles;
    }

    // 深拷贝第一条
    const merged = [[...subtitles[0]]];

    for (let i = 1; i < subtitles.length; i++) {
        const prev = merged[merged.length - 1];
        const curr = subtitles[i];

        // 检查时间戳是否连续
        const isContiguous = prev[1].trim() === curr[0].trim();

        // 检查合并后字数是否超限
        const combinedText = prev[2] + curr[2];
        const isWithinLimit = combinedText.length <= maxChars;

        if (isContiguous && isWithinLimit) {
            // 合并
            prev[1] = curr[1];
            prev[2] = combinedText;
            prev[3] = prev[3] + curr[3];
        } else {
            merged.push([...curr]);
        }
    }

    return merged;
}
```

### 7.3 Java实现

```java
import java.util.*;

public class SubtitleMerger {

    /**
     * 合并时间戳连续的相邻字幕
     * @param subtitles 字幕列表，每条格式 [start_time, end_time, text, english_text]
     * @param maxChars 合并后文本最大字符数，0表示禁用合并
     * @return 合并后的字幕列表
     */
    public static List<String[]> mergeContiguousSubtitles(List<String[]> subtitles, int maxChars) {
        if (maxChars <= 0 || subtitles.size() <= 1) {
            return new ArrayList<>(subtitles);
        }

        List<String[]> merged = new ArrayList<>();
        merged.add(subtitles.get(0).clone()); // 深拷贝第一条

        for (int i = 1; i < subtitles.size(); i++) {
            String[] prev = merged.get(merged.size() - 1);
            String[] curr = subtitles.get(i);

            // 检查时间戳是否连续
            boolean isContiguous = prev[1].trim().equals(curr[0].trim());

            // 检查合并后字数是否超限
            String combinedText = prev[2] + curr[2];
            boolean isWithinLimit = combinedText.length() <= maxChars;

            if (isContiguous && isWithinLimit) {
                // 合并
                prev[1] = curr[1];
                prev[2] = combinedText;
                prev[3] = prev[3] + curr[3];
            } else {
                merged.add(curr.clone());
            }
        }

        return merged;
    }
}
```

---

## 8. 测试用例

### 8.1 基本合并测试

```python
# 输入
input_subtitles = [
    ["00:00:01,000", "00:00:03,000", "你好，", ""],
    ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
    ["00:00:06,000", "00:00:08,000", "再见。", ""]
]

# 期望输出（max_chars=30）
expected = [
    ["00:00:01,000", "00:00:05,000", "你好，欢迎来到。", ""],
    ["00:00:06,000", "00:00:08,000", "再见。", ""]
]
```

### 8.2 超过字数限制测试

```python
# 输入
input_subtitles = [
    ["00:00:01,000", "00:00:03,000", "这是一段比较长的字幕内容，", ""],
    ["00:00:03,000", "00:00:05,000", "再加上这一段就会超过限制了。", ""]
]

# 期望输出（max_chars=30）：不合并
expected = [
    ["00:00:01,000", "00:00:03,000", "这是一段比较长的字幕内容，", ""],
    ["00:00:03,000", "00:00:05,000", "再加上这一段就会超过限制了。", ""]
]
```

### 8.3 链式合并测试

```python
# 输入
input_subtitles = [
    ["00:00:01,000", "00:00:02,000", "你", ""],
    ["00:00:02,000", "00:00:03,000", "好", ""],
    ["00:00:03,000", "00:00:04,000", "呀", ""],
    ["00:00:05,000", "00:00:06,000", "再见", ""]
]

# 期望输出（max_chars=30）
expected = [
    ["00:00:01,000", "00:00:04,000", "你好呀", ""],
    ["00:00:05,000", "00:00:06,000", "再见", ""]
]
```

### 8.4 禁用合并测试

```python
# 输入
input_subtitles = [
    ["00:00:01,000", "00:00:03,000", "你好，", ""],
    ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""]
]

# 期望输出（max_chars=0）：不合并
expected = [
    ["00:00:01,000", "00:00:03,000", "你好，", ""],
    ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""]
]
```

---

## 9. 日志记录建议

合并操作应记录日志，便于调试和监控：

```python
original_count = len(subtitles)
merged_subtitles = merge_contiguous_subtitles(subtitles, max_chars)
merged_count = len(merged_subtitles)

if merged_count < original_count:
    log(f"字幕合并: {original_count} 条 → {merged_count} 条")
```

---

## 10. 性能考虑

- **时间复杂度**：O(N)，只需遍历一次字幕列表
- **空间复杂度**：O(N)，需要存储合并后的结果
- **适用场景**：字幕数量在万级以内无性能问题

---

## 11. 扩展建议

如需更灵活的合并策略，可考虑以下扩展：

1. **时间容差**：允许微小的时间间隙（如10ms内视为连续）
2. **分隔符**：合并时在文本间添加标点或空格
3. **按句号分割**：优先在句子边界处合并
4. **最小字数**：设置合并的最小字数阈值，避免过短的合并
