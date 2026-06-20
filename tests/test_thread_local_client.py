# tests/test_thread_local_client.py
import sys
import os
import threading
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTTSClientThreadLocal:
    """TTSClient thread-local storage tests"""

    def test_each_thread_gets_own_client(self):
        """Each thread should create its own Client instance"""
        from api_client import TTSClient, _thread_local

        # Reset thread-local state
        if hasattr(_thread_local, "client"):
            delattr(_thread_local, "client")
        if hasattr(_thread_local, "connected_url"):
            delattr(_thread_local, "connected_url")

        clients = {}
        lock = threading.Lock()

        def get_client_in_thread(thread_name):
            # Serialize: mock.patch replaces a module-level attribute (global),
            # so concurrent patches would race. Each thread acquires the lock,
            # patches, creates its thread-local Client, then releases.
            with lock:
                with patch("api_client.Client") as mock_client_cls:
                    mock_instance = MagicMock()
                    mock_client_cls.return_value = mock_instance
                    client = TTSClient.get_client()
                    clients[thread_name] = id(client)

        threads = []
        for i in range(3):
            name = f"Thread-{i}"
            t = threading.Thread(target=get_client_in_thread, args=(name,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # All clients should be different instances
        client_ids = list(clients.values())
        assert len(set(client_ids)) == 3, f"Expected 3 unique clients, got {len(set(client_ids))}"

    def test_same_thread_reuses_client(self):
        """Same thread should reuse its Client if URL unchanged"""
        from api_client import TTSClient, _thread_local

        # Reset
        if hasattr(_thread_local, "client"):
            delattr(_thread_local, "client")
        if hasattr(_thread_local, "connected_url"):
            delattr(_thread_local, "connected_url")

        with patch("api_client.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance

            client1 = TTSClient.get_client()
            client2 = TTSClient.get_client()

            assert client1 is client2, "Same thread should reuse client"
            assert mock_client_cls.call_count == 1, "Client should only be created once"


class TestDispatcherThreadLocal:
    """dispatcher.py thread-local storage tests"""

    def test_each_thread_gets_own_dispatcher_client(self):
        """Each dispatcher worker thread should get its own Client per URL"""
        from unittest.mock import patch, MagicMock
        from dispatcher import _get_client, _thread_local

        # Reset thread-local state
        if hasattr(_thread_local, "client_cache"):
            delattr(_thread_local, "client_cache")

        test_url = "http://test-server:7860/"
        clients = {}
        lock = threading.Lock()

        def get_client_in_thread(thread_name):
            # Serialize: mock.patch replaces a module-level attribute (global),
            # so concurrent patches would race. Each thread acquires the lock,
            # patches, creates its thread-local Client, then releases.
            with lock:
                with patch("dispatcher.Client") as mock_client_cls:
                    mock_instance = MagicMock()
                    mock_client_cls.return_value = mock_instance
                    client = _get_client(test_url)
                    clients[thread_name] = id(client)

        threads = []
        for i in range(3):
            name = f"Worker-{i}"
            t = threading.Thread(target=get_client_in_thread, args=(name,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Each thread should have its own client
        client_ids = list(clients.values())
        assert len(set(client_ids)) == 3, f"Expected 3 unique clients, got {len(set(client_ids))}"

    def test_invalidate_clears_current_thread_only(self):
        """invalidate_client should only clear the calling thread's cache"""
        from unittest.mock import patch, MagicMock
        from dispatcher import _get_client, invalidate_client, _thread_local

        test_url = "http://test-server:7860/"

        # Reset
        if hasattr(_thread_local, "client_cache"):
            delattr(_thread_local, "client_cache")

        with patch("dispatcher.Client") as mock_client_cls:
            # Use side_effect so each call returns a new MagicMock
            mock_client_cls.side_effect = lambda url: MagicMock()

            client1 = _get_client(test_url)
            invalidate_client(test_url)
            client2 = _get_client(test_url)

            # Should create new client after invalidation
            assert mock_client_cls.call_count == 2, f"Expected 2 Client calls, got {mock_client_cls.call_count}"
            assert client1 is not client2, "Should create new client after invalidation"
