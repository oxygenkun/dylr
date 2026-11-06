"""录制引擎的配置热加载、房间调度与显式进程初始化。"""

import builtins
import threading
import types
from collections import deque
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.config.store import ConfigStore
from src.network import policy
from src.recording import engine, state


class CycleEnded(BaseException):
    """在一轮完成后的 sleep 结束无限值守循环。"""


@pytest.fixture(autouse=True)
def isolated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in vars(state).copy().items():
        if not name.startswith("__"):
            if isinstance(value, (dict, list, set, deque)):
                value = value.copy()
            monkeypatch.setattr(state, name, value)
    state.script_path = str(tmp_path)
    state.config_file = str(tmp_path / "config.toml")
    state.url_config_file = str(tmp_path / "URL_config.ini")
    state.default_path = str(tmp_path / "downloads")
    state.backup_dir = str(tmp_path / "backup_config")
    state.first_run = False
    state.first_start = True
    state.recording_enabled = False
    state.exit_recording = False
    state.disk_limited = False
    state.scheduler = None
    state.running_list = []
    state.recording = set()
    state.create_var = {}
    state.need_update_line_list = []
    state.not_record_list = []
    Path(state.config_file).write_text('["录制设置"]\nlanguage = "zh_CN"\n', encoding="utf-8")
    Path(state.url_config_file).write_text("https://live.douyin.com/123\n", encoding="utf-8-sig")
    monkeypatch.setattr(engine, "config", ConfigStore(state.config_file))
    engine.config.read()
    monkeypatch.setattr(engine, "language", "zh_CN")
    monkeypatch.setattr(engine, "_last_ssl_disable_platforms_raw", "")
    monkeypatch.setattr(engine, "follow_monitor", Mock())
    monkeypatch.setattr(engine, "douyin_dm_monitor", Mock())
    monkeypatch.setattr(engine, "check_ffmpeg_existence", Mock(return_value=True))
    monkeypatch.setattr(engine, "purge_stale_bytecode_caches", Mock())
    monkeypatch.setattr(engine, "_shutdown_postprocess_executor", Mock())
    monkeypatch.setattr(engine, "_interruptible_sleep", Mock(return_value=False))
    monkeypatch.setattr(engine, "_i18n_set_language", Mock())
    monkeypatch.setattr(policy, "verify_stream_tls", policy.verify_stream_tls)
    monkeypatch.setattr(policy, "stream_tls_overrides", {})
    helpers = types.SimpleNamespace(**vars(engine.utils))
    helpers.check_disk_capacity = Mock(return_value=100)
    monkeypatch.setattr(engine, "utils", helpers)

    def end_cycle(seconds: float) -> None:
        raise CycleEnded

    clock = types.SimpleNamespace(**vars(engine.time))
    clock.sleep = end_cycle
    monkeypatch.setattr(engine, "time", clock)


def test_main_applies_configuration_and_room_entries() -> None:
    Path(state.config_file).write_text(
        '["录制设置"]\n'
        '"同一时间访问网络的线程数" = 7\n'
        '"最大同时录制数(0为不限制)" = 2\n'
        '"循环时间(秒)" = 1\n'
        '"视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频" = "mp4"\n'
        '"视频分段时间(秒)" = -1\n'
        '"单次录制时长上限(秒,0为不限制)" = -1\n'
        '"HLS采集排除平台(逗号分隔)" = "虎牙直播，B站直播"\n'
        '"是否使用代理ip(是/否)" = false\n',
        encoding="utf-8",
    )
    active = "https://live.douyin.com/123"
    commented = "https://www.huya.com/456"
    Path(state.url_config_file).write_text(
        f"{active}\n#{commented}\n高清，https://live.bilibili.com/789，主播: 测试\nhttps://unknown.example/123\n",
        encoding="utf-8-sig",
    )
    engine.config.read()
    with pytest.raises(CycleEnded):
        engine.main(non_interactive=True)
    assert state.video_save_type == "MP4"
    assert state.split_time == "1800" and state.max_record_seconds == 0
    assert state.delay_default == 30 and state.proxy_addr is None
    assert state.hls_collection_exclude_platforms == ["虎牙直播", "B站直播"]
    assert state.scheduler is not None
    assert state.scheduler.network_semaphore.capacity == 7
    assert state.scheduler.recording_limit == 2
    assert state.text_no_repeat_url == [
        ("原画", active, ""),
        ("高清", "https://live.bilibili.com/789", "主播: 测试"),
    ]
    assert commented in state.url_comments
    assert "#https://unknown.example/123" in Path(state.url_config_file).read_text(encoding="utf-8-sig")
    engine.follow_monitor.start.assert_called_once()
    engine.douyin_dm_monitor.start.assert_called_once()


