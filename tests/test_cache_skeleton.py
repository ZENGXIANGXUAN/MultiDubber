"""骨架缓存回归测试：缓存必须始终镜像源目录的字幕集合。

历史问题：init_skeleton 见到 .skeleton_ready 就直接返回，源目录后来新增的字幕
永远进不来，而源目录已删除的字幕留在缓存里被当成任务 —— 日志里就表现为
“处理的清单和目录里的文件对不上”，最后一次运行还把新视频漏掉了。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local_cache_manager import LocalCacheManager

SRT = (
    "1\n00:00:00,100 --> 00:00:04,200\n中文一行\nEnglish line\n\n"
    "2\n00:00:04,200 --> 00:00:08,500\n中文二行\nSecond line\n\n"
)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)


def subtitle_names(folder):
    return sorted(
        name for name in os.listdir(folder)
        if name.lower().endswith(".srt")
        and os.path.isfile(os.path.join(folder, name))
    )


class SkeletonMirrorTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="skeleton_test_")
        self.src = os.path.join(self.root, "src", "IQCapital")
        self.cache_root = os.path.join(self.root, "cache")
        os.makedirs(self.src, exist_ok=True)
        self.manager = LocalCacheManager(self.cache_root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_mirror_follows_source_add_remove_and_update(self):
        write(os.path.join(self.src, "A.srt"), SRT)
        write(os.path.join(self.src, "B.srt"), SRT)
        write(os.path.join(self.src, "B.mp4"), "video")
        write(os.path.join(self.src, "中配", "A.mp4"), "done")

        cache = self.manager.init_skeleton(self.src)
        self.assertEqual(subtitle_names(cache), ["A.srt", "B.srt"])
        # 中配属于产物目录，不进入骨架
        self.assertFalse(os.path.exists(os.path.join(cache, "中配")))

        time.sleep(1.1)
        write(os.path.join(self.src, "C.srt"), SRT)       # 新增
        os.remove(os.path.join(self.src, "B.srt"))        # 源目录删除
        write(os.path.join(self.src, "A.srt"), SRT * 3)   # 内容变化

        cache = self.manager.init_skeleton(self.src)
        self.assertEqual(subtitle_names(cache), ["A.srt", "C.srt"])
        self.assertEqual(
            os.path.getsize(os.path.join(cache, "A.srt")),
            os.path.getsize(os.path.join(self.src, "A.srt")),
        )

        quarantined = []
        for _root, _dirs, files in os.walk(os.path.join(cache, ".orphaned")):
            quarantined.extend(files)
        self.assertEqual(quarantined, ["B.srt"])

    def test_orphan_in_cache_is_quarantined_not_processed(self):
        write(os.path.join(self.src, "A.srt"), SRT)
        cache = self.manager.init_skeleton(self.src)
        # 模拟历史遗留：缓存里的字幕在源目录根本不存在
        write(os.path.join(cache, "doctor-who.fixed.srt"), SRT)

        cache = self.manager.init_skeleton(self.src)
        self.assertEqual(subtitle_names(cache), ["A.srt"])
        self.assertTrue(
            os.path.exists(os.path.join(cache, ".orphaned"))
        )

    def test_empty_source_scan_does_not_wipe_cache(self):
        write(os.path.join(self.src, "A.srt"), SRT)
        cache = self.manager.init_skeleton(self.src)

        # 源目录还在但一个字幕都没有（云盘未挂载/刚清空）→ 不能隔离缓存
        os.remove(os.path.join(self.src, "A.srt"))
        self.manager.init_skeleton(self.src)
        self.assertEqual(subtitle_names(cache), ["A.srt"])

    def test_unchanged_files_are_not_recopied(self):
        write(os.path.join(self.src, "A.srt"), SRT)
        self.manager.init_skeleton(self.src)

        state = dict(self.manager.stats)
        copy_count_before = state["files_copied"]
        self.manager.init_skeleton(self.src)
        self.assertEqual(self.manager.stats["files_copied"], copy_count_before)


if __name__ == "__main__":
    unittest.main()
