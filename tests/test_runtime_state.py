"""运行中服务器池 → 重启上下文的同步测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runtime_state


class RuntimeStateTest(unittest.TestCase):
    def tearDown(self):
        runtime_state.last_job_context = None
        with runtime_state._lock:
            runtime_state.active_servers.clear()
            runtime_state.down_servers.clear()

    def test_update_job_servers_rewrites_context_and_active_set(self):
        runtime_state.register_job_context(
            srt_paths=["demo"],
            server_configs={"http://a/": 1},
            server_protocols={"http://a/": "voxcpm2"},
        )
        runtime_state.mark_server_down("http://a/", "boom")

        runtime_state.update_job_servers(
            {"http://b/": 2}, {"http://b/": "indextts_15"}
        )

        ctx = runtime_state.get_last_job_context()
        self.assertEqual(ctx["server_configs"], {"http://b/": 2})
        self.assertEqual(ctx["server_protocols"], {"http://b/": "indextts_15"})
        self.assertEqual(runtime_state.snapshot()["active_servers"], ["http://b/"])
        self.assertNotIn("http://b/", runtime_state.snapshot()["down_servers"])


if __name__ == "__main__":
    unittest.main()
