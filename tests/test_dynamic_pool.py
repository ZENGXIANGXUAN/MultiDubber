"""动态服务器池回归测试（不依赖真实网络）。"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher as d


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class DynamicPoolTest(unittest.TestCase):
    def setUp(self):
        self._orig = (
            d._call_api_on_server,
            d._get_client,
            d.invalidate_client,
            d._close_thread_clients,
        )
        d.reset_circuit_breaker_states()
        d.LIVE_POOL_ENABLED = True

        self.calls = []
        self.lock = threading.Lock()
        self.gates = {}

        d._call_api_on_server = self._fake_call
        d._get_client = lambda url: object()
        d.invalidate_client = lambda *a, **k: None
        d._close_thread_clients = lambda url: None

        self.dispatcher = None

    def tearDown(self):
        if self.dispatcher is not None:
            self.dispatcher.stop()
            self.dispatcher = None
        (
            d._call_api_on_server,
            d._get_client,
            d.invalidate_client,
            d._close_thread_clients,
        ) = self._orig
        d.reset_circuit_breaker_states()

    def _fake_call(self, url, ref_audio_path, gen_text, speed, task_id=None, **kwargs):
        with self.lock:
            self.calls.append((url, task_id))
        event = self.gates.get(task_id)
        if event is not None:
            event.wait(10)
        return f"/tmp/fake_{task_id}.wav", d._FAIL_NONE

    def _make(self, configs, cpu_workers=2):
        self.dispatcher = d.MultiServerDispatcher(configs, cpu_workers=cpu_workers, max_retries=2)
        self.dispatcher.start()
        return self.dispatcher

    def _run_batch(self, count):
        done = []
        tasks = [(i, "r.wav", f"text{i}", 1.0) for i in range(count)]
        thread = threading.Thread(
            target=self.dispatcher.run_feeder,
            args=(
                iter(tasks),
                lambda *a: None,
                lambda idx, url: done.append(idx),
                lambda: False,
            ),
            daemon=True,
        )
        thread.start()
        return thread, done

    def _urls(self):
        with self.lock:
            return {url for url, _ in self.calls}

    # ── 用例 ──────────────────────────────────
    def test_add_server_shares_backlog_immediately(self):
        pool = self._make({"http://a/": 1})
        gate = self.gates[0] = threading.Event()

        thread, done = self._run_batch(8)
        self.assertTrue(wait_for(lambda: ("http://a/", 0) in list(self.calls)))

        self.assertTrue(pool.add_server("http://b/", "indextts_15", 1))
        self.assertTrue(wait_for(lambda: "http://b/" in self._urls()))

        gate.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(done), list(range(8)))
        self.assertEqual(self._urls(), {"http://a/", "http://b/"})
        self.assertEqual(pool.get_protocol("http://b/"), "indextts_15")
        self.assertFalse(pool.add_server("http://b/", "indextts_15", 1))  # 幂等

    def test_remove_server_drains_inflight_task(self):
        pool = self._make({"http://a/": 1, "http://b/": 1})
        self.gates[0] = threading.Event()

        thread, done = self._run_batch(8)
        self.assertTrue(wait_for(lambda: len(self.calls) >= 1))

        with self.lock:
            taken_url = self.calls[0][0]
        self.assertTrue(pool.remove_server(taken_url))
        self.gates[0].set()
        thread.join(10)

        self.assertEqual(sorted(done), list(range(8)))
        with self.lock:
            taken_calls = [c for c in self.calls if c[0] == taken_url]
        self.assertEqual(len(taken_calls), 1, "摘除后不应再从该节点领取新任务")
        self.assertFalse(pool.all_servers_down)

    def test_remove_last_server_pauses_without_abort_and_resumes(self):
        pool = self._make({"http://a/": 1}, cpu_workers=1)
        self.gates[0] = threading.Event()

        thread, done = self._run_batch(4)
        self.assertTrue(wait_for(lambda: ("http://a/", 0) in list(self.calls)))

        self.assertTrue(pool.remove_server("http://a/"))
        self.gates[0].set()
        self.assertTrue(wait_for(lambda: done == [0]))
        time.sleep(0.3)

        self.assertTrue(thread.is_alive(), "空池应挂起等待，而不是结束")
        self.assertEqual(pool.pool_snapshot()["state"], "PAUSED_NO_SERVERS")
        self.assertEqual(pool.pool_snapshot()["queue_size"], 3)
        self.assertFalse(pool.all_servers_down)

        self.assertTrue(pool.add_server("http://b/", "voxcpm2", 1))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(done), [0, 1, 2, 3])
        self.assertEqual(pool.pool_snapshot()["state"], "RUNNING")

    def test_readd_clears_breaker_state(self):
        pool = self._make({"http://a/": 1}, cpu_workers=1)
        d._get_server_state("http://a/").consecutive_failures = 5

        self.assertTrue(pool.remove_server("http://a/"))
        self.assertTrue(pool.add_server("http://a/", "voxcpm2", 1))

        self.assertNotIn("http://a/", d._server_states)

    def test_sentinel_never_loses_priority_comparison(self):
        q = d.queue.PriorityQueue()
        task = d._GpuTask(1, "r.wav", "t", 1.0, None, None)
        q.put(d._SENTINEL)
        q.put(task)  # 触发 task < sentinel 的比较
        self.assertIs(q.get_nowait(), d._SENTINEL)
        self.assertIs(q.get_nowait(), task)

    def test_pool_snapshot_reflects_live_changes(self):
        pool = self._make({"http://a/": 1, "http://b/": 2})
        snap = pool.pool_snapshot()
        self.assertEqual(snap["server_count"], 2)
        self.assertEqual(snap["threads"], 3)
        self.assertTrue(pool.remove_server("http://b/"))
        self.assertTrue(wait_for(lambda: pool.pool_snapshot()["threads"] == 1))
        self.assertEqual(pool.pool_snapshot()["server_count"], 1)

    def test_stop_while_paused_releases_feeder(self):
        pool = self._make({"http://a/": 1}, cpu_workers=1)
        self.gates[0] = threading.Event()
        thread, done = self._run_batch(4)
        self.assertTrue(wait_for(lambda: ("http://a/", 0) in list(self.calls)))

        self.assertTrue(pool.remove_server("http://a/"))
        self.gates[0].set()
        self.assertTrue(wait_for(lambda: done == [0]))
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())

        pool.stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_runtime_helpers_noop_without_active_pool(self):
        d.clear_active_dispatcher()
        self.assertFalse(d.runtime_add_server("http://x/", "voxcpm2", 1))
        self.assertFalse(d.runtime_remove_server("http://x/"))


if __name__ == "__main__":
    unittest.main()
