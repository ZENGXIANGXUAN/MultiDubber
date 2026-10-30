"""run 按钮状态机测试：任务运行中动态增删服务器不得把 PROCESSING... 重新点亮。"""

import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# gui.pyw 不是标准 .py 后缀，用 importlib 显式加载。
_spec = importlib.util.spec_from_file_location(
    "gui_under_test", os.path.join(ROOT, "gui.pyw")
)
gui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gui)


class FakeBtn:
    def __init__(self):
        self.enabled = None
        self.tooltip = None

    def setEnabled(self, v):
        self.enabled = bool(v)

    def setToolTip(self, t):
        self.tooltip = t


class FakeApp:
    """只带 _update_run_btn 所需状态的桩，不实例化 Qt 控件。"""

    _update_run_btn = gui.TTSApp._update_run_btn

    def __init__(self, processing=False, servers=None, folders=None):
        self._processing = processing
        self.run_btn = FakeBtn()
        self._online_servers = servers if servers is not None else {"http://a/": 1}
        self._folder_widgets = folders if folders is not None else {"/srt": object()}

    def _get_server_configs(self):
        return dict(self._online_servers)


class RunBtnStateTest(unittest.TestCase):
    def test_enabled_when_idle_with_servers_and_folders(self):
        app = FakeApp(processing=False)
        app._update_run_btn()
        self.assertTrue(app.run_btn.enabled)
        self.assertEqual(app.run_btn.tooltip, "")

    def test_disabled_when_idle_without_servers(self):
        app = FakeApp(processing=False, servers={})
        app._update_run_btn()
        self.assertFalse(app.run_btn.enabled)
        self.assertIn("API server", app.run_btn.tooltip)

    def test_disabled_when_idle_without_folders(self):
        app = FakeApp(processing=False, folders={})
        app._update_run_btn()
        self.assertFalse(app.run_btn.enabled)
        self.assertIn("SRT folder", app.run_btn.tooltip)

    def test_stays_disabled_while_processing_even_with_servers_added(self):
        """回归：运行中添加服务器 → 探测完成回调 _update_run_btn，按钮必须保持禁用。"""
        app = FakeApp(processing=True, servers={}, folders={})
        app._update_run_btn()
        self.assertFalse(app.run_btn.enabled)

        # 模拟运行中动态加入一台在线服务器（_on_server_check_done 的路径）
        app._online_servers["http://b/"] = 1
        app._folder_widgets["/srt"] = object()
        app._update_run_btn()
        self.assertFalse(app.run_btn.enabled)
        self.assertIn("running", app.run_btn.tooltip.lower())

    def test_stays_disabled_while_processing_even_when_servers_removed(self):
        app = FakeApp(processing=True)
        app._online_servers.clear()
        app._update_run_btn()
        self.assertFalse(app.run_btn.enabled)

    def test_reenabled_after_processing_ends(self):
        app = FakeApp(processing=True)
        app._update_run_btn()
        self.assertFalse(app.run_btn.enabled)

        app._processing = False
        app._update_run_btn()
        self.assertTrue(app.run_btn.enabled)
        self.assertEqual(app.run_btn.tooltip, "")


class StartProcessingGuardTest(unittest.TestCase):
    def test_reentry_ignored_while_processing(self):
        """回归：按钮被误点亮后再次点击，不得启动第二条流水线。"""

        class GuardApp:
            start_processing = gui.TTSApp.start_processing

            def __init__(self):
                self._processing = True
                self.logs = []
                self.started = False

            def append_log(self, text):
                self.logs.append(text)

            def _get_server_configs(self):  # pragma: no cover - 正常不会被调用
                self.started = True
                return {"http://a/": 1}

        app = GuardApp()
        app.start_processing()
        self.assertFalse(app.started)
        self.assertTrue(any("已在运行中" in line for line in app.logs))


if __name__ == "__main__":
    unittest.main()
