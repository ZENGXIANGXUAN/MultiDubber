import os
import shutil
import subprocess
import sys
from typing import Optional, List
import numpy as np

from logger import log as _log

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
import soundfile as sf
import pyrubberband as rb

# === CONFIG RUBBERBAND PATH ===
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RUBBERBAND_DIR = os.path.join(PROJECT_ROOT, "rubberband")
if os.path.exists(RUBBERBAND_DIR):
    os.environ["PATH"] += os.pathsep + RUBBERBAND_DIR
    exe_path = os.path.join(RUBBERBAND_DIR, "rubberband.exe")
    if not os.path.exists(exe_path):
        _log("WARN", f"rubberband directory found but rubberband.exe is missing at {exe_path}")
else:
    _log("WARN", f"Local rubberband directory not found at {RUBBERBAND_DIR}")

from config import USE_TQDM_PROGRESS_BAR, MIN_RUBBERBAND_RATE
from utils import check_ffmpeg, time_str_to_seconds


def merge_single_audio_video(video_file, audio_file, output_file):
    """
    将单个音频文件和视频文件合并成一个MP4视频文件。
    """
    if not os.path.exists(video_file):
        _log("ERROR", f"视频文件 '{video_file}' 不存在。")
        return False
    if not os.path.exists(audio_file):
        _log("ERROR", f"音频文件 '{audio_file}' 不存在。")
        return False
    if not check_ffmpeg():
        return False
    output_dir = os.path.dirname(output_file)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "ffmpeg", "-i", video_file, "-i", audio_file, "-c:v", "copy", "-c:a", "aac",
        "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-y", output_file
    ]
    try:
        # 由于上面已经应用了全局补丁，这里其实不需要手动加 flags 了，
        # 但为了双重保险，保留也可以。全局补丁会覆盖它。
        process = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True, text=True, encoding='utf-8',
            timeout=600
        )
        _log("INFO", f"  成功合并到 '{output_file}'。")
        return True
    except subprocess.CalledProcessError as e:
        _log("ERROR", "  错误：合并失败。")
        _log("ERROR", f"  ffmpeg 命令: {' '.join(cmd)}")
        _log("ERROR", f"  ffmpeg 返回码: {e.returncode}")
        return False
    except Exception as e:
        _log("ERROR", f"  处理时发生意外错误: {e}")
        return False


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
            'ffmpeg', '-i', video_path,
            '-map', '0:a',              # 明确选择第一个音频流
            '-acodec', 'pcm_s16le',
            '-ar', '44100', '-ac', '2',
            '-af', 'aresample=async=1',  # 修复音频时间戳不同步问题
            audio_output_path, '-y'
        ]

        result = subprocess.run(
            ffmpeg_cmd, capture_output=True, encoding='utf-8', errors='ignore',
            timeout=600
        )

        if result.returncode == 0:
            if not USE_TQDM_PROGRESS_BAR:
                _log("INFO", f"[提取成功] 临时参考音频: {os.path.basename(audio_output_path)}")
            return True
        else:
            err_msg = result.stderr.strip() if result.stderr else "(无错误输出)"
            _log("ERROR", f"[提取失败] 无法从 '{os.path.basename(video_path)}' 提取音频 (返回码: {result.returncode})")
            _log("ERROR", f"  ffmpeg 错误: {err_msg[:500]}")
            return False
    except Exception as e:
        _log("ERROR", f"[提取错误] {e}")
        return False


def crop_audio(start_time_str: str, end_time_str: str, input_file: str) -> Optional[AudioSegment]:
    try:
        start_ms = int(time_str_to_seconds(start_time_str) * 1000)
        end_ms = int(time_str_to_seconds(end_time_str) * 1000)
        audio = AudioSegment.from_file(input_file)
        return audio[max(0, start_ms):end_ms]
    except Exception as e:
        _log("ERROR", f"裁剪或读取音频时发生错误: {e}")
        return None


def adjust_duration_with_rubberband(input_path: str, output_path: str, target_duration_s: float):
    try:
        y, sr = sf.read(input_path)
        current_duration_s = len(y) / sr
        if current_duration_s < 0.01:
            shape = (int(target_duration_s * sr), y.shape[1]) if y.ndim > 1 else (int(target_duration_s * sr),)
            sf.write(output_path, np.zeros(shape, dtype=y.dtype), sr)
            return
        rate = current_duration_s / target_duration_s
        if abs(rate - 1.0) < 0.001: shutil.copy(input_path, output_path); return
        if not USE_TQDM_PROGRESS_BAR:
            _log("INFO", f"需调节时长：从 {current_duration_s:.2f}s 到 {target_duration_s:.2f}s (原始速率: {rate:.2f}x)")
        if rate < MIN_RUBBERBAND_RATE:
            if not USE_TQDM_PROGRESS_BAR:
                _log("WARN",
                    f"速率 {rate:.2f}x 低于下限 {MIN_RUBBERBAND_RATE}x。将以 {MIN_RUBBERBAND_RATE}x 速度拉伸并补充静音。")

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
            sf.write(output_path, final_y, sr)
        else:
            stretched_y = rb.time_stretch(y, sr, rate)
            sf.write(output_path, stretched_y, sr)
    except Exception as e:
        _log("ERROR", f"时长调节时发生严重错误: {e}")
        shutil.copy(input_path, output_path)


def merge_audio(parsed_subtitles: List[List], current_tmp_dir: str) -> AudioSegment:
    merged_audio = AudioSegment.empty()
    if not parsed_subtitles: return merged_audio
    _log("INFO", "开始合并音频片段")
    first_start_sec = time_str_to_seconds(parsed_subtitles[0][0])
    if first_start_sec > 0:
        merged_audio += AudioSegment.silent(duration=first_start_sec * 1000)
    for i, subtitle in enumerate(parsed_subtitles):
        start_time_str, end_time_str, _, _ = subtitle
        target_duration_ms = (time_str_to_seconds(end_time_str) - time_str_to_seconds(start_time_str)) * 1000
        segment_file = os.path.join(current_tmp_dir, f"output_{i}.wav")
        try:
            audio_segment = AudioSegment.from_file(segment_file)
            merged_audio += audio_segment
        except Exception as e:
            _log("WARN", f"无法读取文件 {segment_file} ({e})。将使用 {target_duration_ms / 1000:.2f}s 的静音代替。")
            merged_audio += AudioSegment.silent(duration=target_duration_ms)
        if i < len(parsed_subtitles) - 1:
            current_end_sec = time_str_to_seconds(end_time_str)
            next_start_sec = time_str_to_seconds(parsed_subtitles[i + 1][0])
            gap_sec = next_start_sec - current_end_sec
            if gap_sec > 0.001:
                merged_audio += AudioSegment.silent(duration=gap_sec * 1000)
    return merged_audio