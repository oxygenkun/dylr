"""弹幕采集计划：录制服务按房间与平台配置构建，弹幕侧只消费。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

type DanmakuArgs = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DanmakuPlan:
    """一次录制会话附带的弹幕采集参数。"""

    write_srt: bool
    monitor_only: bool
    split_seconds: float | None
    room_name: str
    base_filename: str
    args: DanmakuArgs
    platform: str = ""
    only_fans: bool | None = None
