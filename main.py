import os
import re
import shutil
import hashlib
import threading
import time
import gc  # 引入垃圾回收机制

# 必须尽早安装：用于捕捉启动阶段、后台线程、C 扩展 fatal error 的兜底日志。
from crash_guard import install_crash_guard
install_crash_guard("MultTTS")

from logger import init_logger
init_logger(name="MultTTS")
from typing import List, Optional, Dict, Union, Callable
from tqdm import tqdm
from pydub import AudioSegment
import soundfile as sf
import subprocess
import concurrent.futures

import config
import runtime_state
from config import (
    SRT_PATH,
    MODEL_PATH, TRAINING_THRESHOLD, MIN_SPEED, MAX_SPEED,
    TRANSFORMERS_LINE, USE_TQDM_PROGRESS_BAR, model_lock, status_lock
)
from logger import log as logger_log
from utils import load_status, save_status, clear_status, time_str_to_seconds, is_valid_audio_segment
from subtitle_parser import parse_subtitles, merge_contiguous_subtitles
from subtitle_preprocessor import preprocess_srt_content, preprocess_text
from audio_processor import (
    extract_single_audio, merge_single_audio_video, crop_audio,
    adjust_duration_with_rubberband, merge_audio
)
from model import DurationPredictor
from dispatcher import MultiServerDispatcher, reset_circuit_breaker_states  # 多服务器分发器与熔断重置
from local_cache_manager import LocalCacheManager, is_cloud_path

# Initialize global duration predictor
duration_predictor = DurationPredictor(MODEL_PATH, TRAINING_THRESHOLD)

REFERENCE_AUDIO_SAMPLE_RATE = 24000


def _export_reference_clip(reference_clip: AudioSegment, output_path: str) -> None:
    """导出 VoxCPM 参考音频：24kHz、单声道、16-bit WAV。"""
    optimized_clip = (
        reference_clip
        .set_channels(1)
        .set_frame_rate(REFERENCE_AUDIO_SAMPLE_RATE)
        .set_sample_width(2)
    )
    optimized_clip.export(output_path, format="wav")


# === 回调接口类 ===
class ProgressCallback:
    def log(self, message: str): pass

    def set_total_files(self, total: int): pass

    def update_file_progress(self, current: int): pass

    def set_current_task_range(self, total: int): pass

    def update_task_progress(self, current: int): pass


# === 字幕文本预处理 ===
def post_process_audio_task(index: int, raw_generated_path: str, applied_speed: float, text: str,
                            target_duration_s: float, current_tmp_dir: str):
    output_path_for_merge = os.path.join(current_tmp_dir, f"output_{index}.wav")

    if not raw_generated_path or not os.path.exists(raw_generated_path):
        logger_log("POST", f"!! [Task {index}] API生成失败，创建静音占位符。")
        AudioSegment.silent(duration=target_duration_s * 1000).export(output_path_for_merge, format="wav")
        return index
    # 校验文件大小：0 字节或极小文件视为下载/传输失败
    try:
        file_size = os.path.getsize(raw_generated_path)
    except OSError:
        file_size = 0
    if file_size < 100:
        logger_log("POST", f"!! [Task {index}] 生成音频文件无效 (size={file_size}B)，创建静音占位符。")
        AudioSegment.silent(duration=target_duration_s * 1000).export(output_path_for_merge, format="wav")
        try:
            os.remove(raw_generated_path)
        except OSError:
            pass
        return index
    try:
        actual_raw_duration_s = 0
        try:
            audio = AudioSegment.from_file(raw_generated_path)
            actual_raw_duration_s = (len(audio) / 1000.0) * applied_speed
        except Exception as e:
            logger_log("POST", f"!! [Task {index}] 无法解码生成音频 (文件可能已损坏)，创建静音占位符: {e}")
            AudioSegment.silent(duration=target_duration_s * 1000).export(output_path_for_merge, format="wav")
            try:
                os.remove(raw_generated_path)
            except OSError:
                pass
            return index
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
    with model_lock:
        predicted_raw_duration_s = duration_predictor.predict_duration(text)
    required_speed = (predicted_raw_duration_s / target_duration_s) if predicted_raw_duration_s > 0.1 else 1.0
    applied_speed = max(MIN_SPEED, min(required_speed, MAX_SPEED))

    ref_tmp_dir = os.path.join(current_tmp_dir, "ref_clips")
    os.makedirs(ref_tmp_dir, exist_ok=True)
    ref_clip_path = os.path.join(ref_tmp_dir, f"ref_{index}.wav")
    _export_reference_clip(reference_clip, ref_clip_path)

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
        _export_reference_clip(reference_clip, ref_clip_path)
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
    with model_lock:
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


def _delete_completed_wav(audio_path, log):
    """最终视频已经成功生成后删除整条配音 WAV；删除失败只记录，不影响视频完成状态。"""
    if not audio_path or not os.path.exists(audio_path):
        return
    try:
        os.remove(audio_path)
        log(f"  -> 已删除合并完成后的 WAV: {os.path.basename(audio_path)}")
    except OSError as e:
        log(f"!! 警告: 最终视频已生成，但 WAV 删除失败: {audio_path} | {e}")


