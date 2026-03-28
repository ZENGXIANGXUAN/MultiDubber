from typing import List
from config import TIME_LINE, TRANSFORMERS_LINE, ENGLISH_LINE, MAX_SUBTITLE_LENGTH

def parse_subtitles(file_content: str, transformers_line: int = TRANSFORMERS_LINE) -> List[List]:
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
            print(f"段落 {idx} 解析时间错误: {e}")
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


def merge_consecutive_subtitles(subtitles: List[List]) -> List[List]:
    if not subtitles: return []
    merged = []
    current_start, current_end, current_text, current_english = subtitles[0]
    for i in range(1, len(subtitles)):
        next_start, next_end, next_text, next_english = subtitles[i]
        if current_end == next_start and len(current_text + next_text) <= MAX_SUBTITLE_LENGTH:
            current_end = next_end
            current_text += next_text
            current_english += next_english
        else:
            merged.append([current_start, current_end, current_text, current_english])
            current_start, current_end, current_text, current_english = next_start, next_end, next_text, next_english
    merged.append([current_start, current_end, current_text, current_english])
    return merged
