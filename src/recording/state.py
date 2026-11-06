"""录制配置与进程内共享状态；导入不启动引擎、注册信号或检查外部程序。"""

import datetime
import os
import re
import sys
import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from src.config.reader import TEXT_ENCODING
from src.recording.scheduler import ResizableSemaphore
from src.support.console import Color

if TYPE_CHECKING:
    from src.recording.scheduler import ConcurrencyScheduler


def _read_version_from_pyproject() -> str:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject_path.exists():
        text = pyproject_path.read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*["\'](.+?)["\']', text, re.MULTILINE)
        if m:
            return f"v{m.group(1)}"
    return "v0.0.0"  # 最终回退


version: str = _read_version_from_pyproject()


recording: set[str] = set()  # 正在录制的直播间集合
monitoring: int = 0  # 正在监控的直播间数量
running_list: list[str] = []  # 正在运行的 URL 列表
recording_time_list: dict[str, list[datetime.datetime | str]] = {}  # [开始时间, 设置画质, 实际画质]
exit_recording: bool = False  # 退出标志
disk_limited: bool = False
recording_enabled: bool = True

error_count: int = 0  # 累计错误计数（自进程启动起单调递增，不做周期清零；窗口口径见 error_window）
max_request: int = 3  # 同一时间访问网络的线程数（由 recorder.main() 读取配置后覆盖）
max_request_lock: threading.Lock = threading.Lock()  # 最大请求数的线程锁
error_window_size: int = 10  # 错误窗口大小
error_window: deque[int] = deque(maxlen=error_window_size)  # 错误窗口（deque maxlen 自动裁剪，避免无界增长）

url_tuples_list: list[tuple[str, str, str]] = []  # 解析后的 URL 配置项（画质, URL, 主播名）
url_comments: set[str] = set()
text_no_repeat_url: list[tuple[str, str, str]] = []  # 保序去重后的 URL 配置项
need_update_line_list: list[str] = []  # 需要更新的配置行
not_record_list: list[str] = []  # 本轮需跳过的规范 URL（Shopee uid 回写后）
ini_URL_content: str = ""  # URL 配置文件初始内容（用于 update_file 异常恢复）

first_start: bool = True  # 首次启动标志
first_run: bool = True  # 首次运行标志
global_proxy: bool = False  # 全局代理启用标志
use_proxy: bool = False  # 是否使用代理 IP（由 recorder.main() 读取配置后覆盖）
create_var: dict[str, threading.Thread] = {}  # 动态创建的线程登记表（字幕线程 / 房间线程）
start_display_time: datetime.datetime = datetime.datetime.now()  # 显示信息开始时间
process_start_time: datetime.datetime = datetime.datetime.now()

delay_default: int = 120  # 循环监测间隔时间（秒）
video_record_quality: str = "原画"  # 录制视频画质
video_save_type: str = "ts"  # 录制视频格式
split_video_by_time: bool = False  # 是否开启分段录制
split_time: str = "1800"  # 视频分段时间（秒）
create_time_file: bool = False  # 是否生成时间字幕文件
auto_update_anchor_name: bool = True  # 主播名变更时自动同步配置与录制文件（由 recorder.main() 读取配置后覆盖）
hls_collection_enabled: bool = True  # 是否优先使用 HLS(m3u8) 源采集；关闭时回退 FLV
hls_collection_exclude_platforms: list[str] = []
enable_danmaku: bool = False  # 是否录制弹幕
enable_danmaku_monitor: bool = False  # 是否弹幕监控（与弹幕录制解耦：仅监控不落 SRT）
danmaku_split_time: float = 1800.0  # 弹幕分片时长(秒)
danmaku_platforms: list[str] = []  # 弹幕录制平台列表


