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
