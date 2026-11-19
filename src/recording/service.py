"""录制服务：房间生命周期、选源入口与轮询线程协调。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from src.config import settings
from src.recording import state as recording_state
from src.recording.pipeline.selection import select_source_url
from src.recording.sources.room import RoomPoller, _interruptible_sleep
from src.support import i18n, utils


class RecordingService:
    """管理配置房间的轮询线程；选源统一从 service 进入。"""

    def __init__(
        self,
        *,
        select_source: Callable[..., str | None] = select_source_url,
    ) -> None:
        self._select_source = select_source
        self._pollers: dict[str, RoomPoller] = {}
        self._lock = threading.Lock()

    def select_source(
        self,
        port_info: dict[str, object],
        proxy_address: str | None,
        platform: str,
        *,
        cookies: str = "",
    ) -> str | None:
        return self._select_source(port_info, proxy_address, platform, cookies=cookies)

    def sync_rooms(self, rooms: Iterable[tuple[str, str, str]]) -> None:
        """按当前配置增补房间线程；已移除/注释的 URL 由线程自身收敛退出。"""
        for url_tuple in rooms:
            with recording_state.record_state_lock:
                recording_state.monitoring = len(recording_state.running_list)

            if url_tuple[1] in recording_state.not_record_list:
                continue

            if (
                url_tuple[1] not in recording_state.running_list
                and not recording_state.exit_recording
                and not recording_state.disk_limited
                and recording_state.recording_enabled
            ):
                print(
                    i18n.tr(
                        "\r{first_start}地址: {url_tuple}",
                        first_start="新增" if not recording_state.first_start else "传入",
                        url_tuple=utils.mask_credentials(url_tuple[1]),
                    )
                )
                with recording_state.record_state_lock:
                    recording_state.monitoring += 1
                    recording_state.running_list.append(url_tuple[1])
                    index = recording_state.monitoring

                poller = RoomPoller(
                    url_tuple,
                    index,
                    select_source=self.select_source,
                    on_exit=self._forget,
                )
                with self._lock:
                    self._pollers[url_tuple[1]] = poller
                if not poller.start():
                    with self._lock:
                        self._pollers.pop(url_tuple[1], None)
                    continue
                _interruptible_sleep(settings.local_delay_default)

        if recording_state.scheduler is not None:
            recording_state.scheduler.set_active_count(recording_state.monitoring)

    def _forget(self, url: str) -> None:
        with self._lock:
            self._pollers.pop(url, None)

    def poller_count(self) -> int:
        with self._lock:
            return len(self._pollers)
