# I/O 瓶颈修复与内存泄漏防护 (OOM-Safe) 设计文档

**日期**: 2026-06-21
**状态**: 已批准
**作者**: Claude Fable 5

## 1. 问题描述

当前项目在批量处理字幕音频时，存在严重的磁盘 I/O 阻塞。`crop_audio` 函数在循环/多线程中被频繁调用，导致同一大音频文件被重复读取成百上千次，造成 GPU 算力闲置。

**性能影响**：
- 假设 100 个字幕，音频文件 100MB
- 优化前总 I/O：100 × 100MB = 10GB
- 优化后总 I/O：1 × 100MB = 100MB
- 性能提升：约 100 倍（取决于字幕数量）

## 2. 设计目标

1. **性能优化**：解决批量处理时的 I/O 阻塞，提高 GPU 利用率
2. **内存安全**：防止处理极长音频时发生 OOM
3. **兼容性保持**：确保现有的多服务器和单服务器模式都能正常工作

## 3. 架构设计

### 3.1 核心思路

在任务处理开始前一次性加载音频文件到内存，处理过程中传递内存对象而非文件路径，处理完成后立即释放内存。

**内存生命周期**：
```
加载音频 → 任务处理 → 释放内存
   ↓           ↓          ↓
 一次I/O    多次内存切片   GC回收
```

### 3.2 关键改动点

1. `audio_processor.py` 中的 `crop_audio` 函数需要兼容 `str` 和 `AudioSegment` 两种输入
2. `main.py` 中的 `tts_generation_task` 和 `_prepare_tts_params` 函数需要接收内存对象
3. 在多服务器和单服务器模式的任务迭代器中实现内存加载和释放

## 4. 详细设计

### 4.1 audio_processor.py 中的 crop_audio 函数改造

**当前实现**：
```python
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

**改造后**：
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

**关键点**：
- 参数名从 `input_file` 改为 `input_file_or_audio`，更准确地反映其用途
- 使用 `isinstance` 检查类型，确保向后兼容
- 如果传入的是字符串，按原方式读取文件；如果是 AudioSegment 对象，直接使用

### 4.2 main.py 中的函数签名改动

**需要修改的函数**：
1. `tts_generation_task` - 接收内存对象作为参考音频
2. `_prepare_tts_params` - 接收内存对象作为参考音频

**类型注解更新**：
```python
from typing import List, Optional, Dict, Union
from pydub import AudioSegment

def tts_generation_task(index: int, subtitle: List, main_reference_audio: Union[str, AudioSegment], current_tmp_dir: str):
    ...

def _prepare_tts_params(index: int, subtitle: List, main_reference_audio: Union[str, AudioSegment],
                        current_tmp_dir: str, last_valid_ref_path: str = None) -> Optional[dict]:
    ...
```

### 4.3 内存管理策略（OOM 防护）

#### 4.3.1 多服务器模式的内存管理

**内存加载时机**：在 `task_iterator` 生成器启动时一次性加载

**内存释放时机**：生成器执行完毕（所有切片任务入队完成后）立即释放

**实现方式**：
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

#### 4.3.2 单服务器模式的内存管理

**内存加载时机**：在 `ThreadPoolExecutor` 启动前加载

**内存释放时机**：所有任务执行完毕后释放

**实现方式**：
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

            gen_future.add_done_callback(process_when_done)
            futures.add(gen_future)
        concurrent.futures.wait(futures)
finally:
    # [OOM防护]：单机模式任务队列派发并执行完毕后，强制清理内存
    if 'loaded_main_audio' in locals() and isinstance(loaded_main_audio, AudioSegment):
        del loaded_main_audio
        gc.collect()
```

#### 4.3.3 关键设计原则

1. **延迟加载**：音频只在真正需要时才加载到内存
2. **及时释放**：使用 `try-finally` 块确保内存一定会被释放
3. **优雅降级**：如果加载失败，自动回退到磁盘读取模式
4. **显式 GC**：调用 `gc.collect()` 确保内存立即回收，不依赖 Python 的自动垃圾回收

### 4.4 错误处理和兼容性

#### 4.4.1 错误处理策略

