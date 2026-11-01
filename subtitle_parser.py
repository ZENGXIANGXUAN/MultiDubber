from config import TIME_LINE, TRANSFORMERS_LINE, ENGLISH_LINE
from logger import log as _log
from subtitle_preprocessor import should_skip_short_segment, MIN_LINES_WITH_TEXT


def parse_subtitles(file_content: str, transformers_line: int = TRANSFORMERS_LINE) -> list[list[str]]:
    subtitles = file_content.strip().split('\n\n')
    result = []
    for idx, segment in enumerate(subtitles):
        lines = segment.strip().splitlines()
        if len(lines) < TIME_LINE + 1: continue
        if should_skip_short_segment(lines): continue
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


def diagnose_srt_blocks(file_content: str) -> str:
    """
    解释“为什么一条字幕都解析不出来”。

    parse_subtitles 只用空列表表达失败，日志里只剩一句“无有效字幕”；
    真实原因可能是空文件、缺时间轴，或者缺英文对照行（只有 3 行的单语字幕）。
    把原因说出来，才不用靠猜去判断该修字幕还是该改配置。
    """
    blocks = [b for b in file_content.strip().split('\n\n') if b.strip()]
    if not blocks:
        return "文件为空"

    short_blocks = 0
    no_timeline = 0
    short_line_counts = {}
    for segment in blocks:
        lines = segment.strip().splitlines()
        if len(lines) < MIN_LINES_WITH_TEXT:
            short_blocks += 1
            short_line_counts[len(lines)] = short_line_counts.get(len(lines), 0) + 1
        elif TIME_LINE >= len(lines) or " --> " not in lines[TIME_LINE]:
            no_timeline += 1

    parts = [f"共 {len(blocks)} 段"]
    if short_blocks:
        common_lines = max(short_line_counts.items(), key=lambda kv: kv[1])[0]
        parts.append(
            f"{short_blocks} 段只有 {common_lines} 行"
            f"（本流程要求 序号/时间轴/中文/英文 共 {MIN_LINES_WITH_TEXT} 行，缺对照语言的段落不配音）"
        )
    if no_timeline:
        parts.append(f"{no_timeline} 段缺少合法时间轴（应为 HH:MM:SS,mmm --> HH:MM:SS,mmm）")
    if len(parts) == 1:
        return f"共 {len(blocks)} 段，格式未见异常（解析结果为空，检查文件编码与换行符）"
    return "；".join(parts)


def merge_contiguous_subtitles(subtitles: list[list[str]], max_chars: int = 30) -> list[list[str]]:
    """
    合并时间戳连续的相邻字幕。

    已废弃（deprecated）：主流程不再调用。合并会让内部 index 与用户可见的
    SRT 条目编号脱节，片段一旦错位就无法按字幕编号排查；需要减少请求数时
    请改在服务端做批处理，而不是改变本地索引语义。
    保留此函数仅为兼容旧脚本/测试。

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