def _copy_srt_to_output(srt_file, srt_path, output_folder_zh, log):
    """将清洗后的 SRT 原子写入中配目录；TXT 输入无需复制。"""
    if not srt_file or os.path.splitext(srt_file)[1].lower() != ".srt":
        return True

    source_srt = os.path.join(srt_path, os.path.basename(srt_file))
    target_srt = os.path.join(output_folder_zh, os.path.basename(srt_file))
    temp_srt = target_srt + ".part"

    if not os.path.isfile(source_srt):
        log(f"!! 字幕复制失败: 找不到源字幕 {source_srt}")
        return False

    if os.path.normcase(os.path.abspath(source_srt)) == os.path.normcase(os.path.abspath(target_srt)):
        return True

    try:
        os.makedirs(output_folder_zh, exist_ok=True)
        with open(source_srt, "rb") as source_file:
            source_bytes = source_file.read()
        has_utf8_bom = source_bytes.startswith(b"\xef\xbb\xbf")
        source_text = source_bytes.decode("utf-8-sig")
        processed_text = preprocess_srt_content(source_text, TRANSFORMERS_LINE)
        target_bytes = (b"\xef\xbb\xbf" if has_utf8_bom else b"") + processed_text.encode("utf-8")

        if os.path.exists(temp_srt):
            os.remove(temp_srt)
        with open(temp_srt, "wb") as temp_file:
            temp_file.write(target_bytes)
        shutil.copystat(source_srt, temp_srt)
        os.replace(temp_srt, target_srt)
        log(f"字幕已复制到中配: {os.path.basename(target_srt)}")
        return True
    except Exception as e:
        try:
            if os.path.exists(temp_srt):
                os.remove(temp_srt)
        except OSError:
            pass
        log(f"!! 字幕复制失败: {source_srt} -> {target_srt} | {e}")
        return False


def _find_and_merge_video(base_name_no_ext, audio_path, output_folder_zh, srt_path, log,
                          srt_file=None, cloud_source_path=None):
    """
    查找源视频并与配音 WAV 合并，输出到中配文件夹。

    返回 True 表示最终 MP4 已经可靠存在；False 表示本次未能完成合并。
    为避免进程在 ffmpeg 写文件途中中断后留下“看似存在、实际损坏”的 MP4，
    先写入 .part.mp4，成功后再原子替换正式文件。
    """
    os.makedirs(output_folder_zh, exist_ok=True)
    target_video_output = os.path.join(output_folder_zh, f"{base_name_no_ext}.mp4")
    if os.path.exists(target_video_output):
        log(f"  -> 最终视频已存在: {os.path.basename(target_video_output)}，无需重复合并。")
        return _copy_srt_to_output(srt_file, srt_path, output_folder_zh, log)

    found_video_path = _find_video_path(base_name_no_ext, srt_path)
    if not found_video_path and cloud_source_path:
        found_video_path = _find_video_path(base_name_no_ext, cloud_source_path)

    if not found_video_path:
        log(f"!! 警告: 未能为 '{base_name_no_ext}' 找到匹配的视频文件，跳过合并。")
        return False

    if not os.path.isfile(found_video_path):
        log(f"!! 警告: 视频文件路径不存在: {found_video_path}，跳过合并。")
        return False
    if not os.path.isfile(audio_path):
        log(f"!! 警告: 音频文件路径不存在: {audio_path}，跳过合并。")
        return False

    temp_video_output = target_video_output + ".part.mp4"
    try:
        if os.path.exists(temp_video_output):
            os.remove(temp_video_output)
    except OSError:
        pass

    log(f"--- 开始合并音视频: {os.path.basename(target_video_output)} ---")
    try:
        t0 = time.time()
        ok, err = merge_single_audio_video(found_video_path, audio_path, temp_video_output)
        elapsed = time.time() - t0
        if not ok:
            log(f"!! 视频合并失败: {err}")
            try:
                if os.path.exists(temp_video_output):
                    os.remove(temp_video_output)
            except OSError:
                pass
            return False

        if not os.path.exists(temp_video_output) or os.path.getsize(temp_video_output) <= 0:
            log("!! 视频合并失败: ffmpeg 未生成有效的临时 MP4。")
            return False

        os.replace(temp_video_output, target_video_output)
        log(f"视频合并成功: {os.path.basename(target_video_output)}  耗时 {elapsed:.1f}s")
        return _copy_srt_to_output(srt_file, srt_path, output_folder_zh, log)
    except Exception as e:
        log(f"!! 合并失败: {e}")
        try:
            if os.path.exists(temp_video_output):
                os.remove(temp_video_output)
        except OSError:
            pass
        return False


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


def _count_existing_segments(current_tmp_dir: str) -> int:
    """扫描临时目录中已存在的有效 output_*.wav 片段数量（非纯静音）。"""
    if not os.path.isdir(current_tmp_dir):
        return 0
    count = 0
    try:
        for f in os.listdir(current_tmp_dir):
            if f.startswith("output_") and f.endswith(".wav"):
                if is_valid_audio_segment(os.path.join(current_tmp_dir, f)):
                    count += 1
    except OSError:
        pass
    return count


