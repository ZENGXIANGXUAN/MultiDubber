import os
import threading
from typing import Optional
from gradio_client import Client

# ✨ 确保单机模式与集群模式共享相同的 Gradio 5.x/4.x 文件包装适配器
try:
    from gradio_client import handle_file as file
except ImportError:
    from gradio_client import file
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
        # 探测 API 版本：检查 v2.0 专属特征端点
        try:
            api_info = cli.view_api(return_format="dict", print_info=False)
            named_endpoints = api_info.get("named_endpoints", {})
            if "/on_method_change" in named_endpoints or "/on_add_glossary_term" in named_endpoints:
                version = "2.0"
            else:
                version = "1.5"
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
                # 探测 API 版本：检查 v2.0 专属特征端点
                try:
                    api_info = _thread_local.client.view_api(return_format="dict", print_info=False)
                    named_endpoints = api_info.get("named_endpoints", {})
                    if "/on_method_change" in named_endpoints or "/on_add_glossary_term" in named_endpoints:
                        _thread_local.api_version = "2.0"
                    else:
                        _thread_local.api_version = "1.5"
                except Exception:
                    _thread_local.api_version = "1.5"
                _log("CONNECT", f"[{thread_name}] 连接成功！接口协议: v{_thread_local.api_version}")
            except Exception as e:
                _log("CONNECT", f"[{thread_name}] 无法连接到 Gradio 服务。错误: {e}")
                raise e
        return _thread_local.client

    @classmethod
    def get_api_version(cls) -> str:
        """返回当前线程缓存的 API 版本，未连接时默认 '1.5'"""
        return getattr(_thread_local, "api_version", "1.5")


def generate_audio_api(ref_audio_path: str, gen_text: str, speed: float) -> Optional[str]:
    if not os.path.exists(ref_audio_path):
        _log("API_ERR", f"参考音频文件不存在: {ref_audio_path}")
        return None

    try:
        client = TTSClient.get_client()
        version = TTSClient.get_api_version()

        if version == "1.5":
            result = client.predict(
                prompt=file(ref_audio_path), text=gen_text, infer_mode='普通推理',
                max_text_tokens_per_sentence=120, sentences_bucket_max_size=4, param_5=True,
                param_6=0.8, param_7=30, param_8=speed, param_9=0.0, param_10=3,
                param_11=10.0, param_12=600, api_name="/gen_single"
            )
            if isinstance(result, dict):
                return result.get("value")
            return None
        else:
            # v2.0: speed 由下游 rubberband 后处理对齐，此处不传入
            # ✨ 核心修复：改用关键字参数传参，杜绝位置错位导致的组件类型异常
            result = client.predict(
                emo_control_method="Same as the voice reference",
                prompt=file(ref_audio_path),
                text=gen_text,
                emo_ref_path=file(ref_audio_path),
                emo_weight=0.65,
                vec1=0.0, vec2=0.0, vec3=0.0, vec4=0.0, vec5=0.0, vec6=0.0, vec7=0.0, vec8=0.0,
                emo_text="",
                emo_random=False,
                max_text_tokens_per_segment=120.0,
                param_16=True,
                param_17=0.8,
                param_18=30.0,
                param_19=0.8,
                param_20=0.0,
                param_21=3.0,
                param_22=10.0,
                param_23=1500.0,
                api_name="/gen_single"
            )
            if result is None:
                return None
            if isinstance(result, dict):
                return result.get("name")
            return result
    except Exception as e:
        _log("API_ERR", f"API 调用失败: {e}")
        return None
