import os
import re
import shutil
import hashlib
import threading
import time
import gc  # 引入垃圾回收机制
from typing import List, Optional, Dict, Union
from tqdm import tqdm
from pydub import AudioSegment
import subprocess

import config
from config import (
    SRT_PATH,
    MODEL_PATH, TRAINING_THRESHOLD, MIN_SPEED, MAX_SPEED,
    TRANSFORMERS_LINE, USE_TQDM_PROGRESS_BAR, model_lock, status_lock
)
from logger import log as logger_log
from utils import load_status, save_status, clear_status, time_str_to_seconds
from subtitle_parser import parse_subtitles, merge_contiguous_subtitles
from audio_processor import (
    extract_single_audio, merge_single_audio_video, crop_audio,
    adjust_duration_with_rubberband, merge_audio
)
from model import DurationPredictor
from api_client import generate_audio_api  # 单服务器模式保留兼容
from dispatcher import MultiServerDispatcher  # 多服务器分发器

# Initialize global duration predictor
duration_predictor = DurationPredictor(MODEL_PATH, TRAINING_THRESHOLD)


# === 回调接口类 ===
class ProgressCallback:
    def log(self, message: str): pass

    def set_total_files(self, total: int): pass

    def update_file_progress(self, current: int): pass

    def set_current_task_range(self, total: int): pass

    def update_task_progress(self, current: int): pass


# === 字幕文本预处理 ===
def preprocess_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("**", "")
    text = text.strip()
    return text


def post_process_audio_task(index: int, raw_generated_path: str, applied_speed: float, text: str,
                            target_duration_s: float, current_tmp_dir: str):
    output_path_for_merge = os.path.join(current_tmp_dir, f"output_{index}.wav")

    if not raw_generated_path or not os.path.exists(raw_generated_path):
        logger_log("POST", f"!! [Task {index}] API生成失败，创建静音占位符。")
        AudioSegment.silent(duration=target_duration_s * 1000).export(output_path_for_merge, format="wav")
        return index
    try:
        actual_raw_duration_s = 0
        try:
            audio = AudioSegment.from_file(raw_generated_path)
            actual_raw_duration_s = (len(audio) / 1000.0) * applied_speed
        except Exception as e:
            logger_log("POST", f"!! [Task {index}] 无法获取生成音频的时长: {e}")
        adjust_duration_with_rubberband(raw_generated_path, output_path_for_merge, target_duration_s)
        try:
            os.remove(raw_generated_path)
        except OSError:
            pass
        if actual_raw_duration_s > 0:
            with model_lock:
                duration_predictor.add_data_point_and_retrain(text, actual_raw_duration_s)
        return index
    except Exception as e:
        logger_log("POST", f"!! [Task {index}] Post-processing 发生严重错误: {e}")
        return index


def tts_generation_task(index: int, subtitle: List, main_reference_audio: Union[str, AudioSegment], current_tmp_dir: str):
    start_time, end_time, raw_text, _ = subtitle
    text = preprocess_text(raw_text)
    if not text: return None, 0, text, 0
    reference_clip = crop_audio(start_time, end_time, main_reference_audio)

    if not reference_clip or len(reference_clip) < 500:
        return None, 0, text, 0

    target_duration_s = time_str_to_seconds(end_time) - time_str_to_seconds(start_time)
    predicted_raw_duration_s = duration_predictor.predict_duration(text)
    required_speed = (predicted_raw_duration_s / target_duration_s) if predicted_raw_duration_s > 0.1 else 1.0
    applied_speed = max(MIN_SPEED, min(required_speed, MAX_SPEED))

    ref_tmp_dir = os.path.join(current_tmp_dir, "ref_clips")
    os.makedirs(ref_tmp_dir, exist_ok=True)
    ref_clip_path = os.path.join(ref_tmp_dir, f"ref_{index}.wav")
    reference_clip.export(ref_clip_path, format="wav")

    raw_generated_path = generate_audio_api(ref_clip_path, text, applied_speed)
    return raw_generated_path, applied_speed, text, target_duration_s


