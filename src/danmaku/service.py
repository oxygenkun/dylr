"""弹幕平台注册表与采集器工厂。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.danmaku.plan import DanmakuPlan
    from src.danmaku.session import DanmakuCollector
    from src.platforms.danmaku.base import DanmakuBase


def get_danmaku_class(platform: str) -> type[DanmakuBase] | None:
    """按平台名返回弹幕协议实现；平台实现按需加载，避免导入本模块即拉起 protobuf 与 websocket。"""
    from src.platforms.danmaku.bilibili import BilibiliDanmaku
    from src.platforms.danmaku.douyin import DouyinDanmaku
    from src.platforms.danmaku.douyu import DouyuDanmaku
    from src.platforms.danmaku.huya import HuyaDanmaku
    from src.platforms.danmaku.twitch import TwitchDanmaku

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
    from src.danmaku.session import DanmakuCollector

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


class DanmakuService:
    """按 DanmakuPlan 创建采集会话的领域入口。"""

    def create_session(self, plan: DanmakuPlan) -> DanmakuCollector | None:
        return get_danmaku_collector(
            platform=plan.platform,
            danmaku_args=plan.args,
            base_filename=plan.base_filename,
            segment_seconds=plan.split_seconds,
            only_fans=plan.only_fans,
            room_name=plan.room_name,
            write_srt=plan.write_srt,
        )
