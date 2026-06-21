# I/O 瓶颈修复与 OOM 防护实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复批量处理字幕音频时的磁盘 I/O 阻塞问题，通过"单次读入，多次内存切片"机制提高性能，同时加入 OOM 防护防止内存溢出。

**Architecture:** 在任务处理开始前一次性加载音频文件到内存，处理过程中传递内存对象而非文件路径，处理完成后立即释放内存。使用 `gc.collect()` 显式垃圾回收确保内存及时释放。

**Tech Stack:** Python, pydub (AudioSegment), gc (垃圾回收)

## Global Constraints

- 所有改动必须保持向后兼容，支持 `str` 和 `AudioSegment` 两种输入
- 内存加载失败时必须优雅降级到磁盘读取模式
- 使用 `try-finally` 块确保内存一定被释放
- 不破坏现有的多服务器和单服务器模式逻辑

---

### Task 1: 修改 audio_processor.py 中的 crop_audio 函数

**Files:**
- Modify: `audio_processor.py:140-148`

**Interfaces:**
- Consumes: `start_time_str: str`, `end_time_str: str`, `input_file_or_audio: Union[str, AudioSegment]`
- Produces: `Optional[AudioSegment]` - 裁剪后的音频片段

- [ ] **Step 1: 读取当前 crop_audio 函数实现**

```python
# 当前实现 (audio_processor.py:140-148)
def crop_audio(start_time_str: str, end_time_str: str, input_file: str) -> Optional[AudioSegment]:
    try:
        start_ms = int(time_str_to_seconds(start_time_str) * 1000)
        end_ms = int(time_str_to_seconds(end_time_str) * 1000)
        audio = AudioSegment.from_file(input_file)
        return audio[max(0, start_ms):end_ms]
    except Exception as e:
        _log("ERROR", f"裁剪或读取音频时发生错误: {e}")
        return None
```

- [ ] **Step 2: 修改函数签名和实现**

将 `input_file` 参数改为 `input_file_or_audio`，并添加类型检查：

```python
def crop_audio(start_time_str: str, end_time_str: str, input_file_or_audio) -> Optional[AudioSegment]:
    try:
        start_ms = int(time_str_to_seconds(start_time_str) * 1000)
        end_ms = int(time_str_to_seconds(end_time_str) * 1000)
        
        # 兼容性处理：如果传入的是路径，则读取；如果是内存对象，则直接复用
        if isinstance(input_file_or_audio, str):
            audio = AudioSegment.from_file(input_file_or_audio)
        else:
            audio = input_file_or_audio
            
        return audio[max(0, start_ms):end_ms]
    except Exception as e:
        _log("ERROR", f"裁剪或读取音频时发生错误: {e}")
        return None
```

- [ ] **Step 3: 验证修改**

确认函数同时支持 `str` 和 `AudioSegment` 两种输入类型。

- [ ] **Step 4: 提交更改**

```bash
git add audio_processor.py
git commit -m "refactor: make crop_audio accept both str and AudioSegment input"
```

---

### Task 2: 修改 main.py 的导入和函数签名

**Files:**
- Modify: `main.py:1-10` (导入区域)
- Modify: `main.py:84` (tts_generation_task 函数签名)
- Modify: `main.py:107` (_prepare_tts_params 函数签名)

**Interfaces:**
- Consumes: `main_reference_audio: Union[str, AudioSegment]`
- Produces: 更新后的函数签名，支持内存对象传参

- [ ] **Step 1: 添加 gc 模块导入**

在 `main.py` 顶部添加 `gc` 模块导入：

```python
import hashlib
import threading
import time
import gc  # 引入垃圾回收机制
from typing import List, Optional, Dict, Union
```

- [ ] **Step 2: 修改 tts_generation_task 函数签名**

```python
# 当前签名 (main.py:84)
def tts_generation_task(index: int, subtitle: List, main_reference_audio: str, current_tmp_dir: str):

# 修改为
def tts_generation_task(index: int, subtitle: List, main_reference_audio: Union[str, AudioSegment], current_tmp_dir: str):
```

