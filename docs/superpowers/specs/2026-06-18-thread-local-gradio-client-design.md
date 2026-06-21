# Thread-Local Gradio Client 设计文档

**日期**: 2026-06-18
**状态**: 已批准
**范围**: 修复并发线程共享 `gradio_client.Client` 实例导致的偶发调用失败

---

## 问题背景

`gradio_client.Client` 底层维护 WebSocket 连接和会话状态，不是线程安全的。当前代码中：

- `api_client.py` — `TTSClient._client` 是类级别单例，所有线程共享
- `dispatcher.py` — `_client_cache` 是全局字典 + 锁，但锁只保护查找，不保护 `predict()` 调用

当 `MAX_WORKERS > 1` 时，多线程同时调用同一 Client 的 `predict()` 方法，会导致 WebSocket 串线、响应匹配失败，引发偶发的"调用失败"报错。

## 设计目标

1. 每个工作线程拥有独立的 `Client` 实例
2. 零锁竞争，不引入新的同步原语
3. 最小化代码改动，不改变现有调用方逻辑
4. 保持现有错误处理和重试机制不变

## 方案选择

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| **Thread-Local Storage** | 无锁竞争，代码简洁 | N Worker = N 连接 | ✅ 采用 |
| 连接池 | 可限制连接数 | 实现复杂 | ❌ 过度设计 |
| 锁保护 predict() | 改动最小 | 串行化，吞吐量降 1/5 | ❌ 不可接受 |

## 架构变更

### 变更文件
- `api_client.py` — 单服务器模式
- `dispatcher.py` — 多服务器模式

### 不变部分
- `main.py` — 调用方无需改动
- `config.py` — 配置不变
- 线程数 / 重试逻辑 / 队列机制 — 全部保持原样

## 实现细节

### api_client.py

```python
import threading

_thread_local = threading.local()

class TTSClient:
    @classmethod
    def get_client(cls):
        if not hasattr(_thread_local, "client") or \
           getattr(_thread_local, "connected_url", None) != config.GRADIO_URL:
            thread_name = threading.current_thread().name
            _log("CONNECT", f"[{thread_name}] 正在连接到 Gradio 服务 ({config.GRADIO_URL})...")
            _thread_local.client = Client(config.GRADIO_URL)
            _thread_local.connected_url = config.GRADIO_URL
            _log("CONNECT", f"[{thread_name}] 连接成功！")
        return _thread_local.client
```

**行为**：
- 首次调用时，当前线程创建专属 Client
- 后续调用复用同一 Client（URL 未变时）
- URL 变化时自动重建连接
- 每个线程的连接互不干扰

### dispatcher.py

```python
_thread_local = threading.local()

def _get_client(url: str) -> Client:
    if not hasattr(_thread_local, "client_cache"):
        _thread_local.client_cache = {}
    if url not in _thread_local.client_cache:
        thread_name = threading.current_thread().name
        _log("CONNECT", f"[{thread_name}] 正在建立独立连接: {url}")
        _thread_local.client_cache[url] = Client(url)
        _log("CONNECT", f"[{thread_name}] 连接建立成功: {url}")
    return _thread_local.client_cache[url]

def invalidate_client(url: str):
    """清除当前线程对应 URL 的连接"""
    if hasattr(_thread_local, "client_cache"):
        _thread_local.client_cache.pop(url, None)
```

**行为**：
- 每个 GPU Worker 线程拥有独立的 `client_cache` 字典
- Worker 固定绑定 URL，每个线程实际只有 1 个 Client
- `invalidate_client` 只清除当前线程的连接（错误重连时正确）
- 移除 `_client_lock` — 不再需要

## 错误处理

| 场景 | 处理方式 |
|------|----------|
| 连接失败 | 异常传播，Worker 进入重试逻辑（现有机制不变） |
| 调用失败 | `invalidate_client` 清除当前线程连接，下次自动重建 |
| URL 切换 | `api_client.py` 中 `connected_url` 检查自动重建 |
| 线程退出 | Thread-Local 数据随线程自动回收 |

## 预期效果

- 偶发的"调用失败"报错应消失
- 每个线程首次调用时会多一条 `CONNECT` 日志（正常）
- 整体吞吐量不受影响（无锁竞争）