def _prepare_tts_params(index: int, subtitle: List, main_reference_audio: Union[str, AudioSegment],
                        current_tmp_dir: str, last_valid_ref_path: str = None) -> Optional[dict]:
    """
    预处理字幕，导出参考音频片段，返回 API 调用所需参数。
    多服务器模式专用，不发起 API 请求。
    加入“借用上一次音频”的容错机制。
    """
    start_time, end_time, raw_text, _ = subtitle
    text = preprocess_text(raw_text)
    if not text:
        return None

    # 1. 准确计算当前字幕的目标时长（用于控制最终语速），而不是完全依赖音频片段长度
    target_duration_s = time_str_to_seconds(end_time) - time_str_to_seconds(start_time)
    if target_duration_s <= 0:
        target_duration_s = 0.1  # 保底时长

    # 2. 截取当前参考音频
    reference_clip = crop_audio(start_time, end_time, main_reference_audio)

    # 3. 判断音频是否“健康”（低于 500 毫秒极易导致服务端爆显存或报错）
    is_valid_clip = reference_clip is not None and len(reference_clip) >= 500

    ref_clip_path = None
    if is_valid_clip:
        # 当前音频健康，正常导出
        ref_tmp_dir = os.path.join(current_tmp_dir, "ref_clips")
        os.makedirs(ref_tmp_dir, exist_ok=True)
        ref_clip_path = os.path.join(ref_tmp_dir, f"ref_{index}.wav")
        reference_clip.export(ref_clip_path, format="wav")
    else:
        # 当前音频太短/有毒，触发回退机制：使用上一次的健康音频
        if last_valid_ref_path and os.path.exists(last_valid_ref_path):
            current_len = len(reference_clip) if reference_clip else 0
            logger_log("WARN", f"[Task {index}] 参考音频过短 ({current_len}ms)，自动复用上一句的健康音频！")
            ref_clip_path = last_valid_ref_path
        else:
            logger_log("WARN", f"[Task {index}] 参考音频过短，且无历史音频可复用，强制跳过。")
            return None

    # 4. 计算语速
    predicted_raw_duration_s = duration_predictor.predict_duration(text)
    required_speed = (predicted_raw_duration_s / target_duration_s) if predicted_raw_duration_s > 0.1 else 1.0
    applied_speed = max(MIN_SPEED, min(required_speed, MAX_SPEED))

    return {
        "ref_audio_path": ref_clip_path,
        "text": text,
        "speed": applied_speed,
        "target_duration_s": target_duration_s,
    }