- [ ] **Step 3: 修改 _prepare_tts_params 函数签名**

```python
# 当前签名 (main.py:107)
def _prepare_tts_params(index: int, subtitle: List, main_reference_audio: str,
                        current_tmp_dir: str, last_valid_ref_path: str = None) -> Optional[dict]:

# 修改为
def _prepare_tts_params(index: int, subtitle: List, main_reference_audio: Union[str, AudioSegment],
                        current_tmp_dir: str, last_valid_ref_path: str = None) -> Optional[dict]:
```

- [ ] **Step 4: 验证修改**

确认两个函数的签名都已更新，且类型注解正确。

- [ ] **Step 5: 提交更改**

```bash
git add main.py
git commit -m "refactor: update function signatures to accept AudioSegment input"
```

---

### Task 3: 修改 main.py 多服务器调度模块 (OOM 防护)

**Files:**
- Modify: `main.py:517-549` (task_iterator 函数)

**Interfaces:**
- Consumes: `main_audio_path: str`, `parsed_subtitles: List`, `uncompleted_indices: List`
- Produces: 生成器，yield `(index, ref_audio_path, text, speed)` 元组

- [ ] **Step 1: 读取当前 task_iterator 实现**

```python
# 当前实现 (main.py:521-549)
def task_iterator():
    # 恢复上次中断前最后一个有效的参考音频片段
    last_valid_ref_path = _recover_last_ref(
        current_tmp_dir, uncompleted_indices, log
    )
    for index in uncompleted_indices:
        if config.ABORT_ALL:
            return

        params = _prepare_tts_params(
            index, parsed_subtitles[index],
            main_audio_path, current_tmp_dir,
            last_valid_ref_path=last_valid_ref_path
        )

        if params is None:
            # 文本为空或无可复用音频，直接标记为完成
            with status_lock:
                completed_indices.add(index)
            continue

        # 更新最后一次健康的音频路径，以便下个可能短促的任务复用
        last_valid_ref_path = params["ref_audio_path"]
        task_params_map[index] = params

        yield (index,
               params["ref_audio_path"],
               params["text"],
               params["speed"])
```

- [ ] **Step 2: 修改 task_iterator 添加 OOM 防护**

```python
def task_iterator():
    # 恢复上次中断前最后一个有效的参考音频片段
    last_valid_ref_path = _recover_last_ref(
        current_tmp_dir, uncompleted_indices, log
    )
    
    # [OOM防护]：仅在此处一次性载入大文件，避免 I/O 阻塞
    try:
        log("正在将参考主音频载入内存...")
        loaded_main_audio = AudioSegment.from_file(main_audio_path)
    except Exception as e:
        log(f"内存载入失败，回退至硬盘直读模式: {e}")
        loaded_main_audio = main_audio_path

    try:
        for index in uncompleted_indices:
            if config.ABORT_ALL:
                return

            params = _prepare_tts_params(
                index, parsed_subtitles[index],
                loaded_main_audio, current_tmp_dir,
                last_valid_ref_path=last_valid_ref_path
            )
            if params is None:
                with status_lock:
                    completed_indices.add(index)
                continue

            last_valid_ref_path = params["ref_audio_path"]
            task_params_map[index] = params

            yield (index, params["ref_audio_path"], params["text"], params["speed"])
    finally:
        # [OOM防护]：生成器执行完毕（切片任务入队完成）后，强制释放大内存
        if 'loaded_main_audio' in locals() and isinstance(loaded_main_audio, AudioSegment):
            del loaded_main_audio
            gc.collect()
            log("已释放内存中的主音频对象。")
```

- [ ] **Step 3: 验证修改**

确认：
1. 音频在生成器启动时一次性加载
2. 加载失败时回退到磁盘读取模式
3. 生成器执行完毕后内存被释放
4. 原有的 `yield` 和 `continue` 逻辑未被破坏