def _app_root() -> str:
    """应用根目录：源码运行时是项目根目录；冻结运行时是 exe 同级目录——。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(sys.executable))
    return str(Path(__file__).resolve().parents[2])


script_path: str = _app_root()  # 脚本所在目录（冻结后为 exe 同级目录）
config_file: str = f"{script_path}/config/config.toml"  # 主配置文件路径（TOML）
legacy_config_file: str = f"{script_path}/config/config.ini"  # 旧 INI；仅用于首启自动迁移
url_config_file: str = f"{script_path}/config/URL_config.ini"  # URL 配置文件路径
backup_dir: str = f"{script_path}/backup_config"  # 配置备份目录
text_encoding: str = TEXT_ENCODING  # 文本文件编码（兼容 BOM）
rstr: str = r"[\/\\\:\*\？?\"\<\>\|&#.。,， ~！·% ]"  # 文件名字符过滤正则
default_path: str = f"{script_path}/downloads"  # 默认下载目录
file_update_lock: threading.RLock = threading.RLock()  # 配置文件读写锁（防多线程写入冲突）

record_state_lock: threading.Lock = threading.Lock()


proxy_addr: str | None = None
proxy_addr_bak: str = ""
enable_proxy_platform: str = ""
enable_proxy_platform_list: list[str] | None = None
extra_enable_proxy: str = ""
extra_enable_proxy_platform_list: list[str] | None = None
scheduler: ConcurrencyScheduler | None = None
semaphore: ResizableSemaphore = ResizableSemaphore(1)
recording_semaphore: ResizableSemaphore = ResizableSemaphore(1024)
local_delay_default: int = 0
loop_time: bool = False
show_url: bool = False
enable_https_recording: bool = False
disk_space_limit: float = 1.0

douyin_rate_lock: threading.Lock = threading.Lock()
douyin_last_request_time: float = 0.0
douyin_min_interval: float = 3.0  # 两次抖音请求的最小间隔（秒）

video_save_path: str = ""
max_record_seconds: int = 6 * 60 * 60
clean_emoji: bool = True
filename_by_title: bool = False
folder_by_author: bool = False
folder_by_time: bool = False
folder_by_title: bool = False
converts_to_h264: bool = False
converts_to_mp4: bool = False
delete_origin_file: bool = False
is_run_script: bool = False
custom_script: str | None = None
video_save_type_list: tuple[str, ...] = ("FLV", "MKV", "TS", "MP4", "MP3音频", "M4A音频", "MP3", "M4A")

live_status_push: str = ""
push_message_title: str = ""
begin_show_push: bool = True
begin_push_message_text: str = ""
over_show_push: bool = False
over_push_message_text: str = ""
disable_record: bool = False
push_check_seconds: int = 1800

dingtalk_api_url: str = ""
dingtalk_phone_num: str = ""
dingtalk_is_atall: bool = False
xizhi_api_url: str = ""
bark_msg_api: str = ""
bark_msg_level: str = "active"
bark_level_overrides: dict[str, str] = {}
bark_msg_ring: str = "bell"

email_host: str = ""
email_password: str = ""
login_email: str = ""
sender_email: str = ""
sender_name: str = ""
to_email: str = ""
smtp_port: str = ""
open_smtp_ssl: bool = True

tg_chat_id: str = ""
tg_token: str = ""
ntfy_api: str = ""
ntfy_tags: str = "tada"
ntfy_email: str = ""
pushplus_token: str = ""

sooplive_username: str = ""
sooplive_password: str = ""
flextv_username: str = ""
flextv_password: str = ""
popkontv_username: str = ""
popkontv_partner_code: str = "P-00001"
popkontv_password: str = ""
popkontv_access_token: str = ""
twitcasting_account_type: str = "normal"
twitcasting_username: str = ""
twitcasting_password: str = ""

dy_cookie: str = ""
ks_cookie: str = ""
tiktok_cookie: str = ""
hy_cookie: str = ""
douyu_cookie: str = ""
yy_cookie: str = ""
bili_cookie: str = ""
xhs_cookie: str = ""
bigo_cookie: str = ""
blued_cookie: str = ""
sooplive_cookie: str = ""
netease_cookie: str = ""
qiandurebo_cookie: str = ""
pandatv_cookie: str = ""
maoerfm_cookie: str = ""
winktv_cookie: str = ""
flextv_cookie: str = ""
look_cookie: str = ""
liveme_cookie: str = ""
huajiao_cookie: str = ""
liuxing_cookie: str = ""
showroom_cookie: str = ""
acfun_cookie: str = ""
changliao_cookie: str = ""
yinbo_cookie: str = ""
yingke_cookie: str = ""
zhihu_cookie: str = ""
chzzk_cookie: str = ""
haixiu_cookie: str = ""
vvxqiu_cookie: str = ""
yiqilive_cookie: str = ""
langlive_cookie: str = ""
pplive_cookie: str = ""
six_room_cookie: str = ""
lehaitv_cookie: str = ""
huamao_cookie: str = ""
shopee_cookie: str = ""
youtube_cookie: str = ""
taobao_cookie: str = ""
jd_cookie: str = ""
faceit_cookie: str = ""
migu_cookie: str = ""
lianjie_cookie: str = ""
laixiu_cookie: str = ""
picarto_cookie: str = ""
baidu_cookie: str = ""
weibo_cookie: str = ""
kugou_cookie: str = ""
twitch_cookie: str = ""
twitcasting_cookie: str = ""


_recorder_thread: threading.Thread | None = None
os_type: str = os.name
color_obj = Color()
