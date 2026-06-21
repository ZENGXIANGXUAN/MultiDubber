# Thread-Local Gradio Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix concurrent thread safety issue where multiple threads share a single `gradio_client.Client` instance, causing WebSocket conflicts and occasional API call failures.

**Architecture:** Replace shared `Client` instances with `threading.local()` storage, giving each worker thread its own dedicated Client connection. Zero lock contention, minimal code changes.

**Tech Stack:** Python `threading.local()`, `gradio_client.Client`

## Global Constraints

- `gradio_client.Client` is NOT thread-safe — each thread must have its own instance
- Existing retry logic (`max_retries`), server-down detection, and queue mechanisms remain unchanged
- `main.py` and `config.py` require NO modifications
- Each thread's first API call will produce an extra `CONNECT` log line (expected behavior)

---

### Task 1: Modify `api_client.py` — Single Server Mode

**Files:**
- Modify: `api_client.py:1-57` (entire file)

**Interfaces:**
- Consumes: `config.GRADIO_URL`, `logger.log`
- Produces: `TTSClient.get_client()` — same signature, now returns thread-local Client
- Produces: `generate_audio_api()` — unchanged signature, callers unaffected

- [ ] **Step 1: Write failing test for thread-local behavior**

Create `tests/test_thread_local_client.py`:

```python
# tests/test_thread_local_client.py
import sys
import os
import threading
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTTSClientThreadLocal:
    """TTSClient thread-local storage tests"""

    def test_each_thread_gets_own_client(self):
        """Each thread should create its own Client instance"""
        from api_client import TTSClient, _thread_local

        # Reset thread-local state
        if hasattr(_thread_local, "client"):
            delattr(_thread_local, "client")
        if hasattr(_thread_local, "connected_url"):
            delattr(_thread_local, "connected_url")

        clients = {}
        barrier = threading.Barrier(3)

        def get_client_in_thread(thread_name):
            with patch("api_client.Client") as mock_client_cls:
                mock_instance = MagicMock()
                mock_client_cls.return_value = mock_instance
                barrier.wait()  # Sync threads
                client = TTSClient.get_client()
                clients[thread_name] = id(client)

        threads = []
        for i in range(3):
            name = f"Thread-{i}"
            t = threading.Thread(target=get_client_in_thread, args=(name,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # All clients should be different instances
        client_ids = list(clients.values())
        assert len(set(client_ids)) == 3, f"Expected 3 unique clients, got {len(set(client_ids))}"

    def test_same_thread_reuses_client(self):
        """Same thread should reuse its Client if URL unchanged"""
        from api_client import TTSClient, _thread_local

        # Reset
        if hasattr(_thread_local, "client"):
            delattr(_thread_local, "client")
        if hasattr(_thread_local, "connected_url"):
            delattr(_thread_local, "connected_url")

        with patch("api_client.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance

            client1 = TTSClient.get_client()
            client2 = TTSClient.get_client()

            assert client1 is client2, "Same thread should reuse client"
            assert mock_client_cls.call_count == 1, "Client should only be created once"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/test_thread_local_client.py -v`
Expected: FAIL — `_thread_local` not defined, or tests fail due to shared singleton behavior

- [ ] **Step 3: Implement thread-local TTSClient**

Replace `api_client.py` with:

```python
import os
import threading
from typing import Optional
from gradio_client import Client, file
import warnings
import config
from logger import log as _log

# Suppress specific warnings
warnings.filterwarnings("ignore", category=UserWarning, module='gradio_client.utils')

# Thread-local storage for per-thread Client instances
_thread_local = threading.local()


def test_connection(url: str) -> bool:
    try:
        _log("CONNECT", f"正在测试连接: {url} ...")
        Client(url)
        _log("CONNECT", "连接成功！")
        return True
    except Exception as e:
        _log("CONNECT", f"连接失败: {e}")
        return False


class TTSClient:
    @classmethod
    def get_client(cls):
        # Each thread gets its own Client instance
        if not hasattr(_thread_local, "client") or getattr(_thread_local, "connected_url", None) != config.GRADIO_URL:
            try:
                thread_name = threading.current_thread().name
                _log("CONNECT", f"[{thread_name}] 正在连接到 Gradio 服务 ({config.GRADIO_URL})...")
                _thread_local.client = Client(config.GRADIO_URL)
                _thread_local.connected_url = config.GRADIO_URL
                _log("CONNECT", f"[{thread_name}] 连接成功！")
            except Exception as e:
                _log("CONNECT", f"[{thread_name}] 无法连接到 Gradio 服务。错误: {e}")
                raise e
        return _thread_local.client


def generate_audio_api(ref_audio_path: str, gen_text: str, speed: float) -> Optional[str]:
    if not os.path.exists(ref_audio_path):
        _log("API_ERR", f"参考音频文件不存在: {ref_audio_path}")
        return None

    try:
        client = TTSClient.get_client()
        result = client.predict(
            prompt=file(ref_audio_path), text=gen_text, infer_mode='普通推理',
            max_text_tokens_per_sentence=120, sentences_bucket_max_size=4, param_5=True,
            param_6=0.8, param_7=30, param_8=speed, param_9=0.0, param_10=3,
            param_11=10.0, param_12=600, api_name="/gen_single"
        )
        return result["value"] if result else None
    except Exception as e:
        _log("API_ERR", f"API 调用失败: {e}")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/test_thread_local_client.py -v`
