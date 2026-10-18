import re
from typing import Optional


_SLASH_SEPARATOR_RE = re.compile(r"\s*[/／]+\s*")


def preprocess_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("**", "")
    # 部分 TTS 模型可能把斜杠读成异常字符；作为词语分隔符时改为自然停顿。
    text = _SLASH_SEPARATOR_RE.sub("、", text)
    text = text.strip()
    return text


def preprocess_srt_content(content: str, text_line: int) -> str:
    """仅清洗每个 SRT 字幕块中送入 TTS 的文本行，保留时间轴和其他语言行。"""
    processed_lines = []
    block_line = 0
    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        line_ending = line[len(body):]
        if not body.strip():
            block_line = 0
        else:
            if block_line == text_line:
                body = preprocess_text(body)
            block_line += 1
        processed_lines.append(body + line_ending)
    return "".join(processed_lines)


# 最少文本行数：index + timestamp + 中文 + 英文 = 4 行
# 只有 3 行说明缺少一种语言，跳过不配音
MIN_LINES_WITH_TEXT = 4


def should_skip_short_segment(lines: list[str]) -> bool:
    """跳过行数不足的字幕段（缺少中文或英文对照）。"""
    return len(lines) < MIN_LINES_WITH_TEXT
