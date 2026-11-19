"""录制信号契约：房间轮询、关注与私信统一交给录制服务消费。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

type SignalSource = Literal["room", "follow", "dm"]


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """URL_config.ini 中的一条房间配置。"""

    url: str
    quality: str
    anchor_name: str = ""


@dataclass(frozen=True, slots=True)
class LiveHint:
    """关注源的轻量信号：只提示可能开播，由房间轮询源重新解析。"""

    room_key: str
    live: bool
    revision: int
    observed_at: float
    source: SignalSource = "follow"
    title: str = ""
    anchor_name: str = ""


@dataclass(frozen=True, slots=True)
class RoomAdded:
    """私信源发现的新房间；是否持久化由调用方决定。"""

    url: str
    quality: str
    anchor_name: str
    source: SignalSource = "dm"
    persist: bool = True


@dataclass(frozen=True, slots=True)
class RoomObservation:
    """房间轮询源的一次解析结果，携带 manager 选源所需的流数据。"""

    room_key: str
    url: str
    quality: str
    platform: str
    live: bool
    observed_at: float
    source: SignalSource = "room"
    anchor_name: str = ""
    title: str = ""
    port_info: Mapping[str, object] | None = None
    danmaku_args: Mapping[str, object] | None = None
    new_url: str = ""


type Signal = LiveHint | RoomAdded | RoomObservation