- [ ] **Step 4: 提交更改**

```bash
git add main.py
git commit -m "feat: add OOM protection to multi-server task_iterator"
```

---

### Task 4: 修改 main.py 单服务器调度模块 (OOM 防护)

**Files:**
- Modify: `main.py:576-614` (单服务器模式代码块)

**Interfaces:**
- Consumes: `main_audio_path: str`, `parsed_subtitles: List`, `uncompleted_indices: List`
- Produces: 并发执行任务，完成后释放内存

- [ ] **Step 1: 读取当前单服务器模式实现**

```python
# 当前实现 (main.py:576-614)
with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = set()
    for index in uncompleted_indices:
        if config.ABORT_ALL: break
        subtitle = parsed_subtitles[index]
        gen_future = executor.submit(tts_generation_task, index, subtitle,
                                     main_audio_path, current_tmp_dir)

        def process_when_done(fut, idx=index, pbar_instance=pbar):
            # ... 回调函数实现 ...
            pass

        gen_future.add_done_callback(process_when_done)
        futures.add(gen_future)
    concurrent.futures.wait(futures)

if pbar: pbar.close()
```

- [ ] **Step 2: 添加内存加载和释放逻辑**

```python
try:
    log("正在将参考主音频载入内存 (单机模式)...")
    loaded_main_audio = AudioSegment.from_file(main_audio_path)
except Exception as e:
    log(f"内存载入失败，回退至硬盘直读模式: {e}")
    loaded_main_audio = main_audio_path

try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = set()
        for index in uncompleted_indices:
            if config.ABORT_ALL: break
            subtitle = parsed_subtitles[index]
            gen_future = executor.submit(tts_generation_task, index, subtitle,
                                         loaded_main_audio, current_tmp_dir)

            def process_when_done(fut, idx=index, pbar_instance=pbar):
                # ... 回调函数实现保持不变 ...
                pass

            gen_future.add_done_callback(process_when_done)
            futures.add(gen_future)
        concurrent.futures.wait(futures)
finally:
    # [OOM防护]：单机模式任务队列派发并执行完毕后，强制清理内存
    if 'loaded_main_audio' in locals() and isinstance(loaded_main_audio, AudioSegment):
        del loaded_main_audio
        gc.collect()

if pbar: pbar.close()
```

- [ ] **Step 3: 验证修改**

确认：
1. 音频在 ThreadPoolExecutor 启动前加载
2. 加载失败时回退到磁盘读取模式
3. 所有任务完成后内存被释放
4. 原有的并发逻辑和回调函数未被破坏

- [ ] **Step 4: 提交更改**

```bash
git add main.py
git commit -m "feat: add OOM protection to single-server mode"
```

---

### Task 5: 整体验证和最终提交

**Files:**
- Verify: `audio_processor.py`
- Verify: `main.py`

- [ ] **Step 1: 验证所有修改**

检查清单：
1. ✅ `audio_processor.py` 中 `crop_audio` 函数支持 `str` 和 `AudioSegment` 两种输入
2. ✅ `main.py` 导入了 `gc` 模块
3. ✅ `main.py` 中 `tts_generation_task` 和 `_prepare_tts_params` 函数签名已更新
4. ✅ 多服务器模式的 `task_iterator` 添加了 OOM 防护
5. ✅ 单服务器模式添加了 OOM 防护
6. ✅ 所有修改都保持向后兼容

- [ ] **Step 2: 运行测试（如果有）**

```bash
# 如果有测试套件
python -m pytest tests/ -v
```

- [ ] **Step 3: 最终提交**

```bash
git add -A
git commit -m "feat: implement I/O bottleneck fix with OOM protection

- Make crop_audio accept both str and AudioSegment input
- Add gc import and update function signatures in main.py
- Add OOM protection to multi-server task_iterator
- Add OOM protection to single-server mode
- Ensure memory is released after task completion"
```