Expected: PASS — all 2 tests pass

- [ ] **Step 5: Commit**

```bash
cd D:\Users\xuan\Mycode\MultTTS
git add api_client.py tests/test_thread_local_client.py
git commit -m "fix: use thread-local Client in api_client.py to prevent concurrent access conflicts"
```

---

### Task 2: Modify `dispatcher.py` — Multi Server Mode

**Files:**
- Modify: `dispatcher.py:44-62` (connection cache section)

**Interfaces:**
- Consumes: `logger.log`
- Produces: `_get_client(url: str) -> Client` — same signature, now returns thread-local Client
- Produces: `invalidate_client(url: str)` — same signature, clears current thread's client

- [ ] **Step 1: Write failing test for dispatcher thread-local behavior**

Add to `tests/test_thread_local_client.py`:

```python
class TestDispatcherThreadLocal:
    """dispatcher.py thread-local storage tests"""

    def test_each_thread_gets_own_dispatcher_client(self):
        """Each dispatcher worker thread should get its own Client per URL"""
        from unittest.mock import patch, MagicMock
        from dispatcher import _get_client, _thread_local

        # Reset thread-local state
        if hasattr(_thread_local, "client_cache"):
            delattr(_thread_local, "client_cache")

        test_url = "http://test-server:7860/"
        clients = {}
        barrier = threading.Barrier(3)

        def get_client_in_thread(thread_name):
            with patch("dispatcher.Client") as mock_client_cls:
                mock_instance = MagicMock()
                mock_client_cls.return_value = mock_instance
                barrier.wait()
                client = _get_client(test_url)
                clients[thread_name] = id(client)

        threads = []
        for i in range(3):
            name = f"Worker-{i}"
            t = threading.Thread(target=get_client_in_thread, args=(name,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Each thread should have its own client
        client_ids = list(clients.values())
        assert len(set(client_ids)) == 3

    def test_invalidate_clears_current_thread_only(self):
        """invalidate_client should only clear the calling thread's cache"""
        from unittest.mock import patch, MagicMock
        from dispatcher import _get_client, invalidate_client, _thread_local

        test_url = "http://test-server:7860/"

        # Reset
        if hasattr(_thread_local, "client_cache"):
            delattr(_thread_local, "client_cache")

        with patch("dispatcher.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance

            client1 = _get_client(test_url)
            invalidate_client(test_url)
            client2 = _get_client(test_url)

            # Should create new client after invalidation
            assert mock_client_cls.call_count == 2
            assert client1 is not client2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/test_thread_local_client.py::TestDispatcherThreadLocal -v`
Expected: FAIL — dispatcher still uses global cache with lock

- [ ] **Step 3: Implement thread-local dispatcher client**

Replace lines 44-62 in `dispatcher.py` with:

```python
# ──────────────────────────────────────────────
# 服务器连接缓存 (线程独立存储，避免并发冲突)
# ──────────────────────────────────────────────
_thread_local = threading.local()


def _get_client(url: str) -> Client:
    # Each thread maintains its own client cache
    if not hasattr(_thread_local, "client_cache"):
        _thread_local.client_cache = {}

    if url not in _thread_local.client_cache:
        thread_name = threading.current_thread().name
        _log("CONNECT", f"[{thread_name}] 正在建立独立连接: {url}")
        _thread_local.client_cache[url] = Client(url)
        _log("CONNECT", f"[{thread_name}] 连接建立成功: {url}")

    return _thread_local.client_cache[url]


def invalidate_client(url: str):
    """Clear current thread's client for the given URL (used after errors)"""
    if hasattr(_thread_local, "client_cache"):
        _thread_local.client_cache.pop(url, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/test_thread_local_client.py -v`
Expected: PASS — all 4 tests pass (2 from Task 1, 2 from Task 2)

- [ ] **Step 5: Commit**

```bash
cd D:\Users\xuan\Mycode\MultTTS
git add dispatcher.py tests/test_thread_local_client.py
git commit -m "fix: use thread-local Client cache in dispatcher.py to prevent concurrent access conflicts"
```

---

### Task 3: Verify Integration — Full Test Suite

**Files:**
- Test: `tests/test_thread_local_client.py`
- Test: `tests/test_subtitle_merge.py`

- [ ] **Step 1: Run all existing tests**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -m pytest tests/ -v`
Expected: All tests pass (no regressions)

- [ ] **Step 2: Verify no import errors**

Run: `cd D:\Users\xuan\Mycode\MultTTS && python -c "from api_client import TTSClient, generate_audio_api; from dispatcher import MultiServerDispatcher; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 3: Final commit with all changes**

```bash
cd D:\Users\xuan\Mycode\MultTTS
git status
git log --oneline -3
```

Verify the commit history shows both fixes applied cleanly.
