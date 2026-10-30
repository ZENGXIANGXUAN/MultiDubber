"""音频完整性相关逻辑的回归测试。

覆盖 2026-10-30 事故暴露的三条链路：
  1. 字幕布局变化后，旧 status.json 必须作废（否则片段错位复用）
  2. 并发串台产生的“不同文本、相同音频”必须被指纹判重拦下
  3. 时长调节失败必须仍然输出目标时长（不能把短音频原样落盘）
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils
import dispatcher
from dispatcher import (
    _ProductRegistry,
    materialize_private_audio,
    discard_private_audio,
    is_owned_raw_path,
)
from audio_processor import adjust_duration_with_rubberband


def _write_bytes(path, payload: bytes):
    with open(path, "wb") as f:
        f.write(payload)


class LayoutSignatureTest(unittest.TestCase):
    def test_signature_changes_when_text_changes(self):
        a = [["00:00:01,000", "00:00:03,000", "第一句", "first"]]
        b = [["00:00:01,000", "00:00:03,000", "第二句", "first"]]
        self.assertNotEqual(
            utils.compute_layout_signature(a),
            utils.compute_layout_signature(b),
        )

    def test_signature_changes_when_merge_removed(self):
        # 合并前后的布局指纹必须不同：这正是取消字幕合并后旧进度需要作废的原因。
        merged = [["00:00:01,000", "00:00:05,000", "第一句第二句", "a b"]]
        original = [
            ["00:00:01,000", "00:00:03,000", "第一句", "a"],
            ["00:00:03,000", "00:00:05,000", "第二句", "b"],
        ]
        self.assertNotEqual(
            utils.compute_layout_signature(merged),
            utils.compute_layout_signature(original),
        )

    def test_load_status_discards_progress_on_layout_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_file = os.path.join(tmp, "status.json")
            subs = [["00:00:01,000", "00:00:03,000", "第一句", "a"]]
            sig = utils.compute_layout_signature(subs)
            utils.save_status(status_file, "demo.srt", {0, 1, 2}, sig)

            self.assertEqual(utils.load_status(status_file, "demo.srt", sig), {0, 1, 2})
            # 布局变了 → 旧进度必须整体作废
            self.assertEqual(utils.load_status(status_file, "demo.srt", "deadbeefdeadbeef"), set())
            # 不传签名时保持旧行为（兼容老测试/老状态文件）
            self.assertEqual(utils.load_status(status_file, "demo.srt"), {0, 1, 2})

    def test_status_is_layout_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_file = os.path.join(tmp, "status.json")
            sig = "aaaabbbbccccdddd"
            utils.save_status(status_file, "demo.srt", {0}, sig)

            self.assertTrue(utils.status_is_layout_compatible(status_file, "demo.srt", sig))
            self.assertFalse(utils.status_is_layout_compatible(status_file, "demo.srt", "other"))
            # 没有 layout_sig 的旧状态无法证明 index 语义一致 → 必须重跑
            utils.save_status(status_file, "demo.srt", {0})
            self.assertFalse(utils.status_is_layout_compatible(status_file, "demo.srt", sig))
            # 不存在的状态文件视为兼容（等价于全新任务）
            self.assertTrue(utils.status_is_layout_compatible(
                os.path.join(tmp, "none.json"), "demo.srt", sig))


class ProductRegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = _ProductRegistry()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name, payload):
        path = os.path.join(self.tmp.name, name)
        _write_bytes(path, payload)
        return path

    def test_detects_same_audio_with_different_text(self):
        p1 = self._path("a.wav", b"RIFF-identical-payload")
        p2 = self._path("b.wav", b"RIFF-identical-payload")
        dup, _ = self.registry.check(4, "完整拆解夺冠策略。最简单的讲法", p1)
        self.assertFalse(dup)
        dup, owner = self.registry.check(7, "而是等它开始往一侧逼仓。一旦逼仓失败", p2)
        self.assertTrue(dup)
        self.assertEqual(owner, 4)

    def test_allows_same_audio_with_same_text(self):
        p1 = self._path("a.wav", b"RIFF-same")
        p2 = self._path("b.wav", b"RIFF-same")
        self.registry.check(1, "对，就是这同一套东西。", p1)
        dup, _ = self.registry.check(9, "对，就是这同一套东西。", p2)
        self.assertFalse(dup, "SRT 里重复出现的同一句话不应被判为串台")

    def test_allows_retry_of_same_task(self):
        p1 = self._path("a.wav", b"RIFF-x")
        p2 = self._path("b.wav", b"RIFF-x")
        self.registry.check(3, "第一次尝试的文本内容", p1)
        dup, _ = self.registry.check(3, "第一次尝试的文本内容", p2)
        self.assertFalse(dup, "同一任务重试拿到相同内容应放行")

    def test_ignores_very_short_text(self):
        p1 = self._path("a.wav", b"RIFF-y")
        p2 = self._path("b.wav", b"RIFF-y")
        self.registry.check(1, "嗯", p1)
        dup, _ = self.registry.check(2, "啊", p2)
        self.assertFalse(dup)

    def test_new_digest_replaces_previous_registration(self):
        p1 = self._path("a.wav", b"RIFF-old")
        p2 = self._path("b.wav", b"RIFF-new")
        self.registry.check(5, "同一任务的第一版文本", p1)
        dup, _ = self.registry.check(5, "同一任务的第二版文本", p2)
        self.assertFalse(dup)
        # 旧指纹应已被移除，不再挡住别的任务
        dup, _ = self.registry.check(6, "另一个任务的文本内容", p1)
        self.assertFalse(dup)


class PrivateAudioTest(unittest.TestCase):
    def test_materialize_creates_independent_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "shared.wav")
            _write_bytes(src, b"RIFF-shared-content")
            private = materialize_private_audio(src, 42)
            try:
                self.assertIsNotNone(private)
                self.assertNotEqual(os.path.abspath(private), os.path.abspath(src))
                self.assertTrue(is_owned_raw_path(private))
                # 源文件被删除后，私有副本仍然可用
                os.remove(src)
                self.assertTrue(os.path.exists(private))
                with open(private, "rb") as f:
                    self.assertEqual(f.read(), b"RIFF-shared-content")
            finally:
                discard_private_audio(private)

    def test_discard_never_touches_external_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            external = os.path.join(tmp, "shared.wav")
            _write_bytes(external, b"RIFF-x")
            discard_private_audio(external)
            self.assertTrue(os.path.exists(external), "非私有目录的文件绝不能被删除")

    def test_non_stage_path_is_not_owned(self):
        self.assertFalse(is_owned_raw_path(None))
        self.assertFalse(is_owned_raw_path(""))
        self.assertFalse(is_owned_raw_path(os.path.join(tempfile.gettempdir(), "gradio", "x.wav")))


class DurationFallbackTest(unittest.TestCase):
    def test_failed_stretch_still_yields_target_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_input = os.path.join(tmp, "not-exists.wav")
            output = os.path.join(tmp, "out.wav")
            ok = adjust_duration_with_rubberband(missing_input, output, 2.5)
            self.assertFalse(ok, "输入不可读时应当返回 False（表示走了降级路径）")
            self.assertTrue(os.path.exists(output), "降级路径也必须产出文件")
            from pydub import AudioSegment
            duration_ms = len(AudioSegment.from_file(output))
            self.assertAlmostEqual(duration_ms / 1000.0, 2.5, delta=0.08)


class ApiProductIsolationTest(unittest.TestCase):
    """端到端复现本次事故：同一份音频被返回给两个不同文本的任务。"""

    def setUp(self):
        from pydub import AudioSegment
        self.tmp = tempfile.TemporaryDirectory()
        dispatcher._PRODUCT_REGISTRY.reset()

        # 模拟服务端对两个请求都返回同一个文件（固定输出名的典型后果）
        self.shared_audio = os.path.join(self.tmp.name, "gen_single_output_0.wav")
        AudioSegment.silent(duration=1500).export(self.shared_audio, format="wav")

        self.ref_audio = os.path.join(self.tmp.name, "ref.wav")
        AudioSegment.silent(duration=1500).export(self.ref_audio, format="wav")

    def tearDown(self):
        self.tmp.cleanup()
        dispatcher._PRODUCT_REGISTRY.reset()

    def _call(self, text, task_id):
        from unittest import mock
        with mock.patch("dispatcher._get_client", return_value=object()), \
             mock.patch("dispatcher.indextts_15.predict_audio", return_value=self.shared_audio):
            return dispatcher._call_api_on_server(
                "http://fake-server", self.ref_audio, text, 1.0,
                task_id=task_id, protocol="indextts_15",
            )

    def test_second_task_with_different_text_is_rejected(self):
        path_a, kind_a = self._call("完整拆解夺冠策略。最简单的讲法，是把它拆成四个步骤。", 4)
        self.assertEqual(kind_a, dispatcher._FAIL_NONE)
        self.assertTrue(path_a and os.path.exists(path_a))
        self.assertTrue(is_owned_raw_path(path_a), "返回路径必须是任务独占副本")
        self.assertNotEqual(
            os.path.abspath(path_a), os.path.abspath(self.shared_audio),
            "不能把 gradio_client 的共享下载路径直接交给后处理",
        )

        path_b, kind_b = self._call("而是等它开始往一侧逼仓。一旦逼仓失败，就造出一批被套住的人。", 7)
        self.assertIsNone(path_b)
        self.assertEqual(kind_b, dispatcher._FAIL_API, "串台产物必须判为失败并重试")

        # 被丢弃的产物不应留在临时目录
        discard_private_audio(path_a)

    def test_same_text_repeated_is_allowed(self):
        text = "对，就是这同一套东西。"
        path_a, kind_a = self._call(text, 1)
        path_b, kind_b = self._call(text, 2)
        try:
            self.assertEqual(kind_a, dispatcher._FAIL_NONE)
            self.assertEqual(kind_b, dispatcher._FAIL_NONE, "重复台词不应被判为串台")
        finally:
            discard_private_audio(path_a)
            discard_private_audio(path_b)


class SegmentInspectionTest(unittest.TestCase):
    """复现事故里的片段形态：目标 10.4s，实际只有 6.5s 语音 + 3.9s 补零。"""

    def test_short_segment_is_reported(self):
        from pydub import AudioSegment
        from audio_processor import inspect_audio_segments, merge_audio

        with tempfile.TemporaryDirectory() as tmp:
            subs = [
                ["00:00:00,000", "00:00:10,400", "完整拆解夺冠策略。最简单的讲法", "en1"],
                ["00:00:10,400", "00:00:14,000", "而是等它开始往一侧逼仓", "en2"],
            ]
            # 第一段只给 7.5s（事故里 index 4 拿到的正是 index 7 的 7.5s 音频）
            AudioSegment.silent(duration=7500).export(os.path.join(tmp, "output_0.wav"), format="wav")
            AudioSegment.silent(duration=3600).export(os.path.join(tmp, "output_1.wav"), format="wav")

            issues = inspect_audio_segments(subs, tmp)
            types = {i["index"]: i["type"] for i in issues}
            self.assertEqual(types.get(0), "duration_mismatch")
            self.assertIsNone(types.get(1))

            # 合并后总时长仍等于时间轴末尾（静音补齐），但日志会明确报出来
            merged = merge_audio(subs, tmp)
            self.assertAlmostEqual(len(merged) / 1000.0, 14.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
