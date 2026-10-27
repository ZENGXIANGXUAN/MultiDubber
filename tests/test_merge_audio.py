"""merge_audio 回归测试：结果需与旧的逐段拼接实现一致，且长音频不能退化。"""

import os
import sys
import tempfile
import unittest
import warnings

import numpy as np
import soundfile as sf
from pydub import AudioSegment

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_processor as ap

warnings.filterwarnings("ignore", category=ResourceWarning)


def ts(ms: int) -> str:
    s, ms2 = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


def legacy_merge(parsed_subtitles, current_tmp_dir):
    """改造前的实现，作为等价性基准。"""
    merged_audio = AudioSegment.empty()
    for i, subtitle in enumerate(parsed_subtitles):
        start_ms, end_ms = ap._subtitle_ms_bounds(subtitle)
        target_duration_ms = end_ms - start_ms
        if len(merged_audio) < start_ms:
            merged_audio += AudioSegment.silent(duration=start_ms - len(merged_audio))
        segment_file = os.path.join(current_tmp_dir, f"output_{i}.wav")
        try:
            audio_segment = AudioSegment.from_file(segment_file)
            merged_audio += ap._fit_audio_segment(audio_segment, target_duration_ms)
        except Exception:
            merged_audio += AudioSegment.silent(duration=target_duration_ms)
    return merged_audio


class MergeAudioTest(unittest.TestCase):
    def setUp(self):
        self._old_log = ap.logger_log
        ap.logger_log = lambda *a, **k: None
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        ap.logger_log = self._old_log
        self._tmp.cleanup()

    def _write_segment(self, index, duration_ms, sample_rate, channels, offset=0):
        frames = int(sample_rate * duration_ms / 1000)
        data = (np.random.default_rng(index + offset).standard_normal(frames) * 0.01).astype(np.float32)
        if channels == 2:
            data = np.stack([data, data], axis=1)
        sf.write(os.path.join(self.root, f"output_{index}.wav"), data, sample_rate, subtype="PCM_16")

    def test_uniform_format_matches_legacy_duration(self):
        subtitles = []
        cursor = 0
        for i in range(20):
            start = cursor + 250
            end = start + 500
            self._write_segment(i, 500, 24000, 1)
            subtitles.append([ts(start), ts(end), f"line {i}", ""])
            cursor = end

        expected = legacy_merge(subtitles, self.root)
        actual = ap.merge_audio(subtitles, self.root)

        # 旧实现用 11025Hz 静音再重采样，每个间隙会少 1~2 帧（<0.1ms）。
        # 新实现按目标帧率精确补齐，因此只要求毫秒级一致 + 帧数差异有界。
        self.assertEqual(len(actual), cursor)
        self.assertEqual(len(actual), len(expected))
        self.assertLessEqual(
            abs(len(actual.raw_data) - len(expected.raw_data)),
            2 * 2 * 20,  # 20 个间隙 × 最多 2 帧 × 2 字节
        )
        self.assertEqual((actual.channels, actual.frame_rate, actual.sample_width), (1, 24000, 2))

        # 第一段位于 250~750ms，随后的 250ms（750~1000ms）必须是纯静音
        bytes_per_ms = 24000 * 2 // 1000
        gap = actual.raw_data[750 * bytes_per_ms:1000 * bytes_per_ms]
        self.assertEqual(len(gap), 250 * bytes_per_ms)
        self.assertFalse(any(gap))
        # 第一段必须有内容
        self.assertTrue(any(actual.raw_data[250 * bytes_per_ms:750 * bytes_per_ms]))

    def test_mixed_formats_promote_to_max(self):
        subtitles = []
        cursor = 0
        formats = [(22050, 1), (44100, 2), (32000, 1), (44100, 2)]
        for i in range(12):
            sr, ch = formats[i % len(formats)]
            start = cursor + 100
            end = start + 600
            self._write_segment(i, 600, sr, ch)
            subtitles.append([ts(start), ts(end), f"line {i}", ""])
            cursor = end

        merged = ap.merge_audio(subtitles, self.root)
        self.assertEqual((merged.channels, merged.frame_rate, merged.sample_width), (2, 44100, 2))
        self.assertAlmostEqual(len(merged), cursor, delta=2)

    def test_empty_input(self):
        self.assertEqual(len(ap.merge_audio([], self.root)), 0)

    def test_missing_segment_becomes_silence(self):
        subtitles = [[ts(0), ts(500), "a", ""], [ts(500), ts(1000), "b", ""]]
        self._write_segment(0, 500, 24000, 1)
        # 故意不写 output_1.wav
        merged = ap.merge_audio(subtitles, self.root)
        self.assertEqual(len(merged), 1000)
        tail = merged.raw_data[500 * 48:]
        self.assertFalse(any(tail))


if __name__ == "__main__":
    unittest.main()