def _check_task_skip_or_recover(subtitle_name, srt_file, output_folder_zh, output_audio_file,
                                 srt_path, log, existing_segments=0):
    """
    判断任务完成状态，返回 "skip"、"process" 或 "merge_failed"。

    判断顺序：
      1. 中配文件夹下已有最终视频和对应 SRT → 任务完成；若还残留同名 WAV，顺手删除
      2. 无视频但已有完整 WAV → 直接恢复合成视频；成功后删除 WAV
      3. 无视频无 WAV但有片段级进度 → 继续 TTS
      4. 无视频无 WAV无片段 → 全新处理
    """
    target_video_output = os.path.join(output_folder_zh, f"{subtitle_name}.mp4")

    # ── 第 1 级：最终视频已存在，任务才算真正完成 ──
    if os.path.exists(target_video_output):
        if not _copy_srt_to_output(srt_file, srt_path, output_folder_zh, log):
            return "merge_failed"
        log(f"  -> [完成] 最终视频已存在: {os.path.basename(target_video_output)}，跳过。")
        # 处理“视频已经合并成功，但程序刚好在删 WAV 前中断”的残留情况。
        _delete_completed_wav(output_audio_file, log)
        return "skip"

    # ── 第 2 级：只有 WAV → 说明 TTS/音频合并已完成，但视频封装尚未完成 ──
    if os.path.exists(output_audio_file):
        try:
            wav_size = os.path.getsize(output_audio_file)
        except OSError:
            wav_size = 0
        if wav_size < 1000:
            log(f"  !! [恢复] WAV 文件异常 ({wav_size} bytes)，视为无效，重新生成。")
            try:
                os.remove(output_audio_file)
            except OSError:
                pass
            return "process"

        # 额外验证：确保 WAV 可被读取（只读文件头，避免整段解码占用大量内存）
        try:
            info = sf.info(output_audio_file)
            duration_ms = int(round(info.frames / info.samplerate * 1000)) if info.samplerate > 0 else 0
            if duration_ms < 100:  # < 0.1 秒，视为无效占位
                log(f"  !! [恢复] WAV 时长异常 ({duration_ms}ms)，重新生成。")
                try:
                    os.remove(output_audio_file)
                except OSError:
                    pass
                return "process"
        except Exception:
            log("  !! [恢复] WAV 文件损坏无法读取，重新生成。")
            try:
                os.remove(output_audio_file)
            except OSError:
                pass
            return "process"

        log(f"  -> [恢复] 已找到配音 WAV ({wav_size / 1024:.0f} KB)，跳过 TTS，直接补做视频合并...")
        merge_ok = _find_and_merge_video(
            subtitle_name, output_audio_file, output_folder_zh, srt_path, log,
            srt_file=srt_file, cloud_source_path=srt_path,
        )
        if not merge_ok:
            log(f"!! [{subtitle_name}] 已有 WAV，但恢复合并视频失败；保留 WAV，避免重复配音。")
            return "merge_failed"

        # 只有最终视频确认生成成功后，才删除 WAV。
        _delete_completed_wav(output_audio_file, log)

        # 清理可能残留的参考音频（来自上一次未完成运行）
        ref_audio_dir = os.path.join(srt_path, "REF_AUDIO_PATH")
        stale_ref = os.path.join(ref_audio_dir, f"{subtitle_name}.wav")
        if os.path.exists(stale_ref):
            try:
                os.remove(stale_ref)
            except OSError:
                pass

        return "skip"

    # ── 第 3/4 级：无视频无 WAV，继续或开始 TTS ──
    if existing_segments <= 0:
        log("  -> [新建] 未检测到任何产物，开始完整流程。")
    return "process"


def _finalize_audio_video_job(subtitle_name: str,
                              srt_file: str,
                              parsed_subtitles: List[List],
                              current_tmp_dir: str,
                              output_audio_file: str,
                              local_status_file: str,
                              output_folder_zh: str,
                              srt_path: str,
                              main_audio_path: str,
                              audio_extracted_in_this_run: bool,
                              log: Callable[[str], None],
                              cloud_source_path: str = None) -> bool:
    """
    后台收尾任务：合并字幕片段 WAV、导出整条配音、合成视频、删除整条 WAV、清理临时目录。

    完成定义以“最终 MP4 已成功生成”为准。WAV 只是中间产物：
    - MP4 合并失败：保留 WAV，便于下次直接恢复合并，避免重复 TTS。
    - MP4 合并成功：立即删除 WAV。
    """
    task_completed_successfully = False
    cloud_sync_succeeded = True
    try:
        if config.ABORT_ALL:
            log(f"--- [{subtitle_name}] 已中止，跳过后台合并并清理临时文件 ---")
            return False

        log(f"--- [{subtitle_name}] 字幕片段后台合并中... ---")
        merged_audio = merge_audio(parsed_subtitles, current_tmp_dir)
        merged_audio.export(output_audio_file, format="wav")
        log(f"[{subtitle_name}] 输出临时配音 WAV: {os.path.basename(output_audio_file)}")

        # 后台合并现在会和下一文件 TTS 重叠，训练模型必须和预测/增量训练互斥。
        with model_lock:
            duration_predictor.train()

        merge_ok = _find_and_merge_video(
            subtitle_name, output_audio_file, output_folder_zh, srt_path, log,
            srt_file=srt_file, cloud_source_path=cloud_source_path,
        )
        if not merge_ok:
            # 不清状态、不删 WAV：下次启动会优先发现 WAV，直接重试视频合并。
            log(f"!! [{subtitle_name}] 视频合并未完成，保留 WAV 和任务进度供下次恢复。")
            return False

        # 最终视频成功生成后，任务才算完成。
        clear_status(local_status_file)
        task_completed_successfully = True

        # WAV 是中间产物，最终 MP4 成功后立即删除。
        _delete_completed_wav(output_audio_file, log)

        # ── 云存储模式：同步最终视频和对应 SRT，不再保留/同步 WAV ──
        if cloud_source_path:
            try:
                src_mp4 = os.path.join(output_folder_zh, f"{subtitle_name}.mp4")
                dst_mp4 = os.path.join(cloud_source_path, "中配", f"{subtitle_name}.mp4")
                if os.path.exists(src_mp4) and not os.path.exists(dst_mp4):
                    os.makedirs(os.path.dirname(dst_mp4), exist_ok=True)
                    shutil.copy2(src_mp4, dst_mp4)
                    log(f"[{subtitle_name}] 视频已同步到源目录")
                if not _copy_srt_to_output(
                    srt_file, cloud_source_path, os.path.join(cloud_source_path, "中配"), log
                ):
                    cloud_sync_succeeded = False
            except Exception as e:
                cloud_sync_succeeded = False
                log(f"!! [{subtitle_name}] 同步到源目录失败: {e}（最终视频仍在缓存中）")

        return cloud_sync_succeeded
    except Exception as e:
        log(f"!! [{subtitle_name}] 后台合并错误: {e}")
        return False
    finally:
        # 只有最终视频已经成功生成，或用户主动中断时才清理 tmp。
        # 若视频合并失败，则保留 tmp/status/WAV，供下次直接恢复。
        should_cleanup = task_completed_successfully or (
            config.ABORT_ALL and config.STOP_REASON != "ALL_SERVERS_DOWN"
        )
        if should_cleanup:
            if os.path.exists(current_tmp_dir):
                try:
                    shutil.rmtree(current_tmp_dir)
                except Exception as e:
                    log(f"!! [{subtitle_name}] 清理临时目录失败: {e}")

        # 参考音频是本次从视频提取的，必须等后台合并线程接管完成后再删，避免提前删除。
        if audio_extracted_in_this_run and os.path.exists(main_audio_path):
            try:
                os.remove(main_audio_path)
            except OSError:
                pass

        # 云存储模式：最终视频已成功同步回源目录后，删除缓存中的视频副本。
        if task_completed_successfully and cloud_source_path and cloud_sync_succeeded:
            cached_video = os.path.join(output_folder_zh, f"{subtitle_name}.mp4")
            if os.path.exists(cached_video):
                try:
                    os.remove(cached_video)
                except OSError:
                    pass


