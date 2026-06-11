# tests/test_subtitle_merge.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subtitle_parser import merge_contiguous_subtitles


class TestMergeContiguousSubtitles:
    """merge_contiguous_subtitles 的单元测试"""

    def test_basic_merge(self):
        """连续字幕在字数限制内应合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
            ["00:00:06,000", "00:00:08,000", "再见。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2
        assert result[0] == ["00:00:01,000", "00:00:05,000", "你好，欢迎来到。", ""]
        assert result[1] == ["00:00:06,000", "00:00:08,000", "再见。", ""]

    def test_no_merge_non_contiguous(self):
        """时间戳不连续的字幕不应合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:04,000", "00:00:06,000", "世界。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2

    def test_no_merge_exceeds_max_chars(self):
        """合并后超过字数限制的不应合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "这是一段比较长的字幕内容，需要超过限制才行", ""],
            ["00:00:03,000", "00:00:05,000", "再加上这一段就会超过限制了。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2

    def test_chain_merge(self):
        """支持链式合并：A+B 合并后继续判断 (A+B)+C"""
        input_data = [
            ["00:00:01,000", "00:00:02,000", "你", ""],
            ["00:00:02,000", "00:00:03,000", "好", ""],
            ["00:00:03,000", "00:00:04,000", "呀", ""],
            ["00:00:05,000", "00:00:06,000", "再见", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 2
        assert result[0] == ["00:00:01,000", "00:00:04,000", "你好呀", ""]
        assert result[1] == ["00:00:05,000", "00:00:06,000", "再见", ""]

    def test_disabled_merge_zero(self):
        """max_chars=0 时禁用合并，返回原列表"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=0)
        assert len(result) == 2
        assert result == input_data

    def test_disabled_merge_negative(self):
        """max_chars 为负数时禁用合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=-1)
        assert result == input_data

    def test_empty_list(self):
        """空列表应返回空列表"""
        result = merge_contiguous_subtitles([], max_chars=30)
        assert result == []

    def test_single_item(self):
        """单条字幕应原样返回"""
        input_data = [["00:00:01,000", "00:00:03,000", "你好", ""]]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert result == input_data

    def test_all_mergeable(self):
        """所有字幕都可合并时，合并为 1 条"""
        input_data = [
            ["00:00:01,000", "00:00:02,000", "你", ""],
            ["00:00:02,000", "00:00:03,000", "好", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1
        assert result[0] == ["00:00:01,000", "00:00:03,000", "你好", ""]

    def test_english_text_merged(self):
        """英文文本应随中文一起合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好", "Hello"],
            ["00:00:03,000", "00:00:05,000", "世界", "World"],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1
        assert result[0][3] == "HelloWorld"

    def test_does_not_mutate_input(self):
        """不应修改原始输入数据"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "你好，", ""],
            ["00:00:03,000", "00:00:05,000", "欢迎来到。", ""],
        ]
        original_end_time = input_data[0][1]
        original_text = input_data[0][2]
        merge_contiguous_subtitles(input_data, max_chars=30)
        assert input_data[0][1] == original_end_time
        assert input_data[0][2] == original_text

    def test_whitespace_in_timestamps(self):
        """时间戳前后有空格时应能正确比较"""
        input_data = [
            ["00:00:01,000", " 00:00:03,000 ", "你好，", ""],
            [" 00:00:03,000 ", "00:00:05,000", "欢迎来到。", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1

    def test_empty_text_subtitle(self):
        """text 为空的字幕应正常参与合并"""
        input_data = [
            ["00:00:01,000", "00:00:03,000", "", ""],
            ["00:00:03,000", "00:00:05,000", "内容", ""],
        ]
        result = merge_contiguous_subtitles(input_data, max_chars=30)
        assert len(result) == 1
        assert result[0][2] == "内容"
