"""弹幕平台注册表与采集器工厂。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.danmaku.base import DanmakuBase
    from src.danmaku.collector import DanmakuCollector


def get_danmaku_class(platform: str) -> type[DanmakuBase] | None:
    """按平台名返回弹幕协议实现；平台实现按需加载，避免导入本模块即拉起 protobuf 与 websocket。"""
    from src.danmaku.platforms.bilibili import BilibiliDanmaku
    from src.danmaku.platforms.douyin import DouyinDanmaku
    from src.danmaku.platforms.douyu import DouyuDanmaku
    from src.danmaku.platforms.huya import HuyaDanmaku
    from src.danmaku.platforms.twitch import TwitchDanmaku

    _DANMAKU_REGISTRY = {
        "斗鱼直播": DouyuDanmaku,
        "B站直播": BilibiliDanmaku,
        "虎牙直播": HuyaDanmaku,
        "抖音直播": DouyinDanmaku,
        "TwitchTV": TwitchDanmaku,
    }
    return _DANMAKU_REGISTRY.get(platform)


def get_danmaku_collector(
    platform: str,
    danmaku_args: Any,
    base_filename: str,
    segment_seconds: float | None = 1800.0,
    only_fans: bool | None = None,
    room_name: str | None = None,
    write_srt: bool = True,
) -> DanmakuCollector | None:
    """工厂函数：平台不支持或 danmaku_args 为空时返回 None；采集器与平台实现一并按需加载。"""
    from src.danmaku.collector import DanmakuCollector

    cls = get_danmaku_class(platform)
    if cls is None:
        return None
    if not danmaku_args:
        return None
    return DanmakuCollector(
        danmaku_cls=cls,
        danmaku_args=danmaku_args,
        base_filename=base_filename,
        segment_seconds=segment_seconds,
        only_fans=only_fans,
        room_name=room_name,
        platform_name=platform,
        write_srt=write_srt,
    )
