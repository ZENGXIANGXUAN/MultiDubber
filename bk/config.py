import os
import threading

# === Progress Bar Toggle ===
# Set to True to use tqdm progress bar (CLI mode), False for verbose line printing
USE_TQDM_PROGRESS_BAR = True

# === Subtitle Parsing Configuration ===
TRANSFORMERS_LINE = 2
TIME_LINE = 1
ENGLISH_LINE = -1
MAX_SUBTITLE_LENGTH = 35

# === API Configuration ===
GRADIO_URL = "http://127.0.0.1:7860/"

# === Thread Configuration (NEW) ===
MAX_WORKERS = 5  # 默认并发线程数

# === Retry Configuration ===
MAX_RETRIES = 3  # 单个任务最大重试次数

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