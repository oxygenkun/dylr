from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

JS_SCRIPT_PATH = Path(__file__).resolve().parent / "javascript"


def initialize_runtime() -> None:
    from src.runtime.node import check_node
    from src.support.logger import script_path

    node_dir = Path(script_path) / "node"
    env_path = os.environ.get("PATH", "")
    if node_dir.is_dir() and str(node_dir) not in env_path.split(os.pathsep):
        os.environ["PATH"] = str(node_dir) + os.pathsep + env_path
    if os.environ.get("DOUYIN_SKIP_RUNTIME_CHECK", "").strip().lower() not in ("1", "true", "yes"):
        check_node()


if TYPE_CHECKING:
    from src.danmaku.base import DanmakuBase
    from src.danmaku.collector import DanmakuCollector


def get_danmaku_class(platform: str) -> Optional[type[DanmakuBase]]:
    """按平台名返回弹幕协议实现。"""
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
    segment_seconds: Optional[float] = 1800.0,
    only_fans: Optional[bool] = None,
    room_name: Optional[str] = None,
    write_srt: bool = True,
) -> Optional["DanmakuCollector"]:
    """工厂函数：平台不支持或 danmaku_args 为空时返回 None（导入放在函数内，避免循环依赖）。"""
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
