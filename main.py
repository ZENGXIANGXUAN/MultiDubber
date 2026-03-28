import os
import re
import shutil
import hashlib
import threading
import time
from typing import List, Optional, Dict
from tqdm import tqdm
from pydub import AudioSegment
import subprocess

import config
from config import (
    SRT_PATH, REF_AUDIO_PATH, OUTPUT_PATH, TMP_DIR, MAX_SUBTITLE_LENGTH,
    MODEL_PATH, TRAINING_THRESHOLD, MIN_SPEED, MAX_SPEED, STATUS_FILE,
    TRANSFORMERS_LINE, USE_TQDM_PROGRESS_BAR, model_lock, status_lock
)
from utils import load_status, save_status, clear_status, time_str_to_seconds
from subtitle_parser import parse_subtitles, merge_consecutive_subtitles
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
                            target_duration_s: float, current_tmp_dir: str, callback=None):
    output_path_for_merge = os.path.join(current_tmp_dir, f"output_{index}.wav")

    def log(msg):
        if callback:
            callback.log(msg)
        elif not USE_TQDM_PROGRESS_BAR:
            print(msg)

    if not raw_generated_path or not os.path.exists(raw_generated_path):
        log(f"  !! [Task {index}] API生成失败，创建静音占位符。")
        AudioSegment.silent(duration=target_duration_s * 1000).export(output_path_for_merge, format="wav")
        return index
    try:
        actual_raw_duration_s = 0
        try:
            audio = AudioSegment.from_file(raw_generated_path)
            actual_raw_duration_s = (len(audio) / 1000.0) * applied_speed
        except Exception as e:
            log(f"!! [Task {index}] 无法获取生成音频的时长: {e}")
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
        log(f"!! [Task {index}] Post-processing 发生严重错误: {e}")
        return index


def tts_generation_task(index: int, subtitle: List, main_reference_audio: str, current_tmp_dir: str, callback=None):
    """单服务器模式用（兼容保留）"""
    start_time, end_time, raw_text, _ = subtitle
    text = preprocess_text(raw_text)
    if not text: return None, 0, text, 0
    reference_clip = crop_audio(start_time, end_time, main_reference_audio)

    # 【修复单服务器崩溃】如果音频极短，强制跳过避免崩溃
    if not reference_clip or len(reference_clip) < 500:
        return None, 0, text, 0

    target_duration_s = len(reference_clip) / 1000.0
    predicted_raw_duration_s = duration_predictor.predict_duration(text)
    required_speed = (predicted_raw_duration_s / target_duration_s) if predicted_raw_duration_s > 0.1 else 1.0
    applied_speed = max(MIN_SPEED, min(required_speed, MAX_SPEED))

    ref_tmp_dir = os.path.join(current_tmp_dir, "ref_clips")
    os.makedirs(ref_tmp_dir, exist_ok=True)
    ref_clip_path = os.path.join(ref_tmp_dir, f"ref_{index}.wav")
    reference_clip.export(ref_clip_path, format="wav")

    raw_generated_path = generate_audio_api(ref_clip_path, text, applied_speed)
    return raw_generated_path, applied_speed, text, target_duration_s


