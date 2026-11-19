"""录制运行态及其同步锁。"""

import datetime
import threading
from collections import deque
from typing import TYPE_CHECKING

from src.recording.scheduler import ResizableSemaphore

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Protocol

    from src.recording.scheduler import ConcurrencyScheduler

    class _RecordingService(Protocol):
        def sync_rooms(self, rooms: Iterable[tuple[str, str, str]]) -> None: ...


record_state_lock: threading.Lock = threading.Lock()
max_request_lock: threading.Lock = threading.Lock()

recording: set[str] = set()  # 正在录制的直播间集合
monitoring: int = 0  # 正在监控的直播间数量
running_list: list[str] = []  # 正在运行的 URL 列表
recording_time_list: dict[str, list[datetime.datetime | str]] = {}  # [开始时间, 设置画质, 实际画质]
exit_recording: bool = False  # 退出标志
disk_limited: bool = False
recording_enabled: bool = True
error_count: int = 0  # 累计错误计数（自进程启动起单调递增，不做周期清零；窗口口径见 error_window）
error_window_size: int = 10  # 错误窗口大小
error_window: deque[int] = deque(maxlen=error_window_size)  # 错误窗口（deque maxlen 自动裁剪，避免无界增长）
url_tuples_list: list[tuple[str, str, str]] = []  # 解析后的 URL 配置项（画质, URL, 主播名）
url_comments: set[str] = set()
text_no_repeat_url: list[tuple[str, str, str]] = []  # 保序去重后的 URL 配置项
need_update_line_list: list[str] = []  # 需要更新的配置行
not_record_list: list[str] = []  # 本轮需跳过的规范 URL（Shopee uid 回写后）
first_start: bool = True  # 首次启动标志
first_run: bool = True  # 首次运行标志
global_proxy: bool = False  # 全局代理启用标志
create_var: dict[str, threading.Thread] = {}  # 动态创建的线程登记表（字幕线程 / 房间线程）
start_display_time: datetime.datetime = datetime.datetime.now()  # 显示信息开始时间
process_start_time: datetime.datetime = datetime.datetime.now()
scheduler: ConcurrencyScheduler | None = None
recording_service: _RecordingService | None = None
semaphore: ResizableSemaphore = ResizableSemaphore(1)
recording_semaphore: ResizableSemaphore = ResizableSemaphore(1024)
_recorder_thread: threading.Thread | None = None
