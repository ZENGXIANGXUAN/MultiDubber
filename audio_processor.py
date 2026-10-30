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

        # 参考音频只用于裁剪片段，最终会被统一转成 24kHz 单声道；
        # 直接按目标格式抽取，可把长视频的常驻内存/磁盘降到 1/3.7。
        ffmpeg_cmd = [
            'ffmpeg', '-i', video_path, '-vn', '-acodec', 'pcm_s16le',
            '-ar', '24000', '-ac', '1', audio_output_path, '-y'
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


def adjust_duration_with_rubberband(input_path: str, output_path: str, target_duration_s: float) -> bool:
    """
    把 input_path 的时长精确对齐到 target_duration_s，写入 output_path。

    返回 True 表示按预期完成（含无需调整的直通）；False 表示变速失败、走了
    兜底路径——此时仍会保证输出时长等于目标时长，但音质不保证。

    关键约束：无论成功失败，输出文件的时长都必须等于目标时长。
    历史实现在异常分支里直接 shutil.copy(input_path, output_path)，把未调节
    时长的原始音频原样落盘，再由 merge_audio 静默补零，最终表现为“这段没人
    说话”；同时失败只走 _safe_print（stdout），GUI 模式下日志里看不到任何痕迹。
    """
    target_ms = int(round(max(0.0, target_duration_s) * 1000))
    try:
        y, sr = sf.read(input_path)
        target_samples = _target_samples(target_duration_s, sr)
        target_duration_s = target_samples / sr
        current_duration_s = len(y) / sr
        if current_duration_s < 0.01:
            shape = (target_samples, y.shape[1]) if y.ndim > 1 else (target_samples,)
            sf.write(output_path, np.zeros(shape, dtype=y.dtype), sr)
            return True
        rate = current_duration_s / target_duration_s
        if abs(rate - 1.0) < 0.001:
            sf.write(output_path, _fit_array_to_samples(y, target_samples), sr)
            return True
        if not USE_TQDM_PROGRESS_BAR:
            _safe_print(f"    -> 需调节时长：从 {current_duration_s:.2f}s 到 {target_duration_s:.2f}s (原始速率: {rate:.2f}x)")
        if rate < MIN_RUBBERBAND_RATE:
            logger_log(
                "AUDIO",
                f"    -> 速率 {rate:.2f}x 低于下限 {MIN_RUBBERBAND_RATE}x，"
                f"以 {MIN_RUBBERBAND_RATE}x 拉伸后补静音"
                f"（{current_duration_s:.2f}s / 目标 {target_duration_s:.2f}s）",
            )

            # rb.time_stretch 在后台调用 rubberband.exe
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
        return True
    except Exception as e:
        logger_log(
            "AUDIO",
            f"!! 时长调节失败，已改用 pydub 强制对齐到 {target_ms}ms（音质不保证）: "
            f"{os.path.basename(input_path)} | {e}",
        )
        try:
            segment = AudioSegment.from_file(input_path)
            _fit_audio_segment(segment, target_ms).export(output_path, format="wav")
        except Exception as e2:
            logger_log("AUDIO", f"!! 兜底对齐同样失败，写出静音占位: {os.path.basename(input_path)} | {e2}")
            try:
                AudioSegment.silent(duration=target_ms).export(output_path, format="wav")
            except Exception as e3:
                logger_log("AUDIO", f"!! 静音占位写入失败: {output_path} | {e3}")
                return False
        return False


_SF_SAMPLE_WIDTH = {
    "PCM_S8": 1, "PCM_U8": 1, "PCM_16": 2, "PCM_24": 3, "PCM_32": 4,
    "FLOAT": 4, "DOUBLE": 8, "ALAW": 1, "ULAW": 1,
}


def _probe_audio_format(path: str):
    """只读文件头，返回 (channels, frame_rate, sample_width)；失败返回 None。"""
    try:
        info = sf.info(path)
    except Exception:
        return None
    width = _SF_SAMPLE_WIDTH.get(str(info.subtype).upper())
    if not width or info.samplerate <= 0 or info.channels <= 0:
        return None
    return int(info.channels), int(info.samplerate), int(width)


def merge_audio(parsed_subtitles: List[List], current_tmp_dir: str) -> AudioSegment:
    if not parsed_subtitles:
        return AudioSegment.empty()

    total = len(parsed_subtitles)
    logger_log("MERGE", f"开始合并音频片段 ({total} 段)")

    # 1) 扫 header 求目标格式，保持原“取最大 channels/frame_rate/sample_width”语义。
    #    只读文件头，不整段解码。
    target_format = None
    for i in range(total):
        fmt = _probe_audio_format(os.path.join(current_tmp_dir, f"output_{i}.wav"))
        if fmt is None:
            continue
        if target_format is None:
            target_format = fmt
        else:
            target_format = (
                max(target_format[0], fmt[0]),
                max(target_format[1], fmt[1]),
                max(target_format[2], fmt[2]),
            )

    channels, frame_rate, sample_width = target_format or (1, 1, 1)
    frame_bytes = max(1, channels * sample_width)

    # 2) 逐段收集 raw bytes，最后一次性构造。
    #    原实现 `merged_audio += segment` 每次重拷已累积音频，复杂度 O(n²)。
    chunks: List[bytes] = []
    cursor_frames = 0
    short_segments = []   # (index, 目标ms, 实际ms)
    missing_segments = []  # index

    for i, subtitle in enumerate(parsed_subtitles):
        start_ms, end_ms = _subtitle_ms_bounds(subtitle)
        target_duration_ms = max(0, end_ms - start_ms)

        # 用“实际已写入的帧数”对齐到目标起点，避免长时间累积漂移。
        start_frame = int(round(start_ms * frame_rate / 1000.0))
        if start_frame > cursor_frames:
            chunks.append(b"\x00" * ((start_frame - cursor_frames) * frame_bytes))

        segment_file = os.path.join(current_tmp_dir, f"output_{i}.wav")
        data = b""
        if os.path.exists(segment_file):
            try:
                audio_segment = AudioSegment.from_file(segment_file)
                if audio_segment.channels != channels:
                    audio_segment = audio_segment.set_channels(channels)
                if audio_segment.frame_rate != frame_rate:
                    audio_segment = audio_segment.set_frame_rate(frame_rate)
                if audio_segment.sample_width != sample_width:
                    audio_segment = audio_segment.set_sample_width(sample_width)
                data = audio_segment.raw_data
            except Exception as e:
                logger_log("MERGE", f"!! 无法读取 {segment_file} ({e})，用静音代替。")
        else:
            missing_segments.append(i)

        want_frames = int(round(target_duration_ms * frame_rate / 1000.0))
        have_frames = len(data) // frame_bytes
        if have_frames > want_frames:
            data = data[: want_frames * frame_bytes]
        elif have_frames < want_frames:
            pad_ms = int(round((want_frames - have_frames) * 1000.0 / frame_rate))
            if pad_ms >= 300:
                short_segments.append((i, target_duration_ms, int(round(have_frames * 1000.0 / frame_rate))))
            data += b"\x00" * ((want_frames - have_frames) * frame_bytes)

        chunks.append(data)
        cursor_frames = max(cursor_frames, start_frame + want_frames)

        # 每 5 段或最后一段汇报进度
        if (i + 1) % 5 == 0 or i + 1 == total:
            logger_log("MERGE", f"合并进度: {i + 1}/{total} ({(i + 1) * 100 // total}%)")

    # 合并阶段的静音补齐必须可见：历史上这里静默补零，把“片段过短”伪装成
    # “这段本来没人说话”，成品看起来完全正常。
    if missing_segments:
        logger_log(
            "MERGE",
            f"!! 有 {len(missing_segments)} 个片段文件缺失，已按目标时长填静音："
            f"{missing_segments[:20]}{' ...' if len(missing_segments) > 20 else ''}",
        )
    if short_segments:
        preview = ", ".join(
            f"#{idx} {have/1000:.2f}s/{target/1000:.2f}s" for idx, target, have in short_segments[:10]
        )
        logger_log(
            "MERGE",
            f"!! 有 {len(short_segments)} 个片段短于字幕时间槽，已补静音（示例：{preview}"
            f"{' ...' if len(short_segments) > 10 else ''}）",
        )

    merged_audio = AudioSegment(
        data=b"".join(chunks),
        sample_width=sample_width,
        frame_rate=frame_rate,
        channels=channels,
    )
    logger_log("MERGE", f"合并完成，总时长 {len(merged_audio) / 1000:.1f}s")
    return merged_audio
