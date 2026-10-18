from typing import Any, Callable


INDEXTTS_API_NAME = "/gen_single"


def predict_audio(
    client: Any,
    ref_audio_path: str,
    gen_text: str,
    speed: float,
    file_wrapper: Callable[[str], Any],
):
    """调用 IndexTTS 1.5 ``/gen_single`` 接口。"""
    return client.predict(
        prompt=file_wrapper(ref_audio_path),
        text=gen_text,
        infer_mode="普通推理",
        max_text_tokens_per_sentence=120,
        sentences_bucket_max_size=4,
        param_5=True,
        param_6=0.8,
        param_7=30,
        param_8=speed,
        param_9=0.0,
        param_10=3,
        param_11=10.0,
        param_12=600,
        api_name=INDEXTTS_API_NAME,
    )