def _prepare_tts_params(index: int, subtitle: List, main_reference_audio: str,
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
            print(f"  -> ⚠️ [Task {index}] 参考音频过短 ({current_len}ms)，自动复用上一句的健康音频！")
            ref_clip_path = last_valid_ref_path
        else:
            print(f"  -> ⚠️ [Task {index}] 参考音频过短，且无历史音频可复用，强制跳过。")
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


def process_srt_files(srt_path: str, transformers_line: int = TRANSFORMERS_LINE,
                      max_workers: int = 2,
                      output_path: str = None, ref_audio_path: str = None,
                      progress_callback=None,
                      server_configs: Dict[str, int] = None,
                      on_server_down=None,
                      on_all_down=None,
                      max_retries: int = None):
    """
    主处理函数。
    """

    def log(msg):
        if progress_callback:
            progress_callback.log(msg)
        else:
            print(msg)

    # ── 路径规范化（修复 Windows 混合斜杠 + 长路径问题）──
    srt_path = os.path.normpath(srt_path)

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

    if output_path is None: output_path = srt_path
    if ref_audio_path is None: ref_audio_path = os.path.join(srt_path, "REF_AUDIO_PATH")

    os.makedirs(ref_audio_path, exist_ok=True)
    output_folder_zh = os.path.join(srt_path, "中配")
    os.makedirs(output_folder_zh, exist_ok=True)

    valid_extensions = {".srt", ".txt"}
    try:
        srt_files_to_process = [
            f for f in os.listdir(srt_path)
            if os.path.splitext(f)[1].lower() in valid_extensions
               and not f.endswith("zh.srt") and not f.endswith("en.srt")
        ]
    except FileNotFoundError:
        log(f"错误: 路径不存在 {srt_path}")
        return

    try:
        srt_files_to_process = sorted(srt_files_to_process, key=lambda x: int(re.findall(r'\d+', x)[0]))
    except:
        srt_files_to_process.sort()

    # 修复前（第 225 行）
    total_files = len(srt_files_to_process)

    # 修复后：先预算去重后的真实文件数
    seen = set()
    unique_files = []
    for f in srt_files_to_process:
        base, _ = os.path.splitext(f)
        if base not in seen:
            seen.add(base)
            unique_files.append(f)
    srt_files_to_process = unique_files  # 直接用去重后的列表，后面循环也不会重复
    total_files = len(srt_files_to_process)  # 现在这个数才是真实值
    if progress_callback:
        progress_callback.set_total_files(total_files)

    def _find_video_path(base_name_no_ext):
        video_extensions = {".mp4", ".ts", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm"}
        for ext in video_extensions:
            potential_path = os.path.join(srt_path, f"{base_name_no_ext}{ext}")
            if os.path.exists(potential_path):
                return potential_path
        return None

    def _find_and_merge_video(base_name_no_ext, audio_path):
        target_video_output = os.path.join(output_folder_zh, f"{base_name_no_ext}.mp4")
        if os.path.exists(target_video_output):
            log(f"  -> 最终视频已存在: {os.path.basename(target_video_output)}，无需重复合并。")
            return
        found_video_path = _find_video_path(base_name_no_ext)
        if found_video_path:
            log(f"--- 准备合并音视频 ---")
            try:
                if merge_single_audio_video(found_video_path, audio_path, target_video_output):
                    log(f"视频合并成功: {os.path.basename(target_video_output)}")
            except Exception as e:
                log(f"!! 合并失败: {e}")
        else:
            log(f"!! 警告: 未能为 '{base_name_no_ext}' 找到匹配的视频文件，跳过合并。")

    processed_basenames = set()

    for i, srt_file in enumerate(srt_files_to_process):
        if config.ABORT_ALL:
            log("!!! 任务已由用户强制终止 !!!")
            break

        subtitle_name, _ = os.path.splitext(srt_file)
        if subtitle_name in processed_basenames:
            continue
        processed_basenames.add(subtitle_name)

        if progress_callback:
            progress_callback.update_file_progress(i)
            progress_callback.log(f"\n[{i + 1}/{total_files}] === 检查文件 {srt_file} ===")
        else:
            print(f"\n[{i + 1}/{total_files}] === 检查文件 {srt_file} ===")

        output_audio_file = os.path.join(output_path, f"{subtitle_name}.wav")

        # 最终中配视频已存在 → 彻底完成，直接跳过
        target_video_output = os.path.join(output_folder_zh, f"{subtitle_name}.mp4")
        if os.path.exists(target_video_output):
            log(f"  -> 最终视频已存在，跳过。")
            continue

        if os.path.exists(output_audio_file):
            log(f"文件 {output_audio_file} 已存在，跳过生成。")
            _find_and_merge_video(subtitle_name, output_audio_file)
            continue

        main_audio_path = os.path.join(ref_audio_path, subtitle_name + ".wav")
        found_video_path = _find_video_path(subtitle_name)
        audio_extracted_in_this_run = False

        if not os.path.exists(main_audio_path):
            if found_video_path:
                log(f"正在从视频提取参考音频: {os.path.basename(found_video_path)}")
                if extract_single_audio(found_video_path, main_audio_path):
                    audio_extracted_in_this_run = True
                else:
                    log(f"!! ffmpeg 提取音频失败，跳过此文件。")
                    continue
            else:
                log(f"!! 警告: 在 '{srt_path}' 下找不到与 '{subtitle_name}' 匹配的视频文件，跳过。")
                continue

        if not os.path.exists(main_audio_path): continue

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
            continue

        parsed_subtitles = parse_subtitles(file_content, transformers_line)
        if not parsed_subtitles:
            log(f"无有效字幕，跳过。")
            if audio_extracted_in_this_run and os.path.exists(main_audio_path): os.remove(main_audio_path)
            continue

        merged_subtitles = merge_consecutive_subtitles(parsed_subtitles)

        completed_indices = load_status(local_status_file, srt_file)
        all_indices = list(range(len(merged_subtitles)))

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
                        current_tmp_dir, progress_callback
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
                            progress_callback.log(
                                f"  -> Task {idx} 完成 ({completed_count}/{total_tasks}) "
                                f"[{speed_str}] [{server_tag}]"
                            )
                        else:
                            print(f"  -> Task {idx} 完成. [{speed_str}] [{server_tag}]")

                # ── 任务迭代器：边裁剪参考音频边喂给调度器 ──
                # 记录最后一次成功健康的音频，用于异常/超短音频的容错替代
                task_params_map = {}

                def task_iterator():
                    last_valid_ref_path = None
                    for index in uncompleted_indices:
                        if config.ABORT_ALL:
                            return

                        params = _prepare_tts_params(
                            index, merged_subtitles[index],
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
                        subtitle = merged_subtitles[index]
                        gen_future = executor.submit(tts_generation_task, index, subtitle,
                                                     main_audio_path, current_tmp_dir, progress_callback)

                        def process_when_done(fut, idx=index, pbar_instance=pbar):
                            nonlocal completed_count, session_processed_count
                            if config.ABORT_ALL: return
                            try:
                                gen_result = fut.result()
                                post_result = post_process_audio_task(idx, *gen_result, current_tmp_dir,
                                                                      progress_callback)
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
                                        progress_callback.log(
                                            f"  -> Task {idx} 完成 ({completed_count}/{total_tasks}) [{speed_str}]")
                                    elif pbar_instance:
                                        pbar_instance.update(1)
                                    else:
                                        print(f"  -> Task {post_result} 完成.")
                            except Exception as e:
                                log(f"!! [Task {idx}] 错误: {e}")

                        gen_future.add_done_callback(process_when_done)
                        futures.add(gen_future)
                    concurrent.futures.wait(futures)

                if pbar: pbar.close()

        if config.ABORT_ALL: break

        log("--- 字幕片段合并中... ---")
        try:
            merged_audio = merge_audio(merged_subtitles, current_tmp_dir)
            merged_audio.export(output_audio_file, format="wav")
            log(f"输出音频: {os.path.basename(output_audio_file)}")
            clear_status(local_status_file)
            if os.path.exists(current_tmp_dir):
                shutil.rmtree(current_tmp_dir)
            duration_predictor.train()
            _find_and_merge_video(subtitle_name, output_audio_file)
        except Exception as e:
            log(f"!! 合并错误: {e}")
        finally:
            if os.path.exists(main_audio_path):
                try:
                    os.remove(main_audio_path)
                except:
                    pass

    if progress_callback:
        progress_callback.update_file_progress(total_files)
        msg = "\n=== 任务已强制停止 ===" if config.ABORT_ALL else "\n=== 所有任务处理完毕 ==="
        progress_callback.log(msg)

    try:
        if os.name == 'nt':
            subprocess.run('del /q /f /s %TEMP%\\*', shell=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except:
        pass

    if os.path.exists(ref_audio_path) and not os.listdir(ref_audio_path):
        try:
            os.rmdir(ref_audio_path)
        except:
            pass


if __name__ == '__main__':
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    os.makedirs(REF_AUDIO_PATH, exist_ok=True)
    process_srt_files(SRT_PATH, TRANSFORMERS_LINE)