def test_main_hot_reload_replaces_previous_values(monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = Path(state.config_file)
    config_file.write_text('["录制设置"]\n"同一时间访问网络的线程数" = 5\n', encoding="utf-8")
    engine.config.read()
    capacities: list[int] = []

    def next_cycle(seconds: float) -> None:
        assert state.scheduler is not None
        capacities.append(state.scheduler.network_semaphore.capacity)
        if len(capacities) == 1:
            config_file.write_text('["录制设置"]\n"同一时间访问网络的线程数" = 9\n', encoding="utf-8")
        else:
            raise CycleEnded

    monkeypatch.setattr(engine.time, "sleep", next_cycle)
    with pytest.raises(CycleEnded):
        engine.main(non_interactive=True)
    assert capacities == [5, 9]
    assert state.max_request == 9


def test_web_engine_returns_when_disk_limit_is_reached() -> None:
    engine.utils.check_disk_capacity.return_value = 0
    engine.main(non_interactive=True)
    assert state.disk_limited and state.exit_recording
    assert not state.create_var and not state.running_list
    engine._shutdown_postprocess_executor.assert_called_once()


@pytest.mark.parametrize("start_failure", [False, True])
def test_room_threads_release_registration(monkeypatch: pytest.MonkeyPatch, start_failure: bool) -> None:
    threads: list[threading.Thread] = []
    calls: list[tuple[tuple[str, str, str], int]] = []

    class RoomThread(threading.Thread):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            threads.append(self)

        def start(self) -> None:
            if start_failure:
                raise RuntimeError("thread unavailable")
            super().start()

    def record(url_tuple: tuple[str, str, str], number: int, *, room_state: dict[str, str]) -> None:
        calls.append((url_tuple, number))
        room_state["name"] = "测试房间"

    threading_shim = types.SimpleNamespace(**vars(threading))
    threading_shim.Thread = RoomThread
    monkeypatch.setattr(engine, "threading", threading_shim)
    monkeypatch.setattr(engine, "start_record", record)
    state.recording_enabled = True
    try:
        with pytest.raises(CycleEnded):
            engine.main(non_interactive=True)
    finally:
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
            assert not thread.is_alive()
    assert len(threads) == 1
    assert calls == ([] if start_failure else [(("原画", "https://live.douyin.com/123", ""), 1)])
    assert not state.running_list and not state.create_var


@pytest.mark.parametrize("cli", [False, True])
def test_process_initialization_registers_hooks_without_running_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: bool
) -> None:
    signal_shim = types.SimpleNamespace(**vars(engine.signal))
    signal_shim.signal = Mock()
    exit_shim = types.SimpleNamespace(**vars(engine.atexit))
    exit_shim.register = Mock()
    threading_shim = types.SimpleNamespace(**vars(threading))
    threading_shim.Thread = Mock()
    builtin_shim = types.SimpleNamespace(**vars(builtins))
    monkeypatch.setattr(engine, "signal", signal_shim)
    monkeypatch.setattr(engine, "atexit", exit_shim)
    monkeypatch.setattr(engine, "threading", threading_shim)
    monkeypatch.setattr(engine, "builtins", builtin_shim)
    monkeypatch.setattr(engine, "initialize_runtime", Mock())
    monkeypatch.setattr(engine, "detect_system_proxy", Mock(return_value=None))
    monkeypatch.setattr(engine, "ffmpeg_path", str(tmp_path / "ffmpeg"))
    monkeypatch.setattr(engine, "should_prepend_bundled_ffmpeg_dir", Mock(return_value=True))
    monkeypatch.setenv("PATH", "/usr/bin")
    engine.initialize_process(cli=cli)
    engine.initialize_runtime.assert_called_once()
    assert signal_shim.signal.call_count == (2 + hasattr(signal_shim, "SIGBREAK") if cli else 0)
    assert exit_shim.register.call_count == 4
    assert builtin_shim.print is engine.translated_print
    threading_shim.Thread.assert_called_once_with(target=engine.backup_file_start, daemon=True)
    threading_shim.Thread.return_value.start.assert_called_once()
