from typing import Any, Callable

import config


VOXCPM_API_NAME = "/generate"


def predict_audio(
    client: Any,
    ref_audio_path: str,
    gen_text: str,
    speed: float,
    file_wrapper: Callable[[str], Any],
):
    """调用 VoxCPM 2.0 ``/generate`` 接口。

    ``speed`` 参数仅为兼容项目现有调度器签名而保留。VoxCPM 2.0 的
    Gradio ``/generate`` 接口没有原生语速参数，最终字幕时长由项目现有的
    Rubber Band 后处理统一对齐。
    """
    _ = speed
    return client.predict(
        text=gen_text,
        control_instruction=getattr(config, "VOXCPM_CONTROL_INSTRUCTION", ""),
        ref_wav=file_wrapper(ref_audio_path),
        use_prompt_text=getattr(config, "VOXCPM_USE_PROMPT_TEXT", False),
        prompt_text_value=getattr(config, "VOXCPM_PROMPT_TEXT_VALUE", ""),
        cfg_value=float(getattr(config, "VOXCPM_CFG_VALUE", 2.0)),
        do_normalize=bool(getattr(config, "VOXCPM_DO_NORMALIZE", False)),
        denoise=bool(getattr(config, "VOXCPM_DENOISE", False)),
        dit_steps=float(getattr(config, "VOXCPM_DIT_STEPS", 10)),
        seed_value=int(getattr(config, "VOXCPM_SEED_VALUE", 1544503467)),
        api_name=VOXCPM_API_NAME,
    )
