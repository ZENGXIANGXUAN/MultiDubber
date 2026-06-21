import os
import threading
from typing import Optional
from gradio_client import Client, file
import warnings
import config
from logger import log as _log

# Suppress specific warnings
warnings.filterwarnings("ignore", category=UserWarning, module='gradio_client.utils')

# Thread-local storage for per-thread Client instances
_thread_local = threading.local()


def test_connection(url: str) -> bool:
    try:
        _log("CONNECT", f"正在测试连接: {url} ...")
        cli = Client(url)
        # 探测 API 版本：检查是否存在 1.5 专属端点 /gen_single
        try:
            api_info = cli.view_api(return_format="dict", print_info=False)
            if "/gen_single" in api_info.get("named_endpoints", {}):
                version = "1.5"
            else:
                version = "2.0"
        except Exception:
            version = "未知"
        _log("CONNECT", f"连接成功！接口协议: v{version}")
        return True
    except Exception as e:
        _log("CONNECT", f"连接失败: {e}")
        return False


class TTSClient:
    @classmethod
    def get_client(cls):
        # Each thread gets its own Client instance
        if not hasattr(_thread_local, "client") or getattr(_thread_local, "connected_url", None) != config.GRADIO_URL:
            try:
                thread_name = threading.current_thread().name
                _log("CONNECT", f"[{thread_name}] 正在连接到 Gradio 服务 ({config.GRADIO_URL})...")
                _thread_local.client = Client(config.GRADIO_URL)
                _thread_local.connected_url = config.GRADIO_URL
                _log("CONNECT", f"[{thread_name}] 连接成功！")
            except Exception as e:
                _log("CONNECT", f"[{thread_name}] 无法连接到 Gradio 服务。错误: {e}")
                raise e
        return _thread_local.client


def generate_audio_api(ref_audio_path: str, gen_text: str, speed: float) -> Optional[str]:
    if not os.path.exists(ref_audio_path):
        _log("API_ERR", f"参考音频文件不存在: {ref_audio_path}")
        return None

    try:
        client = TTSClient.get_client()
        result = client.predict(
            prompt=file(ref_audio_path), text=gen_text, infer_mode='普通推理',
            max_text_tokens_per_sentence=120, sentences_bucket_max_size=4, param_5=True,
            param_6=0.8, param_7=30, param_8=speed, param_9=0.0, param_10=3,
            param_11=10.0, param_12=600, api_name="/gen_single"
        )
        return result["value"] if result else None
    except Exception as e:
        _log("API_ERR", f"API 调用失败: {e}")
        return None
