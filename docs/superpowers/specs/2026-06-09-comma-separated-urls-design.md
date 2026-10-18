# 逗号分隔多链接批量添加

## 背景

当前 `gui.pyw` 的 `_parse_server_urls` 方法支持 `~N` 批量端口语法（如 `http://127.0.0.1:7860~2/` 生成 7860/7861/7862），但不支持一次输入多个独立链接。

## 目标

在 "Add & Connect" 输入框中支持逗号分隔的多链接输入，每个链接独立支持 `~N` 批量语法。

## 输入格式

| 输入 | 解析结果 |
|------|----------|
| `http://a:7860/` | `http://a:7860/` |
| `http://a:7860~2/` | `http://a:7860/`, `http://a:7861/`, `http://a:7862/` |
| `http://a:7860/, http://b:7860/` | `http://a:7860/`, `http://b:7860/` |
| `http://a:7860~1/, http://b:7860~1/` | `http://a:7860/`, `http://a:7861/`, `http://b:7860/`, `http://b:7861/` |
| `http://a:7860/,http://b:7860/` | `http://a:7860/`, `http://b:7860/` |
| `http://a:7860/ , http://b:7860/` | `http://a:7860/`, `http://b:7860/` |

## 设计

### 改动范围

仅修改 `gui.pyw` 中的 `_parse_server_urls` 方法，提取原有逻辑为 `_parse_single_url` 辅助方法。

### 逻辑流程

```
输入原始字符串
  ↓ 按逗号分割
  ↓ 每段 trim 空格，过滤空串
  ↓ 对每段调用 _parse_single_url（原有 ~N 解析）
  ↓ flatten 合并结果
  ↓ 返回 URL 列表
```

### 代码结构

```python
def _parse_server_urls(self, raw: str) -> list[str]:
    """解析输入，支持逗号分隔多链接 + 每段独立的 ~N 批量语法。"""
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    all_urls = []
    for part in parts:
        all_urls.extend(self._parse_single_url(part))
    return all_urls

def _parse_single_url(self, raw: str) -> list[str]:
    """解析单个 URL，支持 ~N 批量端口语法。
    
    http://127.0.0.1:7860~2/  →  7860, 7861, 7862 共3个
    http://127.0.0.1:7860/    →  仅 7860
    """
    import re
    m = re.search(r'~(\d+)', raw)
    if not m:
        url = raw if raw.endswith('/') else raw + '/'
        return [url]

    extra = int(m.group(1))
    base = raw[:m.start()] + raw[m.end():]
    if not base.endswith('/'):
        base += '/'

    port_m = re.search(r':(\d+)/', base)
    if not port_m:
        self.append_log("⚠️ 无法解析端口号，请确保 URL 格式为 http://host:port/")
        return [base]

    base_port = int(port_m.group(1))
    prefix = base[:port_m.start(1)]
    suffix = base[port_m.end(1):]

    urls = []
    for i in range(extra + 1):
        urls.append(f"{prefix}{base_port + i}{suffix}")
    return urls
```

### 向后兼容

- 不含逗号的输入行为完全不变
- `_parse_single_url` 逻辑与原 `_parse_server_urls` 完全一致
- `add_server` 方法无需修改

## 验证

手动测试用例：
1. 单链接：`http://127.0.0.1:7860/` → 1 个
2. 单链接 + ~N：`http://127.0.0.1:7860~2/` → 3 个
3. 逗号分隔：`http://a:7860/, http://b:7860/` → 2 个
4. 逗号分隔 + ~N：`http://a:7860~1/, http://b:7860~1/` → 4 个
5. 无空格逗号：`http://a:7860/,http://b:7860/` → 2 个
6. 多余空格：`http://a:7860/ , http://b:7860/` → 2 个
