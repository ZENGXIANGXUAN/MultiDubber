from config import TIME_LINE, TRANSFORMERS_LINE, ENGLISH_LINE
from logger import log as _log


def parse_subtitles(file_content: str, transformers_line: int = TRANSFORMERS_LINE) -> list[list[str]]:
    subtitles = file_content.strip().split('\n\n')
    result = []
    for idx, segment in enumerate(subtitles):
        lines = segment.strip().splitlines()
        if len(lines) < TIME_LINE + 1: continue
        time_line = lines[TIME_LINE].strip()
        if " --> " not in time_line: continue
        try:
            start_time, end_time = [t.strip() for t in time_line.split('-->')]
        except Exception as e:
            _log("WARN", f"段落 {idx} 解析时间错误: {e}")
            continue
        if len(lines) <= transformers_line: continue
        text = lines[transformers_line].strip()
        
        # Handle cases where ENGLISH_LINE might be out of bounds if strictly -1 and not enough lines
        # But assuming the original logic works:
        try:
            english_text = lines[ENGLISH_LINE].strip()
        except IndexError:
            english_text = ""
            
        result.append([start_time, end_time, text, english_text])
    return result


def merge_contiguous_subtitles(subtitles: list[list[str]], max_chars: int = 30) -> list[list[str]]:
    """
    合并时间戳连续的相邻字幕。

    Args:
        subtitles: 字幕列表，每条格式 [start_time, end_time, text, english_text]
        max_chars: 合并后文本最大字符数，0 表示禁用合并

    Returns:
        合并后的字幕列表
    """
    if max_chars <= 0 or len(subtitles) <= 1:
        return subtitles

    merged = [subtitles[0][:]]  # 深拷贝第一条

    for i in range(1, len(subtitles)):
        prev = merged[-1]
        curr = subtitles[i]

        # 条件1：时间戳严格连续（字符串比较）
        is_contiguous = prev[1].strip() == curr[0].strip()

        # 条件2：合并后字数不超限
        combined_text = prev[2] + curr[2]
        is_within_limit = len(combined_text) <= max_chars

        if is_contiguous and is_within_limit:
            prev[1] = curr[1]           # 更新结束时间
            prev[2] = combined_text     # 合并文本
            prev[3] = prev[3] + curr[3] # 合并英文
        else:
            merged.append(curr[:])      # 深拷贝添加

    return merged

