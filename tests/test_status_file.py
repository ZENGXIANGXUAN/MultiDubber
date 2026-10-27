"""状态文件原子写入回归测试。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils


class StatusFileTest(unittest.TestCase):
    def test_save_status_is_atomic_and_valid(self):
        with tempfile.TemporaryDirectory() as root:
            status_file = os.path.join(root, "tmp", "status.json")
            for count in (1, 5, 20):
                utils.save_status(status_file, "demo.srt", set(range(count)))
                with open(status_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["current_file"], "demo.srt")
                self.assertEqual(data["completed_indices"], list(range(count)))
            self.assertFalse(os.path.exists(status_file + ".part"))
            self.assertEqual(utils.load_status(status_file, "demo.srt"), set(range(20)))
            self.assertEqual(utils.load_status(status_file, "other.srt"), set())


if __name__ == "__main__":
    unittest.main()
