# -*- coding: utf-8 -*-
# y123 容器部署补丁回归锁：Web 面板「启动即开录」只由环境变量
# DOUYIN_WEB_AUTO_START_RECORDING 决定，且**只决定引擎线程拉起前的初值**。
#
# 背景：上游 2026-08-29 起把 Web 面板改成「默认不自动录制」，启动后必须点面板的
# 「开始录制」（POST /api/recording/toggle）才会拉起房间线程。容器里的无人值守部署
# 没有这个点击动作，表现为「重启后一直不录，直到人打开面板点一下」。
#
# 为什么锁住两种形态而不是只锁开启：这段代码是上游文件，本地补丁要保证
# ① 不设变量时行为与上游逐字一致（面板默认停止态）；② 设了真值才开录，
# 且 0/false/no 之类必须回落停止态——否则一台机器设过 0 也会偷偷开录。
#
# 被测对象是 web.py::main 的接线：`import main` / `uvicorn` / `src.api.routes` 统一换成
# sys.modules 替身（与被测文件同目录的 test_web_background_gate_order.py 同法），
# read_web_config 走 src/api/config.py 真实实现——在测试里重写它等于假绿。

import io
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

import src.recording
from src import web

_ENV_KEY = "DOUYIN_WEB_AUTO_START_RECORDING"


class _FakeEngineModule:
    # 代替 `main` 模块对象：web.run() 只读四个路径属性，写 recording_enabled，
    # 再在退出链调用两个收尾函数。main.main（录制主循环）被守护线程调用，直接返回。
    def __init__(self, root: Path, config_file: Path) -> None:
        self.script_path = str(root)
        self.config_file = str(config_file)
        self.legacy_config_file = str(root / "config.ini.legacy")
        self.url_config_file = str(root / "URL_config.ini")
        self.default_path = str(root / "downloads")
        self.recording_enabled = True  # main.py 的模块默认值
        self.engine_kwargs: list[dict[str, Any]] = []

    def main(self, **kwargs: Any) -> None:
        self.engine_kwargs.append(kwargs)

    def initialize_process(self) -> None:
        return None

    def cleanup_all_ffmpeg_processes(self) -> None:
        return None

    def _shutdown_postprocess_executor(self) -> None:
        return None


class _FakeServer:
    # uvicorn.Server 替身：serve() 是协程且立即返回，于是 web.run() 走完整个启动接线
    # 后进 finally 收尾，不需要哨兵异常，也不会真起 HTTP 服务。
    def __init__(self, config: Any) -> None:
        self.config = config
        self.should_exit = False

    async def serve(self) -> None:
        return None


def _write_web_config(tmp_path: Path) -> Path:
    # 回环绑定 + 未启用认证是正常配置，不会触发「拒绝启动」闸门；
    # web_show_console=true 跳过控制台隐藏。
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        '[Web]\nweb_host = "127.0.0.1"\nweb_port = 8000\nweb_auth_enable = false\nweb_show_console = true\n',
        encoding="utf-8",
    )
    return cfg


def _run_web_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_value: str | None) -> _FakeEngineModule:
    # 返回 fake 引擎模块：断言读它的 recording_enabled，即 web.py 传给录制主循环的初值。
    cfg = _write_web_config(tmp_path)
    engine = _FakeEngineModule(tmp_path, cfg)

    monkeypatch.setitem(sys.modules, "src.recording.engine", engine)
    import src

    monkeypatch.setattr(src.recording, "engine", engine, raising=False)
    monkeypatch.setattr(src.recording, "state", engine)
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(Server=_FakeServer, Config=lambda *a, **kw: None))
    monkeypatch.setitem(sys.modules, "src.api.routes", types.SimpleNamespace(create_app=lambda **kwargs: None))
    # 退出链的 HTTP 连接池收尾：替身避免测试里创建真实 httpx 客户端。
    monkeypatch.setitem(sys.modules, "src.network.http", types.SimpleNamespace(close_clients=lambda: None))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    if env_value is None:
        monkeypatch.delenv(_ENV_KEY, raising=False)
    else:
        monkeypatch.setenv(_ENV_KEY, env_value)

    try:
        web.run()
    finally:
        recorder_thread = getattr(engine, "_recorder_thread", None)
        if recorder_thread is not None:
            recorder_thread.join(timeout=2)
            assert not recorder_thread.is_alive()
    return engine


def test_without_env_var_panel_starts_in_stopped_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 上游语义不可被本地补丁顺手改掉：没设变量就是「面板默认停止态，等人点开始录制」。
    engine = _run_web_main(monkeypatch, tmp_path, env_value=None)
    assert engine.recording_enabled is False


@pytest.mark.parametrize("env_value", ["1", "true", "TRUE", "yes", " yes "])
def test_env_var_enables_auto_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_value: str) -> None:
    # 容器部署注入 =1；大小写与前后空白不敏感（与 DOUYIN_WEB_ALLOW_INSECURE 同口径）。
    engine = _run_web_main(monkeypatch, tmp_path, env_value=env_value)
    assert engine.recording_enabled is True


@pytest.mark.parametrize("env_value", ["0", "false", "no", ""])
def test_falsy_env_var_keeps_panel_stopped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_value: str) -> None:
    # 反向锁：写死 "0"/"false" 也必须回落停止态，不能按「非空即真」判定。
    engine = _run_web_main(monkeypatch, tmp_path, env_value=env_value)
    assert engine.recording_enabled is False


@pytest.mark.parametrize("serve_fails", [False, True])
def test_web_serves_on_main_thread_and_always_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serve_fails: bool
) -> None:
    events: list[str] = []

    async def serve(server: _FakeServer) -> None:
        assert threading.current_thread() is threading.main_thread()
        events.append("serve")
        if serve_fails:
            raise OSError("server startup failed")

    monkeypatch.setattr(_FakeServer, "serve", serve)
    monkeypatch.setattr(
        _FakeEngineModule, "_shutdown_postprocess_executor", lambda engine: events.append("postprocess")
    )
    monkeypatch.setattr(_FakeEngineModule, "cleanup_all_ffmpeg_processes", lambda engine: events.append("ffmpeg"))
    if serve_fails:
        with pytest.raises(OSError, match="server startup failed"):
            _run_web_main(monkeypatch, tmp_path, env_value=None)
    else:
        _run_web_main(monkeypatch, tmp_path, env_value=None)
    assert events == ["serve", "postprocess", "ffmpeg"]
