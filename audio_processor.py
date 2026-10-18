import os
import shutil
import subprocess
import sys
from typing import Optional, List, Union
import numpy as np


def _safe_print(message: str) -> None:
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        if getattr(stream, "closed", False):
            return
        stream.write(str(message) + "\n")
        stream.flush()
    except Exception:
        pass

# ==============================================================================
# 【核弹级防弹窗补丁】 START
# 这段代码必须放在 import pydub 或 import pyrubberband 之前执行
# 它会拦截 Python 所有的子进程调用，强制隐藏窗口
# ==============================================================================
if os.name == 'nt':  # 仅限 Windows
    # 保存原始的 Popen 类
    _original_Popen = subprocess.Popen


    class QuietPopen(_original_Popen):
        def __init__(self, *args, **kwargs):
            # 1. 强制添加 CREATE_NO_WINDOW 标志
            if 'creationflags' not in kwargs:
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            # 2. 额外保险：设置 STARTUPINFO 隐藏窗口
            if 'startupinfo' not in kwargs:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                kwargs['startupinfo'] = startupinfo

            super().__init__(*args, **kwargs)


    # 用我们的修改版覆盖系统的 Popen
    subprocess.Popen = QuietPopen
# ==============================================================================
# 【核弹级防弹窗补丁】 END
# ==============================================================================

# 必须在补丁应用之后再导入这些库
from pydub import AudioSegment
from pydub import AudioSegment
import soundfile as sf
import pyrubberband as rb

# === CONFIG RUBBERBAND PATH ===
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RUBBERBAND_DIR = os.path.join(PROJECT_ROOT, "rubberband")
if os.path.exists(RUBBERBAND_DIR):
    os.environ["PATH"] += os.pathsep + RUBBERBAND_DIR
    exe_path = os.path.join(RUBBERBAND_DIR, "rubberband.exe")
    if not os.path.exists(exe_path):
        _safe_print(f"Warning: rubberband directory found but rubberband.exe is missing at {exe_path}")
else:
    _safe_print(f"Warning: Local rubberband directory not found at {RUBBERBAND_DIR}")

from config import USE_TQDM_PROGRESS_BAR, MIN_RUBBERBAND_RATE
from logger import log as logger_log
from utils import check_ffmpeg, time_str_to_seconds


def _target_samples(duration_s: float, sample_rate: int) -> int:
    return max(1, int(round(max(0.0, duration_s) * sample_rate)))


def _fit_array_to_samples(y: np.ndarray, target_samples: int) -> np.ndarray:
    current_samples = len(y)
    if current_samples == target_samples:
        return y
    if current_samples > target_samples:
        return y[:target_samples]

    pad_samples = target_samples - current_samples
    shape = (pad_samples, y.shape[1]) if y.ndim > 1 else (pad_samples,)
    return np.concatenate((y, np.zeros(shape, dtype=y.dtype)))


def _time_str_to_ms(time_str: str) -> int:
    return int(round(time_str_to_seconds(time_str) * 1000))


def _subtitle_ms_bounds(subtitle: List) -> tuple[int, int]:
    start_ms = _time_str_to_ms(subtitle[0])
    end_ms = max(start_ms, _time_str_to_ms(subtitle[1]))
    return start_ms, end_ms


def _fit_audio_segment(audio_segment: AudioSegment, target_duration_ms: int) -> AudioSegment:
    target_duration_ms = max(0, int(round(target_duration_ms)))
    current_duration_ms = len(audio_segment)
    if current_duration_ms == target_duration_ms:
        return audio_segment
    if current_duration_ms > target_duration_ms:
        return audio_segment[:target_duration_ms]
    return audio_segment + AudioSegment.silent(duration=target_duration_ms - current_duration_ms)


def inspect_audio_segments(parsed_subtitles: List[List], current_tmp_dir: str,
                           duration_tolerance_ms: int = 80) -> List[dict]:
    issues = []
    for i, subtitle in enumerate(parsed_subtitles):
        start_ms, end_ms = _subtitle_ms_bounds(subtitle)
        target_duration_ms = end_ms - start_ms
        segment_file = os.path.join(current_tmp_dir, f"output_{i}.wav")
        issue_base = {
            "index": i,
            "path": segment_file,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "target_ms": target_duration_ms,
        }

        if not os.path.exists(segment_file):
            issues.append({**issue_base, "type": "missing"})
            continue

        try:
            size_bytes = os.path.getsize(segment_file)
        except OSError as e:
            issues.append({**issue_base, "type": "stat_error", "error": str(e)})
            continue

        issue_base = {**issue_base, "size_bytes": size_bytes}
        if size_bytes < 1000:
            issues.append({**issue_base, "type": "too_small"})
            continue

        try:
            audio_segment = AudioSegment.from_file(segment_file)
        except Exception as e:
            issues.append({**issue_base, "type": "unreadable", "error": str(e)})
            continue

        actual_ms = len(audio_segment)
        if actual_ms <= 0 and target_duration_ms > 0:
            issues.append({
                **issue_base,
                "type": "empty_audio",
                "actual_ms": actual_ms,
                "delta_ms": -target_duration_ms,
            })
            continue

        delta_ms = actual_ms - target_duration_ms
        if abs(delta_ms) > duration_tolerance_ms:
            issues.append({
                **issue_base,
                "type": "duration_mismatch",
                "actual_ms": actual_ms,
                "delta_ms": delta_ms,
            })

    return issues


