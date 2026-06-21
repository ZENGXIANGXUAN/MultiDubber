import os
from typing import Optional
from gradio_client import Client, file
import warnings
import config
from logger import log as _log

# Suppress specific warnings
warnings.filterwarnings("ignore", category=UserWarning, module='gradio_client.utils')


def test_connection(url: str) -> bool:
    try:
        _log("CONNECT", f"正在测试连接: {url} ...")
        Client(url)
        _log("CONNECT", "连接成功！")
        return True
    except Exception as e:
        _log("CONNECT", f"连接失败: {e}")
        return False


class TTSClient:
    _client = None
    _connected_url = None  # 记录当前连接的 URL

    @classmethod
    def get_client(cls):
        if cls._client is None or cls._connected_url != config.GRADIO_URL:
            try:
                _log("CONNECT", f"正在连接到 Gradio 服务 ({config.GRADIO_URL})...")
                cls._client = Client(config.GRADIO_URL)
                cls._connected_url = config.GRADIO_URL
                _log("CONNECT", "连接成功！")
            except Exception as e:
                _log("CONNECT", f"无法连接到 Gradio 服务。错误: {e}")
                raise e
        return cls._client


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