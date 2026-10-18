import os
import threading

# === Progress Bar Toggle ===
# Set to True to use tqdm progress bar (CLI mode), False for verbose line printing
USE_TQDM_PROGRESS_BAR = True

# === Subtitle Parsing Configuration ===
TRANSFORMERS_LINE = 2
TIME_LINE = 1
ENGLISH_LINE = -1
# === API Configuration ===
# 支持的 TTS 协议
PROTOCOL_VOXCPM2 = "voxcpm2"
PROTOCOL_INDEXTTS15 = "indextts_15"

# GUI 不读取默认服务器地址；服务地址通过 “Add & Connect” 动态添加并自动探测协议保存。
# 此空值仅保留给旧的单服务器 CLI 调用链兼容，不会由 GUI 自动添加。
GRADIO_URL = ""
TTS_PROTOCOL = PROTOCOL_VOXCPM2

# === VoxCPM 2.0 /generate Parameters ===
VOXCPM_CONTROL_INSTRUCTION = ""
VOXCPM_USE_PROMPT_TEXT = False
VOXCPM_PROMPT_TEXT_VALUE = ""
VOXCPM_CFG_VALUE = 2.0
VOXCPM_DO_NORMALIZE = False
VOXCPM_DENOISE = False
VOXCPM_DIT_STEPS = 10
VOXCPM_SEED_VALUE = 1544503467

# === Thread Configuration ===
MAX_WORKERS = 1  # 单服务器 CLI 默认线程数

# 多服务器模式：每台服务器的 GPU worker 数。
# 默认 1 线程，支持 1 或 2 线程（适合并发能力强的节点）。
DEFAULT_SERVER_THREADS_PER_SERVER = 1
ALLOWED_SERVER_THREADS_PER_SERVER = (1, 2)

# 兼容 GUI 直接读写 config.SERVER_THREADS_PER_SERVER 的写法。
SERVER_THREADS_PER_SERVER = DEFAULT_SERVER_THREADS_PER_SERVER

# === Retry Configuration ===
MAX_RETRIES = 5  # 单个任务最大重试次数
CIRCUIT_BREAKER_THRESHOLD = 6  # 连续重连失败 N 次后触发服务器熔断；-1 表示禁用熔断（Cloudflare 穿透环境下需适当放大）

# === Paths (Defaults) ===
# Note: These are default values. The GUI will override SRT_PATH,
# and main.py will dynamically calculate TMP_DIR based on the selected folder.
SRT_PATH = r""
REF_AUDIO_PATH = os.path.join(SRT_PATH, "REF_AUDIO_PATH")
OUTPUT_PATH = SRT_PATH
TMP_DIR = os.path.join(SRT_PATH, "tmp") # Only used as fallback default
MODEL_PATH = "duration_predictor.joblib"
STATUS_FILE = os.path.join(TMP_DIR, "dubbing_status.json") # Only used as fallback default

# === Processing Configuration ===
TRAINING_THRESHOLD = 5
MIN_SPEED, MAX_SPEED = 0.7, 1.5
MIN_RUBBERBAND_RATE = 0.8

# === Global Locks ===
model_lock = threading.Lock()
status_lock = threading.Lock()

# === Global Control Flags ===
ABORT_ALL = False
STOP_REASON = None  # None | "USER_ABORT" | "ALL_SERVERS_DOWN" | "RESTART_REQUESTED"

# === 字幕合并配置 ===
MERGE_MAX_CHARS = 30  # 合并后文本最大字符数，0 表示禁用合并

# === 本地缓存配置 ===
# 启用本地缓存以优化云存储（如 Google Drive）的处理性能
# 当处理云存储文件夹时，会先复制到本地临时目录，处理完成后再同步回去
USE_LOCAL_CACHE = True  # 设为 True 启用本地缓存
LOCAL_CACHE_DIR = None  # 本地缓存目录，None 表示使用系统临时目录