def merge_single_audio_video(video_file, audio_file, output_file):
    """
    将单个音频文件和视频文件合并成一个MP4视频文件。
    返回 (True, None) 成功，(False, error_msg) 失败。
    """
    if not os.path.exists(video_file):
        return False, f"视频文件不存在: {video_file}"
    if not os.path.exists(audio_file):
        return False, f"音频文件不存在: {audio_file}"
    if not check_ffmpeg():
        return False, "ffmpeg 未安装或不可用"
    output_dir = os.path.dirname(output_file)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-fflags", "+genpts",
        "-i", video_file,
        "-i", audio_file,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "2",
        "-shortest",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y", output_file
    ]
    try:
        process = subprocess.run(cmd, capture_output=True, check=True, text=True, encoding='utf-8')
        return True, None
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else "(无 stderr 输出)"
        return False, f"ffmpeg 合并失败 (exit {e.returncode})\n  命令: {' '.join(cmd)}\n  stderr: {stderr}"
    except Exception as e:
        return False, f"合并时发生意外错误: {e}"


def extract_single_audio(video_path, audio_output_path):
    """
    从单个视频文件中提取音频
    """
    try:
        if os.path.exists(audio_output_path):
            return True

        # 确保目录存在
        os.makedirs(os.path.dirname(audio_output_path), exist_ok=True)

        ffmpeg_cmd = [
            'ffmpeg', '-i', video_path, '-vn', '-acodec', 'pcm_s16le',
            '-ar', '44100', '-ac', '2', audio_output_path, '-y'
        ]

        # 全局补丁会自动处理这里的弹窗
        result = subprocess.run(ffmpeg_cmd, capture_output=True, encoding='utf-8', errors='ignore')

        if result.returncode == 0:
            if not USE_TQDM_PROGRESS_BAR:
                _safe_print(f"[提取成功] 临时参考音频: {os.path.basename(audio_output_path)}")
            return True
        else:
            _safe_print(f"[提取失败] 无法从 '{os.path.basename(video_path)}' 提取音频: {result.stderr}")
            return False
    except Exception as e:
        _safe_print(f"[提取错误] {e}")
        return False


def crop_audio(start_time_str: str, end_time_str: str, input_file_or_audio: Union[str, AudioSegment]) -> Optional[AudioSegment]:
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
        _safe_print(f"!! 裁剪或读取音频时发生错误: {e}")
        return None


def adjust_duration_with_rubberband(input_path: str, output_path: str, target_duration_s: float):
    try:
        y, sr = sf.read(input_path)
        target_samples = _target_samples(target_duration_s, sr)
        target_duration_s = target_samples / sr
        current_duration_s = len(y) / sr
        if current_duration_s < 0.01:
            shape = (target_samples, y.shape[1]) if y.ndim > 1 else (target_samples,)
            sf.write(output_path, np.zeros(shape, dtype=y.dtype), sr)
            return
        rate = current_duration_s / target_duration_s
        if abs(rate - 1.0) < 0.001:
            sf.write(output_path, _fit_array_to_samples(y, target_samples), sr)
            return
        if not USE_TQDM_PROGRESS_BAR:
            _safe_print(f"    -> 需调节时长：从 {current_duration_s:.2f}s 到 {target_duration_s:.2f}s (原始速率: {rate:.2f}x)")
        if rate < MIN_RUBBERBAND_RATE:
            if not USE_TQDM_PROGRESS_BAR:
                _safe_print(
                    f"    -> 警告: 速率 {rate:.2f}x 低于下限 {MIN_RUBBERBAND_RATE}x。将以 {MIN_RUBBERBAND_RATE}x 速度拉伸并补充静音。")

            # rb.time_stretch 在后台调用 rubberband.exe
            # 由于我们的补丁，这里再也不会弹窗了！
            stretched_y = rb.time_stretch(y, sr, MIN_RUBBERBAND_RATE)

            stretched_duration_s = len(stretched_y) / sr
            silence_to_add_s = target_duration_s - stretched_duration_s
            final_y = stretched_y
            if silence_to_add_s > 0.001:
                silence_samples = int(silence_to_add_s * sr)
                shape = (silence_samples, y.shape[1]) if y.ndim > 1 else (silence_samples,)
                final_y = np.concatenate((stretched_y, np.zeros(shape, dtype=y.dtype)))
            sf.write(output_path, _fit_array_to_samples(final_y, target_samples), sr)
        else:
            stretched_y = rb.time_stretch(y, sr, rate)
            sf.write(output_path, _fit_array_to_samples(stretched_y, target_samples), sr)
    except Exception as e:
        _safe_print(f"!! 时长调节时发生严重错误: {e}")
        shutil.copy(input_path, output_path)


def merge_audio(parsed_subtitles: List[List], current_tmp_dir: str) -> AudioSegment:
    merged_audio = AudioSegment.empty()
    if not parsed_subtitles: return merged_audio
    total = len(parsed_subtitles)
    logger_log("MERGE", f"开始合并音频片段 ({total} 段)")
    for i, subtitle in enumerate(parsed_subtitles):
        start_ms, end_ms = _subtitle_ms_bounds(subtitle)
        target_duration_ms = end_ms - start_ms
        if len(merged_audio) < start_ms:
            merged_audio += AudioSegment.silent(duration=start_ms - len(merged_audio))
        segment_file = os.path.join(current_tmp_dir, f"output_{i}.wav")
        try:
            audio_segment = AudioSegment.from_file(segment_file)
            merged_audio += _fit_audio_segment(audio_segment, target_duration_ms)
        except Exception as e:
            logger_log("MERGE", f"!! 无法读取 {segment_file} ({e})，用静音代替。")
            merged_audio += AudioSegment.silent(duration=target_duration_ms)
        # 每 5 段或最后一段汇报进度
        if (i + 1) % 5 == 0 or i + 1 == total:
            logger_log("MERGE", f"合并进度: {i + 1}/{total} ({(i + 1) * 100 // total}%)")
    logger_log("MERGE", f"合并完成，总时长 {len(merged_audio) / 1000:.1f}s")
    return merged_audio
