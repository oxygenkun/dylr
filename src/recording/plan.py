"""录制计划：录制服务完成选源后交给单次录制会话执行。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.danmaku.plan import DanmakuPlan
from src.recording.signals import RoomObservation


@dataclass(frozen=True, slots=True)
class RecordPlan:
    """一次录制尝试的不可变输入；不包含路径与 ffmpeg 命令。"""

    observation: RoomObservation
    source_url: str
    user_agent: str
    headers: Mapping[str, str] | None
    quality_code: str
    record_name: str = ""
    proxy_address: str | None = None
    cookies: str = ""
    danmaku: DanmakuPlan | None = None


@dataclass(frozen=True, slots=True)
class RecordResult:
    """一次录制尝试的结果，供轮询线程决定下一轮动作。"""

    started: bool = False
    comment_end: bool = False
    retry: bool = False