def _find_video_path(base_name_no_ext, srt_path):
    """在 srt_path 下查找匹配的视频文件。"""
    video_extensions = {".mp4", ".ts", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm"}
    for ext in video_extensions:
        potential_path = os.path.join(srt_path, f"{base_name_no_ext}{ext}")
        if os.path.exists(potential_path):
            return potential_path
    return None


def _find_and_merge_video(base_name_no_ext, audio_path, output_folder_zh, srt_path, log):
    """查找源视频并与配音 WAV 合并，输出到中配文件夹。"""
    target_video_output = os.path.join(output_folder_zh, f"{base_name_no_ext}.mp4")
    if os.path.exists(target_video_output):
        log(f"  -> 最终视频已存在: {os.path.basename(target_video_output)}，无需重复合并。")
        return

    found_video_path = _find_video_path(base_name_no_ext, srt_path)
    if found_video_path:
        log("--- 准备合并音视频 ---")
        try:
            if merge_single_audio_video(found_video_path, audio_path, target_video_output):
                log(f"视频合并成功: {os.path.basename(target_video_output)}")
            else:
                log(f"!! 视频合并失败，请检查上方 ffmpeg 错误信息。")
        except Exception as e:
            log(f"!! 合并失败: {e}")
    else:
        log(f"!! 警告: 未能为 '{base_name_no_ext}' 找到匹配的视频文件，跳过合并。")


def _recover_last_ref(current_tmp_dir, uncompleted_indices, log):
    """从上次中断的 ref_clips 中恢复最后一个有效参考音频路径。"""
    ref_tmp_dir = os.path.join(current_tmp_dir, "ref_clips")
    if not os.path.isdir(ref_tmp_dir) or not uncompleted_indices:
        return None
    first_uncompleted = uncompleted_indices[0]
    best_path = None
    best_idx = -1
    try:
        for f in os.listdir(ref_tmp_dir):
            if not (f.startswith("ref_") and f.endswith(".wav")):
                continue
            try:
                idx = int(f[4:-4])
            except ValueError:
                continue
            if idx < first_uncompleted and idx > best_idx:
                candidate = os.path.join(ref_tmp_dir, f)
                if os.path.getsize(candidate) > 1000:
                    best_idx = idx
                    best_path = candidate
    except OSError:
        return None
    if best_path:
        log(f"  -> [恢复系统] 从历史参考片段恢复 fallback 音频: ref_{best_idx}.wav")
    return best_path


def _check_task_skip_or_recover(subtitle_name, output_folder_zh, output_audio_file,
                                 srt_path, log):
    """
    判断任务完成状态，返回 "skip" 或 "process"。

    三级判断：
      1. 中配文件夹下已有视频 → 任务完成，返回 "skip"
      2. 无视频但已有合并好的 WAV（且 WAV 健康）→ 合成视频，返回 "skip"
      3. 无视频也无 WAV → 返回 "process"，开始完整流程
    """
    target_video_output = os.path.join(output_folder_zh, f"{subtitle_name}.mp4")

    # ── 第 1 级：视频已存在 ──
    if os.path.exists(target_video_output):
        log(f"  -> [完成] 最终视频已存在: {os.path.basename(target_video_output)}，跳过。")
        return "skip"

    # ── 第 2 级：WAV 存在 → 恢复合并 ──
    if os.path.exists(output_audio_file):
        wav_size = os.path.getsize(output_audio_file)
        if wav_size < 1000:
            log(f"  !! [恢复] WAV 文件异常 ({wav_size} bytes)，视为无效，重新生成。")
            return "process"

        # 额外验证：确保 WAV 可被 pydub 读取
        try:
            test_audio = AudioSegment.from_file(output_audio_file)
            if len(test_audio) < 100:  # < 0.1 秒，视为无效占位
                log(f"  !! [恢复] WAV 时长异常 ({len(test_audio)}ms)，重新生成。")
                return "process"
        except Exception:
            log(f"  !! [恢复] WAV 文件损坏无法读取，重新生成。")
            return "process"

        log(f"  -> [恢复] 已找到配音 WAV ({wav_size / 1024:.0f} KB)，跳过生成，直接合成视频...")
        _find_and_merge_video(subtitle_name, output_audio_file, output_folder_zh, srt_path, log)

        # 清理可能残留的参考音频（来自上一次未完成运行）
        ref_audio_dir = os.path.join(srt_path, "REF_AUDIO_PATH")
        stale_ref = os.path.join(ref_audio_dir, f"{subtitle_name}.wav")
        if os.path.exists(stale_ref):
            try:
                os.remove(stale_ref)
            except OSError:
                pass

        return "skip"

    # ── 第 3 级：无视频无 WAV，开始全新流程 ──
    log(f"  -> [新建] 未检测到任何产物，开始完整流程。")
    return "process"


def process_srt_files(srt_paths, transformers_line: int = TRANSFORMERS_LINE,
                      max_workers: int = 2,
                      progress_callback=None,
                      server_configs: Dict[str, int] = None,
                      on_server_down=None,
                      on_all_down=None,
                      max_retries: int = None):
    """
    主处理函数。srt_paths 可以是单个字符串（兼容旧调用）或字符串列表。
    """

    def log(msg):
        logger_log("MAIN", msg)

    # ── 兼容旧调用：None/单个字符串转为列表 ──
    if srt_paths is None:
        srt_paths = []
    if isinstance(srt_paths, str):
        srt_paths = [srt_paths]
    srt_paths = [os.path.normpath(p) for p in srt_paths]

    def _longpath(p: str) -> str:
        """Windows 下添加长路径前缀，避免 MAX_PATH 260 字符限制"""
        if os.name == 'nt' and not p.startswith('\\\\?\\'):
            return '\\\\?\\' + os.path.abspath(p)
        return p

    # ── 判断模式 ──────────────────────────────
    use_multi_server = bool(server_configs)
    if use_multi_server:
        summary = ", ".join(f"{u.rstrip('/').split(':')[-1]}×{n}" for u, n in server_configs.items())
        log(f"[模式] 多服务器单队列分发：{summary}")
    else:
        log(f"[模式] 单服务器模式，并发线程: {max_workers}")

    # ── 预扫描所有文件夹，统计 SRT 文件总数 ──
    all_folder_files: list = []  # [(srt_path, srt_file), ...]
    valid_extensions = {".srt", ".txt"}
    for _srt_path in srt_paths:
        try:
            files = [
                f for f in os.listdir(_srt_path)
                if os.path.splitext(f)[1].lower() in valid_extensions
                   and not f.endswith("zh.srt") and not f.endswith("en.srt")
            ]
        except FileNotFoundError:
            log(f"错误: 路径不存在 {_srt_path}")
            continue
        try:
            files = sorted(files, key=lambda x: int(re.findall(r'\d+', x)[0]))
        except (IndexError, ValueError):
            files.sort()
        seen = set()
        for f in files:
            base = os.path.splitext(f)[0]
            if base not in seen:
                seen.add(base)
                all_folder_files.append((_srt_path, f))

    total_files = len(all_folder_files)
    if progress_callback:
        progress_callback.set_total_files(total_files)

    global_file_idx = 0
    processed_basenames = set()

    for srt_path, srt_file in all_folder_files:
        if config.ABORT_ALL:
            log("!!! 任务已由用户强制终止 !!!")
            break

        subtitle_name, _ = os.path.splitext(srt_file)
        dedup_key = (srt_path, subtitle_name)
        if dedup_key in processed_basenames:
            global_file_idx += 1
            continue
        processed_basenames.add(dedup_key)

        # Per-folder output paths
        output_path = srt_path
        output_folder_zh = os.path.join(srt_path, "中配")
        os.makedirs(output_folder_zh, exist_ok=True)
        ref_audio_path = os.path.join(srt_path, "REF_AUDIO_PATH")
        os.makedirs(ref_audio_path, exist_ok=True)

        if progress_callback:
            progress_callback.update_file_progress(global_file_idx)
        log(f"\n[{global_file_idx + 1}/{total_files}] === 检查文件 {srt_file} ===")
        log(f"  目录: {srt_path}")

        output_audio_file = os.path.join(output_path, f"{subtitle_name}.wav")

        skip_result = _check_task_skip_or_recover(
            subtitle_name, output_folder_zh, output_audio_file, srt_path, log
        )
        if skip_result == "skip":
            global_file_idx += 1
            continue

        main_audio_path = os.path.join(ref_audio_path, subtitle_name + ".wav")
        found_video_path = _find_video_path(subtitle_name, srt_path)
        audio_extracted_in_this_run = False

        if not os.path.exists(main_audio_path):
            if found_video_path:
                log(f"正在从视频提取参考音频: {os.path.basename(found_video_path)}")
                if extract_single_audio(found_video_path, main_audio_path):
                    audio_extracted_in_this_run = True
                else:
                    log(f"!! ffmpeg 提取音频失败，跳过此文件。")
                    global_file_idx += 1
                    continue
            else:
                log(f"!! 警告: 在 '{srt_path}' 下找不到与 '{subtitle_name}' 匹配的视频文件，跳过。")
                global_file_idx += 1
                continue

        if not os.path.exists(main_audio_path):
            global_file_idx += 1
            continue

        log(f"开始生成: {srt_file}")
        safe_dir_name = hashlib.md5(subtitle_name.encode('utf-8')).hexdigest()
        dynamic_tmp_root = os.path.join(srt_path, "tmp")
        current_tmp_dir = _longpath(os.path.join(dynamic_tmp_root, safe_dir_name))
        os.makedirs(current_tmp_dir, exist_ok=True)
        local_status_file = os.path.join(current_tmp_dir, "status.json")

        try:
            with open(os.path.join(srt_path, srt_file), "r", encoding="utf-8") as file:
                file_content = file.read()
        except Exception as e:
            log(f"读取文件失败: {e}")
            if audio_extracted_in_this_run and os.path.exists(main_audio_path): os.remove(main_audio_path)
            global_file_idx += 1
            continue

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

        completed_indices = load_status(local_status_file, srt_file)
        all_indices = list(range(len(parsed_subtitles)))

        recovered_count = 0
        for idx in all_indices:
            if idx not in completed_indices:
                potential_wav = os.path.join(current_tmp_dir, f"output_{idx}.wav")
                if os.path.exists(potential_wav) and os.path.getsize(potential_wav) > 1000:
                    completed_indices.add(idx)
                    recovered_count += 1
        if recovered_count > 0:
            log(f"  -> [恢复系统] 扫描到 {recovered_count} 个已存在片段，将跳过。")
            save_status(local_status_file, srt_file, completed_indices)

        uncompleted_indices = [idx for idx in all_indices if idx not in completed_indices]
        total_tasks = len(all_indices)

        if progress_callback:
            progress_callback.set_current_task_range(total_tasks)
            progress_callback.update_task_progress(len(completed_indices))

        if not uncompleted_indices:
            log("--- 所有片段均已存在，直接合并 ---")
        else:
            if config.ABORT_ALL: break

            # ════════════════════════════════════════
            # 多服务器动态分发模式
            # ════════════════════════════════════════
            if use_multi_server:
                log(f"--- [多服务器] 剩余 {len(uncompleted_indices)} 个任务，需求驱动分发 ---")

                def _on_server_down(url, stats):
                    log(f"⚠️ 服务器下线: {url} | {stats}")
                    if on_server_down:
                        on_server_down(url, stats)

                def _on_all_down():
                    log("🛑 所有服务器均已熔断，停止程序！")
                    setattr(config, 'ABORT_ALL', True)
                    if on_all_down:
                        on_all_down()

                dispatcher = MultiServerDispatcher(
                    server_configs=server_configs,
                    max_retries=max_retries if max_retries is not None else config.MAX_RETRIES,
                    on_server_down=_on_server_down,
                    on_all_down=_on_all_down,
                )
                dispatcher.start()

                completed_count = len(completed_indices)
                file_start_time = time.time()
                session_processed_count = 0

                # ── CPU Worker 注入：rubberband/pydub + 状态保存 ──
                def post_process_fn(idx, raw_path, server_url):
                    if config.ABORT_ALL:
                        return
                    params = task_params_map.get(idx)
                    if params is None:
                        return
                    post_process_audio_task(
                        idx, raw_path,
                        params["speed"], params["text"],
                        params["target_duration_s"],
                        current_tmp_dir
                    )
                    with status_lock:
                        completed_indices.add(idx)
                        save_status(local_status_file, srt_file, completed_indices)

                # ── CPU Worker 注入：进度日志 ──
                def done_callback(idx, server_url):
                    nonlocal completed_count, session_processed_count
                    if config.ABORT_ALL:
                        return
                    with status_lock:
                        completed_count += 1
                        session_processed_count += 1
                        elapsed = time.time() - file_start_time
                        if elapsed > 0 and session_processed_count > 0:
                            spd = session_processed_count / elapsed
                            speed_str = f"{spd:.2f} it/s" if spd >= 1 else f"{1 / spd:.2f} s/it"
                        else:
                            speed_str = "Calc..."
                        from urllib.parse import urlparse
                        _p = urlparse(server_url)
                        server_tag = f"{_p.hostname}:{_p.port}" if _p.port else _p.hostname
                        if progress_callback:
                            progress_callback.update_task_progress(completed_count)
                        log(f"  -> Task {idx} 完成 ({completed_count}/{total_tasks}) "
                            f"[{speed_str}] [{server_tag}]")

                # ── 任务迭代器：边裁剪参考音频边喂给调度器 ──
                # 记录最后一次成功健康的音频，用于异常/超短音频的容错替代
                task_params_map = {}

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

                # run_feeder 在当前线程阻塞运行，内部 put() 受有界队列控制
                # Worker 消费一个 → 空出一个位 → 立刻喂入下一个
                dispatcher.run_feeder(
                    task_iter=task_iterator(),
                    post_process_fn=post_process_fn,
                    done_callback=done_callback,
                    abort_flag_fn=lambda: config.ABORT_ALL,
                )
                dispatcher.stop()

            # ════════════════════════════════════════
            # 单服务器模式（原有逻辑，完整保留）
            # ════════════════════════════════════════
            else:
                import concurrent.futures
                log(f"--- 剩余 {len(uncompleted_indices)} 个任务 (并发数: {max_workers}) ---")

                pbar = None
                if not progress_callback and USE_TQDM_PROGRESS_BAR:
                    pbar = tqdm(total=len(uncompleted_indices), desc=f"处理 {subtitle_name}")

                completed_count = len(completed_indices)
                file_start_time = time.time()
                session_processed_count = 0

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = set()
                    for index in uncompleted_indices:
                        if config.ABORT_ALL: break
                        subtitle = parsed_subtitles[index]
                        gen_future = executor.submit(tts_generation_task, index, subtitle,
                                                     main_audio_path, current_tmp_dir)

                        def process_when_done(fut, idx=index, pbar_instance=pbar):
                            nonlocal completed_count, session_processed_count
                            if config.ABORT_ALL: return
                            try:
                                gen_result = fut.result()
                                post_result = post_process_audio_task(idx, *gen_result, current_tmp_dir)
                                with status_lock:
                                    completed_indices.add(post_result)
                                    save_status(local_status_file, srt_file, completed_indices)
                                    completed_count += 1
                                    session_processed_count += 1
                                    elapsed_time = time.time() - file_start_time
                                    speed_str = ""
                                    if elapsed_time > 0 and session_processed_count > 0:
                                        speed = session_processed_count / elapsed_time
                                        speed_str = f"{speed:.2f} it/s" if speed >= 1 else f"{1 / speed:.2f} s/it"
                                    else:
                                        speed_str = "Calc..."
                                    if progress_callback:
                                        progress_callback.update_task_progress(completed_count)
                                    elif pbar_instance:
                                        pbar_instance.update(1)
                                    log(f"  -> Task {idx} 完成 ({completed_count}/{total_tasks}) [{speed_str}]")
                            except Exception as e:
                                log(f"!! [Task {idx}] 错误: {e}")

                        gen_future.add_done_callback(process_when_done)
                        futures.add(gen_future)
                    concurrent.futures.wait(futures)

                if pbar: pbar.close()

        if config.ABORT_ALL: break

        log("--- 字幕片段合并中... ---")
        try:
            merged_audio = merge_audio(parsed_subtitles, current_tmp_dir)
            merged_audio.export(output_audio_file, format="wav")
            log(f"输出音频: {os.path.basename(output_audio_file)}")
            clear_status(local_status_file)
            if os.path.exists(current_tmp_dir):
                shutil.rmtree(current_tmp_dir)
            duration_predictor.train()
            _find_and_merge_video(subtitle_name, output_audio_file, output_folder_zh, srt_path, log)
        except Exception as e:
            log(f"!! 合并错误: {e}")
        finally:
            if os.path.exists(main_audio_path):
                try:
                    os.remove(main_audio_path)
                except OSError:
                    pass

        global_file_idx += 1

    if progress_callback:
        progress_callback.update_file_progress(total_files)
    msg = "\n=== 任务已强制停止 ===" if config.ABORT_ALL else "\n=== 所有任务处理完毕 ==="
    log(msg)

    for _srt_path in srt_paths:
        ref_ap = os.path.join(_srt_path, "REF_AUDIO_PATH")
        if os.path.exists(ref_ap) and not os.listdir(ref_ap):
            try:
                os.rmdir(ref_ap)
            except Exception:
                pass


if __name__ == '__main__':
    process_srt_files(SRT_PATH, TRANSFORMERS_LINE)