def process_srt_files(srt_paths, transformers_line: int = TRANSFORMERS_LINE,
                      max_workers: int = 1,
                      progress_callback=None,
                      server_configs: Dict[str, int] = None,
                      server_protocols: Dict[str, str] = None,
                      on_server_down=None,
                      on_all_down=None,
                      max_retries: int = None,
                      circuit_breaker_threshold: int = None,
                      background_merge: bool = False,
                      server_threads_per_server: int = None):
    """
    主处理函数。srt_paths 可以是单个字符串（兼容旧调用）或字符串列表。
    """

    # ── 保存本次流程参数，供后续重启使用 ──
    runtime_state.register_job_context(
        srt_paths=srt_paths,
        transformers_line=transformers_line,
        max_workers=max_workers,
        progress_callback=progress_callback,
        server_configs=server_configs,
        server_protocols=server_protocols,
        on_server_down=on_server_down,
        on_all_down=on_all_down,
        max_retries=max_retries,
        circuit_breaker_threshold=circuit_breaker_threshold,
        background_merge=background_merge,
        server_threads_per_server=server_threads_per_server,
    )
    runtime_state.mark_pipeline_started()
    reset_circuit_breaker_states()

    def log(msg):
        logger_log("MAIN", msg)

    # ── 兼容旧调用：None/单个字符串转为列表 ──
    if srt_paths is None:
        srt_paths = []
    if isinstance(srt_paths, str):
        srt_paths = [srt_paths]
    srt_paths = [os.path.normpath(p) for p in srt_paths]

    # ── 本地缓存逻辑 ──
    cache_manager = None
    original_paths = srt_paths.copy()
    use_cache = getattr(config, 'USE_LOCAL_CACHE', False)
    _cloud_base_map = {}  # local_path -> original_cloud_path（按需拉取用）

    # 检查是否需要使用本地缓存（任何路径看起来像云存储路径）
    if use_cache and any(is_cloud_path(p) for p in srt_paths):
        log("=== 检测到云存储路径，启用本地缓存优化 ===")
        cache_manager = LocalCacheManager(getattr(config, 'LOCAL_CACHE_DIR', None))

        try:
            # 智能骨架缓存：只复制 SRT + 小文件，大文件按需拉取
            local_paths = []
            for srt_path in srt_paths:
                if is_cloud_path(srt_path):
                    local_path = cache_manager.init_skeleton(srt_path)
                    local_paths.append(local_path)
                else:
                    local_paths.append(srt_path)

            # 使用本地路径进行处理
            srt_paths = local_paths
            for _lp, _op in zip(srt_paths, original_paths):
                if _lp != _op:
                    _cloud_base_map[_lp] = _op
            log(f"本地缓存准备完成，开始处理 {len(srt_paths)} 个文件夹")

        except Exception as e:
            log(f"!! 本地缓存初始化失败，回退到直接处理云端路径: {e}")
            cache_manager = None
            srt_paths = original_paths

    def _longpath(p: str) -> str:
        """Windows 下添加长路径前缀，避免 MAX_PATH 260 字符限制"""
        if os.name == 'nt' and not p.startswith('\\\\?\\'):
            return '\\\\?\\' + os.path.abspath(p)
        return p

    def _sanitize_server_threads(value) -> int:
        """多服务器单节点并发数，允许 1 或 2 线程。"""
        allowed = tuple(getattr(config, "ALLOWED_SERVER_THREADS_PER_SERVER", (1, 2)))
        default = int(getattr(config, "DEFAULT_SERVER_THREADS_PER_SERVER", 1))
        if value is None:
            value = getattr(config, "SERVER_THREADS_PER_SERVER", default)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        if value not in allowed:
            value = default
        return value

    # ── 判断模式 ──────────────────────────────
    use_multi_server = bool(server_configs)
    if use_multi_server:
        selected_server_threads = _sanitize_server_threads(server_threads_per_server)
        server_configs = {
            str(u).strip(): selected_server_threads
            for u in server_configs.keys()
            if str(u).strip()
        }
        server_protocols = server_protocols or {}
        summary = ", ".join(
            f"{u.rstrip('/').split(':')[-1]}[{server_protocols.get(u, 'voxcpm2')}]×{n}"
            for u, n in server_configs.items()
        )
        log(f"[模式] 多服务器单队列分发：{summary} | 每服务器线程数={selected_server_threads}")
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

    # ── 文件级进度概览 ──
    # 云存储模式下，先从源目录确定完成状态（骨架缓存不复制大文件）
    if total_files > 0:
        _files_done = 0
        _files_partial = 0
        _files_new = 0
        for _scan_path, _scan_file in all_folder_files:
            _scan_name = os.path.splitext(_scan_file)[0]
            # 确定要检查的目录：优先源目录（云存储模式下大文件在源目录）
            _check_paths = [_scan_path]
            if _scan_path in _cloud_base_map:
                _check_paths.insert(0, _cloud_base_map[_scan_path])
            # 完成状态只以最终视频为准；只有 WAV 说明“配音已完成、等待/恢复视频合并”。
            _found_video = False
            _found_wav = False
            for _cp in _check_paths:
                _v = os.path.join(_cp, "中配", f"{_scan_name}.mp4")
                _w = os.path.join(_cp, f"{_scan_name}.wav")
                if os.path.exists(_v):
                    _found_video = True
                    break
                if os.path.exists(_w):
                    _found_wav = True
            if _found_video:
                _files_done += 1
            elif _found_wav:
                _files_partial += 1
            else:
                # 检查缓存目录的 tmp 中是否有片段进度
                _scan_tmp = os.path.join(_scan_path, "tmp", hashlib.md5(_scan_name.encode('utf-8')).hexdigest())
                if _count_existing_segments(_scan_tmp) > 0:
                    _files_partial += 1
                else:
                    _files_new += 1
        log(f"[进度概览] 共 {total_files} 个文件: "
            f"已完成 {_files_done} 个 | 进行中 {_files_partial} 个 | 待处理 {_files_new} 个")

    global_file_idx = 0
    processed_basenames = set()
    processing_failures = []  # [(subtitle_name, reason), ...]
    pipeline_exception = None

    # ── 多服务器模式：全局初始化调度器（生命周期覆盖所有文件）──
    dispatcher = None
    if use_multi_server:
        def _on_server_down(url, stats):
            log(f"⚠️ 服务器下线: {url} | {stats}")
            runtime_state.mark_server_down(url, stats)
            if on_server_down:
                on_server_down(url, stats)

        def _on_all_down():
            log("🛑 所有服务器均已熔断，主流程停止，等待外部请求重新启动整条流程")
            setattr(config, 'ABORT_ALL', True)
            config.STOP_REASON = "ALL_SERVERS_DOWN"
            runtime_state.mark_all_servers_down("ALL_SERVERS_DOWN")
            if on_all_down:
                on_all_down()

        def _on_pool_changed(snapshot):
            # 运行中增删节点后，同步日志、状态接口与重启上下文。
            log(f"[服务器池] {snapshot.get('state')} | 节点 {snapshot.get('server_count')} "
                f"| 线程 {snapshot.get('threads')} | 队列 {snapshot.get('queue_size')}")
            runtime_state.update_job_servers(
                snapshot.get("server_configs", {}),
                snapshot.get("server_protocols", {}),
            )

        dispatcher = MultiServerDispatcher(
            server_configs=server_configs,
            server_protocols=server_protocols,
            max_retries=max_retries if max_retries is not None else config.MAX_RETRIES,
            on_server_down=_on_server_down,
            on_all_down=_on_all_down,
            on_pool_changed=_on_pool_changed,
            circuit_breaker_threshold=circuit_breaker_threshold if circuit_breaker_threshold is not None else getattr(config, 'CIRCUIT_BREAKER_THRESHOLD', 6),
        )
        dispatcher.start()

    # 文件级合并/封装流水线：只开 1 个后台收尾线程，避免多个 ffmpeg/pydub 同时抢磁盘和内存。
    # 主线程在每个文件 TTS 片段完成后提交后台合并，然后立刻进入下一个文件的 TTS。
    finalize_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="FINALIZE"
    )
    finalize_futures = []

    def _submit_finalize_job(subtitle_name: str,
                             srt_file: str,
                             parsed_subtitles: List[List],
                             current_tmp_dir: str,
                             output_audio_file: str,
                             local_status_file: str,
                             output_folder_zh: str,
                             srt_path: str,
                             main_audio_path: str,
                             audio_extracted_in_this_run: bool,
                             cloud_source_path: str = None):
        log(f"--- [{subtitle_name}] TTS 片段已完成，后台合并音频/视频；主流程继续下一个文件 ---")
        future = finalize_executor.submit(
            _finalize_audio_video_job,
            subtitle_name,
            srt_file,
            parsed_subtitles,
            current_tmp_dir,
            output_audio_file,
            local_status_file,
            output_folder_zh,
            srt_path,
            main_audio_path,
            audio_extracted_in_this_run,
            log,
            cloud_source_path,
        )
        finalize_futures.append((subtitle_name, future))

    def _wait_finalize_jobs() -> bool:
        if not finalize_futures:
            return True
        log(f"--- 等待 {len(finalize_futures)} 个后台合并任务完成 ---")
        all_ok = True
        while finalize_futures:
            subtitle_name, future = finalize_futures.pop(0)
            try:
                ok = future.result()
                if not ok and not config.ABORT_ALL:
                    all_ok = False
                    log(f"!! [{subtitle_name}] 后台合并未成功，临时目录已保留，便于下次恢复。")
            except Exception as e:
                all_ok = False
                log(f"!! [{subtitle_name}] 后台合并线程异常: {e}")
        return all_ok

    try:
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

            # ── 云存储模式下，先从源目录确定完成状态 ──
            # 判断顺序始终是：最终视频 > 完整 WAV > 片段进度。
            # 若 WAV 在缓存里、源视频在云端，则恢复合并时直接从云端源目录读取原视频。
            _check_folder_zh = output_folder_zh
            _check_audio = output_audio_file
            _check_srt_path = srt_path
            if srt_path in _cloud_base_map:
                _orig = _cloud_base_map[srt_path]
                _orig_folder_zh = os.path.join(_orig, "中配")
                _orig_video = os.path.join(_orig_folder_zh, f"{subtitle_name}.mp4")
                _orig_wav = os.path.join(_orig, f"{subtitle_name}.wav")
                _check_srt_path = _orig

                if os.path.exists(_orig_video):
                    # 云端最终视频是权威完成状态；若本地仍残留 WAV，也一并清掉。
                    _check_folder_zh = _orig_folder_zh
                    _check_audio = _orig_wav if os.path.exists(_orig_wav) else output_audio_file
                elif os.path.exists(_orig_wav):
                    # 旧版本可能把 WAV 同步到了云端：直接在云端恢复合并，成功后删除该 WAV。
                    _check_folder_zh = _orig_folder_zh
                    _check_audio = _orig_wav
                # 否则继续检查本地缓存中的 MP4/WAV；恢复合并时源视频仍从 _orig 查找。

            # ── 提前检测片段级进度，用于日志显示 ──
            _precomputed_safe_dir = hashlib.md5(subtitle_name.encode('utf-8')).hexdigest()
            _precomputed_tmp_dir = os.path.join(srt_path, "tmp", _precomputed_safe_dir)
            _existing_segment_count = _count_existing_segments(_precomputed_tmp_dir)

            skip_result = _check_task_skip_or_recover(
                subtitle_name, srt_file, _check_folder_zh, _check_audio, _check_srt_path, log,
                existing_segments=_existing_segment_count
            )
            if skip_result == "merge_failed":
                processing_failures.append((subtitle_name, "补全最终视频或字幕失败；可在下次运行时重试"))
                global_file_idx += 1
                continue
            if skip_result == "skip":
                # 云存储模式下可能同时存在“源目录 WAV”和“缓存 WAV”，最终视频既然已完成，两边都清理。
                if os.path.normcase(os.path.abspath(_check_audio)) != os.path.normcase(os.path.abspath(output_audio_file)):
                    _delete_completed_wav(output_audio_file, log)
                # 清理前任遗留的 stale tmp 目录
                stale_tmp = os.path.join(srt_path, "tmp", hashlib.md5(subtitle_name.encode('utf-8')).hexdigest())
                if os.path.exists(stale_tmp):
                    try:
                        shutil.rmtree(stale_tmp)
                    except Exception:
                        pass
                global_file_idx += 1
                continue

            # 如果有片段级进度，替换 [新建] 消息为更清晰的进度显示
            if _existing_segment_count > 0:
                log(f"  -> [进度] 检测到 {_existing_segment_count} 个已完成片段，将跳过这些片段继续处理。")

            # ── 1. 优先检查与解析字幕，确保字幕有效后再提取音频，避免浪费 I/O ──
            try:
                with open(os.path.join(srt_path, srt_file), "r", encoding="utf-8") as file:
                    file_content = file.read()
            except Exception as e:
                log(f"读取字幕文件失败: {e}")
                processing_failures.append((subtitle_name, f"读取字幕失败: {e}"))
                global_file_idx += 1
                continue

            parsed_subtitles = parse_subtitles(file_content, transformers_line)
            if not parsed_subtitles:
                log(f"无有效字幕，跳过: {srt_file}")
                processing_failures.append((subtitle_name, "无有效字幕"))
                global_file_idx += 1
                continue

            # 合并连续字幕，减少 API 调用次数
            original_count = len(parsed_subtitles)
            parsed_subtitles = merge_contiguous_subtitles(parsed_subtitles, config.MERGE_MAX_CHARS)
            merged_count = len(parsed_subtitles)
            if merged_count < original_count:
                log(f"  字幕合并: {original_count} 条 → {merged_count} 条")

            # ── 2. 字幕检查通过后，查找视频并提取参考音频 ──
            main_audio_path = os.path.join(ref_audio_path, subtitle_name + ".wav")
            audio_extracted_in_this_run = False
            found_video_path = None

            if cache_manager and srt_path in _cloud_base_map:
                _cloud = _cloud_base_map[srt_path]

                # 优先拉取参考音频（如果本地不存在但云端有）
                _wav_rel = os.path.relpath(main_audio_path, srt_path)
                if not os.path.exists(main_audio_path):
                    _cloud_wav = os.path.join(_cloud, _wav_rel)
                    if os.path.exists(_cloud_wav):
                        cache_manager.ensure_file(_cloud, _wav_rel)

                # 查找源视频（优先使用本地已缓存视频；若本地没有则直接引用云端视频，无需在 TTS 前全量下载大视频）
                local_vid = _find_video_path(subtitle_name, srt_path)
                if local_vid:
                    found_video_path = local_vid
                else:
                    for _ext in (".mp4", ".ts", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm"):
                        _vid_cloud = os.path.join(_cloud, f"{subtitle_name}{_ext}")
                        if os.path.exists(_vid_cloud):
                            found_video_path = _vid_cloud
                            break
            else:
                found_video_path = _find_video_path(subtitle_name, srt_path)

            if not os.path.exists(main_audio_path):
                if found_video_path:
                    log(f"正在从视频提取参考音频: {os.path.basename(found_video_path)}")
                    if extract_single_audio(found_video_path, main_audio_path):
                        audio_extracted_in_this_run = True
                    else:
                        log(f"!! ffmpeg 提取音频失败，跳过此文件。")
                        processing_failures.append((subtitle_name, "ffmpeg 提取参考音频失败"))
                        global_file_idx += 1
                        continue
                else:
                    log(f"!! 警告: 在 '{srt_path}' 下找不到与 '{subtitle_name}' 匹配的视频文件，跳过。")
                    processing_failures.append((subtitle_name, "找不到匹配的源视频文件"))
                    global_file_idx += 1
                    continue

            if not os.path.exists(main_audio_path):
                processing_failures.append((subtitle_name, "参考音频不存在"))
                global_file_idx += 1
                continue

            log(f"开始生成: {srt_file}")
            safe_dir_name = hashlib.md5(subtitle_name.encode('utf-8')).hexdigest()
            dynamic_tmp_root = os.path.join(srt_path, "tmp")
            current_tmp_dir = _longpath(os.path.join(dynamic_tmp_root, safe_dir_name))
            os.makedirs(current_tmp_dir, exist_ok=True)
            local_status_file = os.path.join(current_tmp_dir, "status.json")

            task_completed_successfully = False
            finalize_submitted = False

            try:
                completed_indices = load_status(local_status_file, srt_file)
                all_indices = list(range(len(parsed_subtitles)))

                # 校验从 status.json 加载的已完成索引：若对应文件不存在或全静音，则剔除
                purged_silent_count = 0
                valid_completed_indices = set()
                for idx in completed_indices:
                    wav_path = os.path.join(current_tmp_dir, f"output_{idx}.wav")
                    if is_valid_audio_segment(wav_path):
                        valid_completed_indices.add(idx)
                    else:
                        purged_silent_count += 1
                        if os.path.exists(wav_path):
                            try:
                                os.remove(wav_path)
                            except OSError:
                                pass
                completed_indices = valid_completed_indices
                if purged_silent_count > 0:
                    log(f"  -> [恢复系统] 检测并剔除了 {purged_silent_count} 个损坏或纯静音片段，将重新生成")

                recovered_count = 0
                for idx in all_indices:
                    if idx not in completed_indices:
                        potential_wav = os.path.join(current_tmp_dir, f"output_{idx}.wav")
                        if is_valid_audio_segment(potential_wav):
                            completed_indices.add(idx)
                            recovered_count += 1
                        elif os.path.exists(potential_wav):
                            # 大小异常或纯静音文件直接删除，避免遗留
                            try:
                                os.remove(potential_wav)
                            except OSError:
                                pass
                if recovered_count > 0 or purged_silent_count > 0:
                    total_tasks_for_log = len(all_indices)
                    done_count = len(completed_indices)
                    remaining = total_tasks_for_log - done_count
                    log(f"  -> [恢复系统] 扫描到 {recovered_count} 个已存在有效片段 | "
                        f"进度: {done_count}/{total_tasks_for_log} ({done_count * 100 // total_tasks_for_log}%) | "
                        f"剩余: {remaining} 个")
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

                        completed_count = len(completed_indices)
                        file_start_time = time.time()
                        session_processed_count = 0

                        # ── CPU Worker 注入：rubberband/pydub + 状态保存 ──
                        def post_process_fn(idx, raw_path, server_url):
                            if config.ABORT_ALL or (dispatcher and dispatcher.all_servers_down):
                                return
                            params = task_params_map.get(idx)
                            if params is None:
                                return
                            # 运行中新增的节点不在启动快照里，协议必须实时向池查询。
                            proto = dispatcher.get_protocol(server_url) if dispatcher else "voxcpm2"
                            # IndexTTS 1.5 服务端按 speed 变速生成；VoxCPM 2.0 无 speed 参数，实际为 1.0
                            effective_speed = params["speed"] if proto == "indextts_15" else 1.0
                            post_process_audio_task(
                                idx, raw_path,
                                effective_speed, params["text"],
                                params["target_duration_s"],
                                current_tmp_dir
                            )
                            with status_lock:
                                completed_indices.add(idx)
                                save_status(local_status_file, srt_file, completed_indices)

                        # ── CPU Worker 注入：进度日志 ──
                        def done_callback(idx, server_url):
                            nonlocal completed_count, session_processed_count
                            if config.ABORT_ALL or (dispatcher and dispatcher.all_servers_down):
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
                        gen = task_iterator()
                        try:
                            dispatcher.run_feeder(
                                task_iter=gen,
                                post_process_fn=post_process_fn,
                                done_callback=done_callback,
                                abort_flag_fn=lambda: config.ABORT_ALL,
                            )
                        finally:
                            gen.close()

                    # ════════════════════════════════════════
                    # 单服务器模式（原有逻辑，完整保留）
                    # ════════════════════════════════════════
                    else:
                        log(f"--- 剩余 {len(uncompleted_indices)} 个任务 (并发数: {max_workers}) ---")

                        pbar = None
                        if not progress_callback and USE_TQDM_PROGRESS_BAR:
                            pbar = tqdm(total=len(uncompleted_indices), desc=f"处理 {subtitle_name}")

                        completed_count = len(completed_indices)
                        file_start_time = time.time()
                        session_processed_count = 0

                        # [OOM防护]：单机模式一次性载入主音频，避免每个线程重复读取
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
                        finally:
                            # [OOM防护]：单机模式任务队列派发并执行完毕后，强制清理内存
                            if 'loaded_main_audio' in locals() and isinstance(loaded_main_audio, AudioSegment):
                                del loaded_main_audio
                                gc.collect()

                        if pbar: pbar.close()

                if config.ABORT_ALL: break

                # ── 云存储模式下，确定源目录路径用于结果同步 ──
                _cloud_src = _cloud_base_map.get(srt_path) if cache_manager else None

                # ── 合并：根据设置决定后台异步还是主线程同步 ──
                if background_merge:
                    _submit_finalize_job(
                        subtitle_name=subtitle_name,
                        srt_file=srt_file,
                        parsed_subtitles=parsed_subtitles,
                        current_tmp_dir=current_tmp_dir,
                        output_audio_file=output_audio_file,
                        local_status_file=local_status_file,
                        output_folder_zh=output_folder_zh,
                        srt_path=srt_path,
                        main_audio_path=main_audio_path,
                        audio_extracted_in_this_run=audio_extracted_in_this_run,
                        cloud_source_path=_cloud_src,
                    )
                    finalize_submitted = True
                else:
                    log(f"--- [{subtitle_name}] 同步合并音频/视频... ---")
                    ok = _finalize_audio_video_job(
                        subtitle_name=subtitle_name,
                        srt_file=srt_file,
                        parsed_subtitles=parsed_subtitles,
                        current_tmp_dir=current_tmp_dir,
                        output_audio_file=output_audio_file,
                        local_status_file=local_status_file,
                        output_folder_zh=output_folder_zh,
                        srt_path=srt_path,
                        main_audio_path=main_audio_path,
                        audio_extracted_in_this_run=audio_extracted_in_this_run,
                        log=log,
                        cloud_source_path=_cloud_src,
                    )
                    if not ok:
                        log(f"!! [{subtitle_name}] 同步合并失败")
                        processing_failures.append((subtitle_name, "同步合并或云端结果同步失败"))

                global_file_idx += 1

            finally:
                # 已提交后台合并的文件，由后台收尾线程负责清理 tmp 和本次提取的参考音频。
                # 未提交后台合并的文件，仍按原逻辑在这里清理，避免异常路径泄漏资源。
                if not finalize_submitted:
                    should_cleanup = task_completed_successfully or (
                        config.ABORT_ALL and config.STOP_REASON != "ALL_SERVERS_DOWN"
                    )
                    if should_cleanup:
                        if os.path.exists(current_tmp_dir):
                            try:
                                shutil.rmtree(current_tmp_dir)
                            except Exception as e:
                                log(f"!! 清理临时目录失败: {e}")
                    if audio_extracted_in_this_run and os.path.exists(main_audio_path):
                        try:
                            os.remove(main_audio_path)
                        except OSError:
                            pass

        finalize_all_ok = _wait_finalize_jobs()

        pipeline_ok = (not config.ABORT_ALL) and finalize_all_ok and not processing_failures

        if progress_callback:
            progress_callback.update_file_progress(total_files)
        if config.ABORT_ALL:
            msg = "\n=== 任务已强制停止 ==="
        elif processing_failures:
            msg = f"\n=== 任务结束，但有 {len(processing_failures)} 个文件处理失败 ==="
        elif finalize_all_ok:
            msg = "\n=== 所有任务处理完毕 ==="
        else:
            msg = "\n=== TTS 已完成，但部分后台合并失败 ==="
        log(msg)

        if processing_failures:
            for _name, _reason in processing_failures:
                log(f"  !! 失败: {_name} | {_reason}")

        for _srt_path in srt_paths:
            ref_ap = os.path.join(_srt_path, "REF_AUDIO_PATH")
            if os.path.exists(ref_ap) and not os.listdir(ref_ap):
                try:
                    os.rmdir(ref_ap)
                except Exception:
                    pass

        # 云存储结果已经由 _finalize_audio_video_job 按“单个输出文件”精确同步回源目录。
        # 这里禁止再递归同步 *.mp4：
        # 根目录中的 MP4 是源视频，缓存复制中断时可能是半成品，
        # 递归同步会把损坏缓存反向覆盖到 Google Drive 原视频。
        if cache_manager:
            cache_manager.print_stats()

        # 只有整条流程真正成功时才清理缓存。
        # 任一文件失败时保留缓存，方便恢复与排查。
        if cache_manager and pipeline_ok:
            try:
                cache_manager.cleanup()
                log("=== 本地缓存已清理 ===")
            except Exception as e:
                log(f"!! 清理本地缓存失败: {e}")

        return pipeline_ok

    except Exception as e:
        pipeline_exception = e
        log(f"!!! 主处理流程发生严重崩溃: {e}")
        import traceback
        log(traceback.format_exc())
        return False

    finally:
        # ── 全局关闭调度器 ──
        if dispatcher:
            dispatcher.stop()
        try:
            _wait_finalize_jobs()
        except Exception:
            pass
        finalize_executor.shutdown(wait=True)
        runtime_state.mark_pipeline_stopped(config.STOP_REASON)

        # ── 异常情况下的缓存提示 ──
        # 只有真的抛出了未处理异常才打印；正常 return 不再误报“发生异常”。
        if cache_manager and pipeline_exception is not None:
            log(f"!! 处理过程中发生异常，本地缓存已保留以便调试")
            log(f"!! 本地缓存路径: {cache_manager.cache_dir}")
            log(f"!! 如需手动清理，请删除上述目录")


def restart_last_pipeline():
    """重启上次的处理流程（供 control_server 调用）。"""
    ctx = runtime_state.get_last_job_context()
    if not ctx:
        raise RuntimeError("没有可重启的流程上下文")
    # 运行中把节点删光后，上下文里的池可能为空；此时重启会退化成单服务器模式。
    if "server_configs" in ctx and not ctx.get("server_configs"):
        raise RuntimeError("服务器池为空，请先添加至少一台服务器再重启流程")
    process_srt_files(**ctx)


def _run_cli_entry():
    import control_server
    control_server.set_process_runner(restart_last_pipeline)
    control_server.start_control_server()
    ok = process_srt_files(SRT_PATH, TRANSFORMERS_LINE)

    # 如果是“所有服务器熔断”，原逻辑会让主线程返回，daemon 控制服务器也随进程一起退出；
    # 这看起来就像无缘无故闪退。这里保持控制服务器存活，等待外部 /api/pipeline/restart。
    if getattr(config, "STOP_REASON", None) == "ALL_SERVERS_DOWN":
        logger_log("CONTROL", "主流程因所有服务器熔断而停止；控制服务器继续运行，等待 POST /api/pipeline/restart")
        while True:
            time.sleep(60)
    return ok


if __name__ == '__main__':
    try:
        _run_cli_entry()
    except BaseException as e:
        import traceback
        logger_log("FATAL", "主入口未捕获异常:\n" + traceback.format_exc())
        raise
