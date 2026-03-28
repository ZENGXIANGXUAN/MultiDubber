import os
import json
import subprocess
from typing import Set
from config import USE_TQDM_PROGRESS_BAR

def load_status(status_file: str, current_srt_file: str) -> Set[int]:
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


def save_status(status_file: str, file_name: str, completed_indices: Set[int]):
    try:
        os.makedirs(os.path.dirname(status_file), exist_ok=True)
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump({"current_file": file_name, "completed_indices": sorted(list(completed_indices))}, f, indent=4)
    except IOError as e:
        print(f"!! 严重错误：无法保存状态到 '{status_file}'。错误: {e}")


def clear_status(status_file: str):
    if os.path.exists(status_file):
        try:
            os.remove(status_file)
        except OSError as e:
            print(f"!! 警告：无法删除状态文件 '{status_file}'。错误: {e}")

# === 核心修复：定义防弹窗标志 ===
def get_subprocess_flags():
    """获取用于隐藏控制台窗口的标志 (仅限 Windows)"""
    if os.name == 'nt':
        return subprocess.CREATE_NO_WINDOW
    return 0

def check_ffmpeg():
    """检查 ffmpeg 是否安装并可用"""
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
        if not USE_TQDM_PROGRESS_BAR: print("ffmpeg 已找到。")
        return True
    except FileNotFoundError:
        print("错误：ffmpeg 未找到。请确保它已安装并添加到系统 PATH 中。")
        return False
    except subprocess.CalledProcessError as e:
        if "ffmpeg version" in e.stderr.lower() or "ffmpeg version" in e.stdout.lower():
            if not USE_TQDM_PROGRESS_BAR: print("ffmpeg 已找到 (执行 version 命令时可能输出了警告/错误，但可执行)。")
            return True
        print(f"错误：ffmpeg 执行时出错 (但可能已安装)。尝试检查 ffmpeg -version 手动。\n{e.stderr}")
        return False
    except Exception as e_gen:
        print(f"检查 ffmpeg 时发生未知错误: {e_gen}")
        return False


def time_str_to_seconds(time_str: str) -> float:
    try:
        h, m, s = time_str.split(':')
        s, ms = s.split(',')
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    except ValueError as e:
        print(f"解析时间错误: {time_str}, 错误: {e}")
        return 0.0