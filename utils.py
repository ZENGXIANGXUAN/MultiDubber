import os
import json
import subprocess
from config import USE_TQDM_PROGRESS_BAR
from logger import log as _log


def load_status(status_file: str, current_srt_file: str) -> set[int]:
    if not os.path.exists(status_file):
        return set()
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("current_file") == current_srt_file:
                return set(data.get("completed_indices", []))
            else:
                return set()
    except (json.JSONDecodeError, IOError):
        return set()


def save_status(status_file: str, file_name: str, completed_indices: set[int]) -> None:
    """原子写入状态文件：先写 .part 再替换，避免进程被杀时留下截断 JSON。"""
    temp_file = status_file + ".part"
    try:
        parent = os.path.dirname(status_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(
                {"current_file": file_name, "completed_indices": sorted(list(completed_indices))},
                f,
                separators=(",", ":"),
            )
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