**内存加载失败的处理**：
```python
try:
    loaded_main_audio = AudioSegment.from_file(main_audio_path)
except Exception as e:
    log(f"内存载入失败，回退至硬盘直读模式: {e}")
    loaded_main_audio = main_audio_path  # 回退到文件路径模式
```

**关键点**：
- 捕获所有可能的异常（文件损坏、格式不支持、内存不足等）
- 加载失败时自动回退到原有的磁盘读取模式
- 记录错误日志，便于问题排查

#### 4.4.2 兼容性保证

**向后兼容**：
1. `crop_audio` 函数同时支持 `str` 和 `AudioSegment` 两种输入
2. 现有的单服务器和多服务器模式都能正常工作
3. 如果内存加载失败，系统会自动回退到原有的工作方式

#### 4.4.3 边界情况处理

1. **音频文件不存在**：保持原有的错误处理逻辑
2. **音频文件为空**：保持原有的错误处理逻辑
3. **内存不足**：捕获异常并回退到磁盘读取模式
4. **任务中断**：使用 `try-finally` 确保内存一定被释放

### 4.5 日志记录

**关键日志点**：
- 内存加载开始：`"正在将参考主音频载入内存..."`
- 内存加载失败：`"内存载入失败，回退至硬盘直读模式: {e}"`
- 内存释放完成：`"已释放内存中的主音频对象。"`

## 5. 数据流

### 5.1 多服务器模式数据流

```
┌─────────────────────────────────────────────────────────────┐
│  1. task_iterator() 启动                                      │
│     ├─ 加载音频文件到内存 (loaded_main_audio)                    │
│     └─ 初始化 last_valid_ref_path                             │
├─────────────────────────────────────────────────────────────┤
│  2. 循环处理每个字幕                                            │
│     ├─ _prepare_tts_params(index, subtitle, loaded_main_audio)│
│     │   ├─ crop_audio(start, end, loaded_main_audio)          │
│     │   │   └─ 直接在内存中切片，无需磁盘 I/O                     │
│     │   ├─ 导出参考音频片段到磁盘                                 │
│     │   └─ 返回参数字典                                        │
│     ├─ yield (index, ref_path, text, speed)                  │
│     └─ 更新 last_valid_ref_path                               │
├─────────────────────────────────────────────────────────────┤
│  3. 生成器执行完毕                                              │
│     ├─ del loaded_main_audio                                 │
│     └─ gc.collect()                                          │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 单服务器模式数据流

```
┌─────────────────────────────────────────────────────────────┐
│  1. 加载音频文件到内存 (loaded_main_audio)                       │
├─────────────────────────────────────────────────────────────┤
│  2. 创建 ThreadPoolExecutor                                  │
│     ├─ 循环提交任务                                            │
│     │   ├─ tts_generation_task(index, subtitle, loaded_main_audio)│
│     │   │   ├─ crop_audio(start, end, loaded_main_audio)      │
│     │   │   │   └─ 直接在内存中切片，无需磁盘 I/O                 │
│     │   │   ├─ 生成音频                                        │
│     │   │   └─ 返回结果                                        │
│     │   └─ process_when_done 回调                             │
│     └─ 等待所有任务完成                                         │
├─────────────────────────────────────────────────────────────┤
│  3. finally 块                                               │
│     ├─ del loaded_main_audio                                 │
│     └─ gc.collect()                                          │
└─────────────────────────────────────────────────────────────┘
```

## 6. 验证标准

1. **功能验证**：
   - 确认已在 `audio_processor.py` 补充了类型检查 `isinstance(..., str)`
   - 确认在 `main.py` 导入了 `gc` 模块并使用了 `finally` 块确保大对象被销毁
   - 确认没有破坏原来的生成器 `yield` 和任务 `continue` 逻辑

2. **性能验证**：
   - 批量处理时 GPU 利用率应显著提高
   - 磁盘 I/O 次数应大幅减少

3. **内存安全验证**：
   - 处理极长音频时不应发生 OOM
   - 内存应在任务完成后及时释放

4. **兼容性验证**：
   - 多服务器模式应正常工作
   - 单服务器模式应正常工作
   - 内存加载失败时应能自动回退到磁盘读取模式
