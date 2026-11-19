from __future__ import annotations

from typing import Any

import pytest

from src.config import settings
from src.recording import service as service_module
from src.recording import state as recording_state


def test_select_source_forwards_cookies_and_arguments() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def select(*args: Any, **kwargs: Any) -> str:
        calls.append((args, kwargs))
        return "https://example.com/live.flv"

    service = service_module.RecordingService(select_source=select)

    assert service.select_source({"flv_url": "x"}, "proxy", "抖音直播", cookies="c=1") == "https://example.com/live.flv"
    assert calls == [(({"flv_url": "x"}, "proxy", "抖音直播"), {"cookies": "c=1"})]


@pytest.fixture
def clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recording_state, "running_list", [])
    monkeypatch.setattr(recording_state, "monitoring", 0)
    monkeypatch.setattr(recording_state, "not_record_list", [])
    monkeypatch.setattr(recording_state, "exit_recording", False)
    monkeypatch.setattr(recording_state, "disk_limited", False)
    monkeypatch.setattr(recording_state, "recording_enabled", True)
    monkeypatch.setattr(recording_state, "scheduler", None)
    monkeypatch.setattr(recording_state, "first_start", False)
    monkeypatch.setattr(settings, "local_delay_default", 0)
    monkeypatch.setattr(service_module, "_interruptible_sleep", lambda _seconds: None)


def test_sync_rooms_starts_once_and_skips_not_record(clean_state: None, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[tuple[str, str, str], int]] = []

    class FakePoller:
        def __init__(
            self,
            url_data: tuple[str, str, str],
            index: int,
            *,
            select_source: Any,
            on_exit: Any,
        ) -> None:
            created.append((url_data, index))
            self.url = url_data[1]
            self._on_exit = on_exit

        def start(self) -> bool:
            return True

    monkeypatch.setattr(service_module, "RoomPoller", FakePoller)
    service = service_module.RecordingService(select_source=lambda *_args, **_kwargs: None)
    url = ("原画", "https://live.douyin.com/1", "")

    service.sync_rooms([url])
    service.sync_rooms([url])

    assert created == [(url, 1)]
    assert recording_state.running_list == [url[1]]
    assert recording_state.monitoring == 1
    assert service.poller_count() == 1

    recording_state.not_record_list.append("https://live.douyin.com/2")
    service.sync_rooms([("原画", "https://live.douyin.com/2", "")])
    assert recording_state.running_list == [url[1]]


def test_poller_exit_forgets_room(clean_state: None, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePoller:
        def __init__(
            self,
            url_data: tuple[str, str, str],
            index: int,
            *,
            select_source: Any,
            on_exit: Any,
        ) -> None:
            self._on_exit = on_exit

        def start(self) -> bool:
            return True

    monkeypatch.setattr(service_module, "RoomPoller", FakePoller)
    service = service_module.RecordingService(select_source=lambda *_args, **_kwargs: None)
    service.sync_rooms([("原画", "https://live.douyin.com/1", "")])

    service._forget("https://live.douyin.com/1")

    assert service.poller_count() == 0
