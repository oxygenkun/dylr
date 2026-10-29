import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable


class ShutdownRequested(RuntimeError):
    pass


class GracefulShutdown:
    def __init__(self, log: Callable[..., Any] = print) -> None:
        self.stopping = threading.Event()
        self._condition = threading.Condition(threading.RLock())
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._signalled: set[subprocess.Popen[bytes]] = set()
        self._downloads = 0
        self._finished = False
        self._log = log

    @contextmanager
    def launch(self) -> Iterator[None]:
        with self._condition:
            if self.stopping.is_set():
                raise ShutdownRequested("Recorder is shutting down")
            yield

    def track(self, process: subprocess.Popen[bytes]) -> None:
        with self._condition:
            finished = {p for p in self._processes if p.poll() is not None}
            self._processes.difference_update(finished)
            self._signalled.difference_update(finished)
            self._processes.add(process)

    def owns(self, process: subprocess.Popen[bytes]) -> bool:
        with self._condition:
            return process in self._processes

    def start_process(
        self, command: list[str], *, popen_factory: Callable[..., subprocess.Popen[bytes]] | None = None, **kwargs: Any
    ) -> subprocess.Popen[bytes]:
        with self._condition:
            if self.stopping.is_set():
                raise ShutdownRequested("Recorder is shutting down")
            finished = {p for p in self._processes if p.poll() is not None}
            self._processes.difference_update(finished)
            self._signalled.difference_update(finished)
            if os.name != "nt":
                kwargs.setdefault("start_new_session", True)
            process = (popen_factory or subprocess.Popen)(command, **kwargs)
            self._processes.add(process)
            return process

    def forget(self, process: subprocess.Popen[bytes]) -> None:
        with self._condition:
            self._processes.discard(process)
            self._signalled.discard(process)

    def stop_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._condition:
            if process in self._signalled or process.poll() is not None:
                return
            try:
                if os.name == "nt":
                    if process.stdin:
                        process.stdin.write(b"q\n")
                        process.stdin.flush()
                    else:
                        raise RuntimeError("FFmpeg requires stdin for graceful Windows shutdown")
                else:
                    process.send_signal(signal.SIGINT)
                self._signalled.add(process)
            except ProcessLookupError:
                pass

    def check_output(self, command: list[str], **kwargs: Any) -> bytes:
        process = self.start_process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, **kwargs)
        try:
            output, _ = process.communicate()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, output=output)
            if self.stopping.is_set():
                raise ShutdownRequested("Conversion interrupted; retain original file")
            return output
        finally:
            if process.poll() is not None:
                self.forget(process)

    @contextmanager
    def download(self) -> Iterator[None]:
        with self._condition:
            if self.stopping.is_set():
                raise ShutdownRequested("Recorder is shutting down")
            self._downloads += 1
        try:
            yield
        finally:
            with self._condition:
                self._downloads -= 1
                self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            if self._finished:
                return
            self.stopping.set()
            processes = [p for p in self._processes if p.poll() is None]
        self._log(
            f"Graceful shutdown: closing {len(processes)} FFmpeg process(es); waiting for output files.", flush=True
        )
        for process in processes:
            self.stop_process(process)
        next_log = time.monotonic() + 10
        with self._condition:
            while any(p.poll() is None for p in processes) or self._downloads:
                self._condition.wait(timeout=0.2)
                if time.monotonic() >= next_log:
                    self._log("Graceful shutdown: still waiting for output files; do not force kill.", flush=True)
                    next_log = time.monotonic() + 10
        self._finished = True
        self._log("Graceful shutdown: all recording outputs closed.", flush=True)


shutdown_manager = GracefulShutdown()
