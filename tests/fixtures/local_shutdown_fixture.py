# This fixture is intended for a disposable process or container, never production output.
# The driver sets SHUTDOWN_TEST_OUTPUT to its own temporary directory.
# It imports the real main module, including the production signal handler.
# All network checks are disabled; inputs are FFmpeg's synthetic testsrc2 frames.
# Two concurrent recordings cover normal and segmented MP4 trailer finalization.
# The segmented path uses the same fragment options as the production builder.
# Readiness is published only after both child processes are registered and alive.
# The parent driver then sends SIGTERM to this Python process.
# FFmpeg children run in new sessions, preventing inherited duplicate stop signals.
# Success requires every output file to pass ffprobe and complete ffmpeg decoding.
# The fixture waits indefinitely only so the parent can test real signal delivery.
# All generated video and log files are removed by the driver's temporary context.
# Disposable harness using the actual recorder and signal handler, with synthetic video only.
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.argv = [str(ROOT / "src/recording/engine.py")]
os.environ["DOUYIN_SKIP_RUNTIME_CHECK"] = "1"
os.environ["DOUYIN_LOG_NO_ENQUEUE"] = "1"
os.environ["DOUYIN_DISABLE_LOG_ARCHIVE"] = "1"

from src.app import graceful_shutdown
from src.recording import engine as main
from src.recording import session as recording
from src.recording import state


def run(output: Path) -> None:
    signal.signal(signal.SIGTERM, main.safe_exit)
    signal.signal(signal.SIGINT, main.safe_exit)
    output.mkdir(parents=True, exist_ok=True)
    state.create_time_file = False
    state.split_video_by_time = True
    state.recording_enabled = True
    for kind in ["normal", "segmented"]:
        path = output / ("normal.mp4" if kind == "normal" else "segment_%03d.mp4")
        command = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-c:v",
            "mpeg4",
            "-g",
            "10",
        ]
        if kind == "segmented":
            command += [
                "-f",
                "segment",
                "-segment_time",
                "2",
                "-segment_format",
                "mp4",
                "-reset_timestamps",
                "1",
                "-segment_format_options",
                "movflags=+frag_keyframe+empty_moov",
            ]
        else:
            command += ["-f", "mp4"]
        command.append(str(path))
        threading.Thread(target=recording.check_subprocess, args=(kind, kind, command, "MP4"), daemon=True).start()
    time.sleep(5)
    assert len([p for p in graceful_shutdown.shutdown_manager._processes if p.poll() is None]) == 2
    (output / "ready").write_text("ready")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    run(Path(os.environ["SHUTDOWN_TEST_OUTPUT"]))
