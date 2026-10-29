# These tests drive the real manager rather than reimplementing its lock behavior.
# Recording subprocess mocks are used only to count actual signal dispatches.
# A repeated stop request must not send a second SIGINT to a flushing writer.
# The direct-download test holds the file-lifetime context open on a worker thread.
# Shutdown must wait until that context is released by the worker.
# An explicit event proves the closer cannot finish before the file has closed.
# Both worker and closer joins are followed by liveness assertions.
# A process creation test starts a real child while holding the shared condition.
# The concurrent shutdown must observe that child once the launch lock is released.
# The child runs no network requests and receives the manager's real stop signal.
# Test-local managers keep their stopping state separate from the recorder singleton.
# The process-creation failure case proves stopping blocks future disk writes.
# An explicit second shutdown verifies completed shutdown is idempotent.
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.app.graceful_shutdown import GracefulShutdown, ShutdownRequested


class ShutdownTests(unittest.TestCase):
    def manager(self) -> Any:
        return GracefulShutdown(log=lambda *a, **kw: None)

    def test_stopping_blocks_new_writes(self) -> Any:
        manager = self.manager()
        manager.shutdown()
        with self.assertRaises(ShutdownRequested):
            manager.start_process(["must-not-launch"])
        with self.assertRaises(ShutdownRequested):
            with manager.download():
                self.fail("download must not start")
        manager.shutdown()

    def test_only_one_signal_is_sent(self) -> Any:
        manager = self.manager()
        process = Mock()
        process.poll.return_value = None
        manager.stop_process(process)
        manager.stop_process(process)
        process.send_signal.assert_called_once()

    def test_waits_for_download_file_to_close(self) -> Any:
        manager = self.manager()
        opened, allow_close, finished = threading.Event(), threading.Event(), threading.Event()

        def download() -> Any:
            with manager.download():
                opened.set()
                allow_close.wait(5)

        worker = threading.Thread(target=download)
        worker.start()
        self.assertTrue(opened.wait(2))

        def close() -> None:
            manager.shutdown()
            finished.set()

        closer = threading.Thread(target=close)
        closer.start()
        self.assertFalse(finished.wait(0.1))
        allow_close.set()
        closer.join(2)
        worker.join(2)
        self.assertFalse(closer.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())

    def test_process_creation_is_serialized_with_shutdown(self) -> Any:
        manager = self.manager()
        # Holding the lock simulates a launch in progress when shutdown arrives.
        with manager._condition:
            closer = threading.Thread(target=manager.shutdown)
            closer.start()
            process = manager.start_process([sys.executable, "-c", "import time; time.sleep(30)"])
        closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ShutdownRequested):
            manager.start_process(["must-not-launch"])


if __name__ == "__main__":
    unittest.main()
