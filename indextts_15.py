from typing import Any, Callable


INDEXTTS_API_NAME = "/gen_single"


def predict_audio(
    client: Any,
    ref_audio_path: str,
    gen_text: str,
    speed: float,
    file_wrapper: Callable[[str], Any],
):
    """调用 IndexTTS 1.5 ``/gen_single`` 接口。

    注意：IndexTTS 1.5 的 ``/gen_single`` **没有语速参数**。它的签名是
    ``gen_single(prompt, text, infer_mode, max_text_tokens_per_sentence,
    sentences_bucket_max_size, *args)``，其中 ``*args`` 依次是：

        do_sample, top_p, top_k, temperature, length_penalty,
        num_beams, repetition_penalty, max_mel_tokens

    也就是 param_5..param_12 的映射。此前把 ``speed`` 传给 param_8，
    实际落到了 temperature 上——采样温度随“语速”在 0.7~1.5 之间乱跳，
    语速本身却从未生效。这里把 temperature 固定回 1.0，语速统一交给
    客户端的 rubberband 后处理。

    ``speed`` 参数仅为兼容调度器签名保留，不参与请求。
    """
    _ = speed
    return client.predict(
        prompt=file_wrapper(ref_audio_path),
        text=gen_text,
        infer_mode="普通推理",
        max_text_tokens_per_sentence=120,
        sentences_bucket_max_size=4,
        param_5=True,    # do_sample
        param_6=0.8,     # top_p
        param_7=30,      # top_k
        param_8=1.0,     # temperature（不是 speed）
        param_9=0.0,     # length_penalty
        param_10=3,      # num_beams
        param_11=10.0,   # repetition_penalty
        param_12=600,    # max_mel_tokens
        api_name=INDEXTTS_API_NAME,
    )
