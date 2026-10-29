"""账号级关注与私信监控实例，由录制引擎显式启动。"""

from src.app.graceful_shutdown import shutdown_manager
from src.config.io import append_room_line
from src.monitors.douyin_dm import DouyinDmMonitor
from src.monitors.douyin_follow import DouyinFollowMonitor
from src.recording import state

follow_monitor = DouyinFollowMonitor(shutdown_manager.stopping)


def _add_room_from_dm(url: str, quality: str, name: str) -> str:
    """私信分享自动录制：add 回调把解析出的直播间写进 URL_config.ini，主循环下一轮自动拉起。"""
    return append_room_line(state.url_config_file, url, quality, name)


douyin_dm_monitor = DouyinDmMonitor(shutdown_manager.stopping, add_room=_add_room_from_dm)
