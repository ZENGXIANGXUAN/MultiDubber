import os
import json
import hashlib
import subprocess
from config import USE_TQDM_PROGRESS_BAR
from logger import log as _log


def compute_layout_signature(subtitles) -> str:
    """
    计算字幕布局指纹：每条字幕的 (起始时间, 结束时间, 中文文本) 串起来做 sha1。

    断点续传按 index 复用 output_{i}.wav，一旦字幕布局变了（合并规则调整、
    SRT 被重新断句、MERGE_MAX_CHARS 改动），同一个 index 对应的文本就变了，
    旧的 output_{i}.wav 会被当成“已完成”直接拼进成品，导致整句错位且无任何提示。
    用指纹把旧进度作废，是这里唯一的防线。
    """
    digest = hashlib.sha1()
    for subtitle in subtitles:
        start = str(subtitle[0]).strip()
        end = str(subtitle[1]).strip()
        text = str(subtitle[2]).strip()
        digest.update(f"{start}\t{end}\t{text}\n".encode("utf-8"))
    return digest.hexdigest()[:16]


def status_is_layout_compatible(status_file: str, current_srt_file: str, layout_sig: str) -> bool:
    """
    判断磁盘上的进度文件是否与当前字幕布局兼容。

    返回 False 时调用方必须**物理清空** tmp 下的 output_*.wav，否则后续
    “扫描已存在片段并恢复”的逻辑会把旧布局的片段重新捡回来，作废状态等于白做。

    没有 layout_sig 字段的旧状态一律视为不兼容：无法证明 index 语义一致，
    宁可重跑也不能让错位片段进成品。
    """
    if not os.path.exists(status_file):
        return True
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return True
    if data.get("current_file") != current_srt_file:
        return True
    return data.get("layout_sig") == layout_sig


def load_status(status_file: str, current_srt_file: str, layout_sig: str = None) -> set[int]:
    if not os.path.exists(status_file):
        return set()
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("current_file") != current_srt_file:
                return set()
            if layout_sig is not None:
                saved_sig = data.get("layout_sig")
                if saved_sig != layout_sig:
                    _log(
                        "WARN",
                        f"[恢复系统] 状态文件与当前字幕布局不一致 "
                        f"(saved={saved_sig or '无'} / now={layout_sig})，"
                        f"丢弃旧片段进度，全部重新生成",
                    )
                    return set()
            return set(data.get("completed_indices", []))
    except (json.JSONDecodeError, IOError):
        return set()


def save_status(status_file: str, file_name: str, completed_indices: set[int],
                layout_sig: str = None) -> None:
    """原子写入状态文件：先写 .part 再替换，避免进程被杀时留下截断 JSON。"""
    temp_file = status_file + ".part"
    payload = {
        "current_file": file_name,
        "completed_indices": sorted(list(completed_indices)),
    }
    if layout_sig is not None:
        payload["layout_sig"] = layout_sig
    try:
        parent = os.path.dirname(status_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(temp_file, status_file)
    except (IOError, OSError) as e:
        _log("ERROR", f"严重错误：无法保存状态到 '{status_file}'。错误: {e}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass


def clear_status(status_file: str) -> None:
    if os.path.exists(status_file):
        try:
            os.remove(status_file)
        except OSError as e:
            _log("WARN", f"无法删除状态文件 '{status_file}'。错误: {e}")


def is_valid_audio_segment(file_path: str) -> bool:
    """
    检查音频片段是否有效（大小 > 1000 字节，且非全静音文件）。
    用于断点恢复，防止异常中断或熔断产生的静音占位符被误认为已完成。
    """
    try:
        if not os.path.isfile(file_path) or os.path.getsize(file_path) <= 1000:
            return False
        with open(file_path, "rb") as f:
            f.seek(44)  # 跳过标准 WAV 头部
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                if any(chunk):
                    return True
        return False
    except Exception:
        return False

# === 核心修复：定义防弹窗标志 ===
def get_subprocess_flags() -> int:
    """获取用于隐藏控制台窗口的标志 (仅限 Windows)"""
    if os.name == 'nt':
        return subprocess.CREATE_NO_WINDOW
    return 0

_ffmpeg_available = None


def check_ffmpeg() -> bool:
    """检查 ffmpeg 是否安装并可用（结果缓存，避免每次合并都起子进程）。"""
    global _ffmpeg_available
    if _ffmpeg_available is not None:
        return _ffmpeg_available

    try:
        # === 修复：添加 creationflags ===
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=True,
            text=True,
            encoding='utf-8',
            creationflags=get_subprocess_flags()
        )
        if not USE_TQDM_PROGRESS_BAR: _log("INFO", "ffmpeg 已找到。")
        _ffmpeg_available = True
        return True
    except FileNotFoundError:
        _log("ERROR", "ffmpeg 未找到。请确保它已安装并添加到系统 PATH 中。")
        _ffmpeg_available = False
        return False
    except subprocess.CalledProcessError as e:
        if "ffmpeg version" in e.stderr.lower() or "ffmpeg version" in e.stdout.lower():
            if not USE_TQDM_PROGRESS_BAR: _log("INFO", "ffmpeg 已找到 (执行 version 命令时可能输出了警告/错误，但可执行)。")
            _ffmpeg_available = True
            return True
        _log("ERROR", f"ffmpeg 执行时出错 (但可能已安装)。尝试检查 ffmpeg -version 手动。\n{e.stderr}")
        _ffmpeg_available = False
        return False
    except Exception as e_gen:
        _log("ERROR", f"检查 ffmpeg 时发生未知错误: {e_gen}")
        _ffmpeg_available = False
        return False


def time_str_to_seconds(time_str: str) -> float:
    try:
        h, m, s = time_str.split(':')
        s, ms = s.split(',')
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    except ValueError as e:
        _log("ERROR", f"解析时间错误: {time_str}, 错误: {e}")
        return 0.0