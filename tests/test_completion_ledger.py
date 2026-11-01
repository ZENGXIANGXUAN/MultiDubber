"""完成记录回归测试：成品被搬走后，不能把已经配音过的任务再配一遍。

场景来源：用户把 中配/<name>.mp4 移出目录（交付/清空间），下一轮运行时
完成状态只剩“文件在不在”，于是整套 GPU 时间重跑一遍。
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import main as pipeline

SRT = (
    "1\n00:00:00,100 --> 00:00:02,200\n第一句\nFirst line\n\n"
    "2\n00:00:02,200 --> 00:00:04,500\n第二句\nSecond line\n\n"
)


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="\n") as fp:
        fp.write(text)


class CompletionLedgerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ledger_test_")
        self.name = "Video A [abc]"
        self.srt = os.path.join(self.folder, f"{self.name}.srt")
        self.video = os.path.join(self.folder, f"{self.name}.mp4")
        write(self.srt, SRT)
        write(self.video, "source-video-bytes")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_record_then_recognise_after_output_moved_away(self):
        output = os.path.join(self.folder, "中配", f"{self.name}.mp4")
        write(output, "dubbed-video")

        pipeline._record_completion(self.folder, self.name, self.srt, f"{self.name}.mp4")
        self.assertTrue(os.path.isfile(pipeline._ledger_path(self.folder)))

        # 用户把成品搬走
        os.remove(output)

        entry = pipeline._load_ledger(self.folder).get(self.name)
        self.assertTrue(
            pipeline._ledger_matches(
                entry, self.srt, pipeline._video_size_for(self.name, self.folder)
            ),
            "成品搬走后仍应认出这条已经配音过",
        )

    def test_changed_subtitle_forces_redub(self):
        pipeline._record_completion(self.folder, self.name, self.srt, f"{self.name}.mp4")
        write(self.srt, SRT + SRT)  # 字幕内容变了

        entry = pipeline._load_ledger(self.folder).get(self.name)
        self.assertFalse(
            pipeline._ledger_matches(
                entry, self.srt, pipeline._video_size_for(self.name, self.folder)
            ),
            "字幕改过必须重配",
        )

    def test_replaced_source_video_forces_redub(self):
        pipeline._record_completion(self.folder, self.name, self.srt, f"{self.name}.mp4")
        write(self.video, "another-source-video-with-different-length")

        entry = pipeline._load_ledger(self.folder).get(self.name)
        self.assertFalse(
            pipeline._ledger_matches(
                entry, self.srt, pipeline._video_size_for(self.name, self.folder)
            ),
            "源视频换了必须重配",
        )

    def test_pipeline_skips_ledger_done_items_without_tts(self):
        """整条流程在“成品已被搬走 + 有完成记录”时应当直接跳过，不启动 TTS。"""
        pipeline._record_completion(self.folder, self.name, self.srt, f"{self.name}.mp4")

        previous_abort = config.ABORT_ALL
        config.ABORT_ALL = False
        try:
            ok = pipeline.process_srt_files([self.folder], max_workers=1)
        finally:
            config.ABORT_ALL = previous_abort

        self.assertTrue(ok, "全部命完成记录时应视为成功")
        self.assertFalse(
            os.path.exists(os.path.join(self.folder, "tmp")),
            "命中完成记录时不应产生 tmp 片段目录",
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.folder, "REF_AUDIO_PATH", f"{self.name}.wav")),
            "命中完成记录时不应提取参考音频",
        )


if __name__ == "__main__":
    unittest.main()
