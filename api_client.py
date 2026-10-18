import os
import threading
from typing import Optional, Tuple

from gradio_client import Client
import httpx

try:
    from gradio_client import handle_file as file
except ImportError:
    from gradio_client import file

import warnings

import config
import indextts_15
import voxcpm2
from logger import log as _log


warnings.filterwarnings("ignore", category=UserWarning, module="gradio_client.utils")

# Gradio Client 不是线程安全的，每个工作线程复用自己的连接。
_thread_local = threading.local()

PROTOCOL_LABELS = {
    config.PROTOCOL_INDEXTTS15: "IndexTTS 1.5",
    config.PROTOCOL_VOXCPM2: "VoxCPM 2.0",
}


def detect_protocol_from_client(client: Client) -> str:
    """从已连接的 Gradio Client 实例中识别服务协议。"""
    # 1. 优先检查 client.endpoints 实例列表
    try:
        endpoints = getattr(client, "endpoints", [])
        api_names = [getattr(ep, "api_name", None) for ep in endpoints]
        if "/gen_single" in api_names:
            return config.PROTOCOL_INDEXTTS15
        if "/generate" in api_names:
            return config.PROTOCOL_VOXCPM2
    except Exception:
        pass

    # 2. 检查 view_api 字典
    try:
        api_info = client.view_api(print_info=False, return_format="dict")
        named = api_info.get("named_endpoints", {}) if isinstance(api_info, dict) else {}
        if "/gen_single" in named:
            return config.PROTOCOL_INDEXTTS15
        if "/generate" in named:
            return config.PROTOCOL_VOXCPM2
    except Exception:
        pass

    # 3. 检查 config 字典
    try:
        deps = client.config.get("dependencies", [])
        api_names = [d.get("api_name") for d in deps if isinstance(d, dict)]
        if "/gen_single" in api_names or "gen_single" in api_names:
            return config.PROTOCOL_INDEXTTS15
        if "/generate" in api_names or "generate" in api_names:
            return config.PROTOCOL_VOXCPM2
    except Exception:
        pass

    return "unknown"


def test_connection(url: str) -> Tuple[bool, str, str]:
    """验证服务是否可连接并自动探测协议类型。

    在添加服务器时一次性确定协议，返回 (success, protocol, message)。
    测试 Client 用完立即关闭，不参与正式推理。
    """
    client = None
    try:
        _log("CONNECT", f"正在测试连接: {url} ...")
        client = Client(
            url,
            verbose=False,
            httpx_kwargs={"timeout": httpx.Timeout(12.0, connect=8.0)},
        )
        proto = detect_protocol_from_client(client)
        proto_name = PROTOCOL_LABELS.get(proto, proto)
        _log("CONNECT", f"连接成功！接口协议: {proto_name}")
        return True, proto, f"连接成功 ({proto_name})"
    except Exception as exc:
        _log("CONNECT", f"连接失败: {exc}")
        return False, "unknown", str(exc)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


class TTSClient:
    @classmethod
    def get_client(cls) -> Client:
        connected_url = getattr(_thread_local, "connected_url", None)
        if not hasattr(_thread_local, "client") or connected_url != config.GRADIO_URL:
            thread_name = threading.current_thread().name
            try:
                _log("CONNECT", f"[{thread_name}] 正在连接到 Gradio 服务 ({config.GRADIO_URL})...")
                # 初始化阶段限制握手/元数据读取时间，防止永久卡住；
                # 初始化完成后改为“仅限制 connect”，避免长时间 TTS 推理被 read timeout 误杀。
                _thread_local.client = Client(
                    config.GRADIO_URL,
                    verbose=False,
                    httpx_kwargs={"timeout": httpx.Timeout(15.0, connect=8.0)},
                )
                _thread_local.client.httpx_kwargs["timeout"] = httpx.Timeout(None, connect=10.0)
                _thread_local.connected_url = config.GRADIO_URL
                proto = detect_protocol_from_client(_thread_local.client)
                proto_name = PROTOCOL_LABELS.get(proto, proto)
                _log("CONNECT", f"[{thread_name}] 连接成功！接口协议: {proto_name}")
            except Exception as exc:
                _log("CONNECT", f"[{thread_name}] 无法连接到 Gradio 服务。错误: {exc}")
                raise
        return _thread_local.client


def _extract_audio_path(result) -> Optional[str]:
    """提取音频 filepath（兼容 IndexTTS 与 VoxCPM 返回格式）。"""
    if result is None:
        return None
    if isinstance(result, (tuple, list)):
        return _extract_audio_path(result[0]) if result else None
    if isinstance(result, dict):
        return result.get("value") or result.get("path") or result.get("url") or result.get("name")
    if isinstance(result, str):
        return result
    return None


def generate_audio_api(ref_audio_path: str, gen_text: str, speed: float,
                       protocol: str = None) -> Optional[str]:
    """单机模式调用 TTS 接口。"""
    if not os.path.exists(ref_audio_path):
        _log("API_ERR", f"参考音频文件不存在: {ref_audio_path}")
        return None

    if protocol is None:
        protocol = getattr(config, "TTS_PROTOCOL", config.PROTOCOL_VOXCPM2)

    try:
        client = TTSClient.get_client()
        if protocol == config.PROTOCOL_INDEXTTS15:
            result = indextts_15.predict_audio(
                client,
                ref_audio_path,
                gen_text,
                speed,
                file,
            )
        else:
            result = voxcpm2.predict_audio(
                client,
                ref_audio_path,
                gen_text,
                speed,
                file,
            )
        return _extract_audio_path(result)
    except Exception as exc:
        _log("API_ERR", f"API 调用失败: {exc}")
        return None
