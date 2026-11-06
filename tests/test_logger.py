from __future__ import annotations

import importlib
import io
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

import src.support.logger as logger_mod


@pytest.fixture()
def reload_logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # 把 _app_root() 的落点（sys.argv[0] 所在目录）指到 tmp_path，隔离 config/ logs/ 读写；
    # sys.stderr 置 None 跳过控制台 sink，用例仅关注文件 sink 的创建与否（add() 即建文件）。
    # 重载会清空全局 sink，退出时一并复位，避免 tmp 句柄/异步写线程泄漏给其他用例
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "entry.py")])
    monkeypatch.setattr(sys, "stderr", None)
    yield tmp_path
    logger.complete()
    logger.remove()
    # LT-08：三个 sink id 都要复位。重载会往全局 loguru 注册文件 sink，只清 _console_sink_id
    # 时两个录制 sink 的 id 会带着已随用例清理的句柄留给下一个用例——remove_file_sinks() /
    # add_file_sinks() 对着死 id 走幂等分支，而「归档后重建 sink」这类用例判的就是这两个 id。
    # 与同构 fixture tests/test_log_archive.py::TestLoggerFileSinkOps.reload_logger 保持一致。
    logger_mod._console_sink_id = None
    logger_mod._streamget_sink_id = None
    logger_mod._playurl_sink_id = None


def _write_config(tmp_path: Path, enabled: str) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.ini").write_text(f"[录制设置]\n是否启用日志文件(是/否) = {enabled}\n", encoding="utf-8")


def test_recorder_process_owns_recording_log_files(reload_logger: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLI / Web 进程正常持有两份录制日志。
    _write_config(reload_logger, "是")
    importlib.reload(logger_mod)
    assert (reload_logger / "logs" / "streamget.log").is_file()
    assert (reload_logger / "logs" / "PlayURL.log").is_file()


def test_import_time_console_sink_colorize_follows_isatty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # L-16 回归锁：导入期控制台 sink 的 colorize 必须按 isatty 判定，并与 rebind_console_sink()
    # 共用 src/support/logger.py::_console_colorize。旧形态导入期硬编码 colorize=True，stderr 被重定向到
    # 文件/管道时（web.py 后台模式、`python -m src.main > x.log`）ANSI 转义序列照样写进日志内容，
    # 而排障恰恰要看这个文件。两侧都钉：非 tty 不得有 ESC、真 tty 仍须有 ESC（防「恒 False」退化）。
    # 关掉文件 sink（配置「否」），本用例只观察控制台这一路，也不往 tmp_path 外落文件。
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "entry.py")])
    _write_config(tmp_path, "否")
    redirected = io.StringIO()  # StringIO.isatty() 恒 False = 重定向形态
    monkeypatch.setattr(sys, "stderr", redirected)
    try:
        importlib.reload(logger_mod)
        logger.info("着色判定")
        logger.complete()
        assert "\x1b" not in redirected.getvalue(), repr(redirected.getvalue())
        assert "着色判定" in redirected.getvalue(), "控制台 sink 根本没注册上，上一条断言是假绿"

        class _TtyStringIO(io.StringIO):
            # 真终端形态：只有 isatty() 这一处不同
            def isatty(self) -> bool:
                return True

        terminal = _TtyStringIO()
        monkeypatch.setattr(sys, "stderr", terminal)
        importlib.reload(logger_mod)
        logger.info("着色判定")
        logger.complete()
        assert "\x1b" in terminal.getvalue(), "tty 形态被一并关掉着色（判据恒 False）"
    finally:
        # 与 reload_logger fixture 同款复位：三个 sink id 都要清，tmp 句柄不留给后续用例
        logger.complete()
        logger.remove()
        logger_mod._console_sink_id = None
        logger_mod._streamget_sink_id = None
        logger_mod._playurl_sink_id = None
