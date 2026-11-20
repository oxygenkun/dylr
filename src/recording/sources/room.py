"""房间轮询线程入口：把 URL 配置项转成一条常驻检测线程。"""

from __future__ import annotations

import datetime
import random
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

from loguru import logger

from src.config import settings
from src.config.io import update_anchor_name
from src.danmaku.hub import get_hub
from src.notifications import msg_push
from src.notifications.notify import (
    clear_record_info,
    push_message,
    record_error,
    record_success,
    remove_room_from_running,
)
from src.recording import state as recording_state
from src.recording.anchor import rename_anchor_directory
from src.recording.pipeline.dispatch import _resolve_platform_stream
from src.recording.pipeline.selection import (
    MOBILE_UA,
    clean_name,
    get_quality_code,
    get_record_headers,
    get_record_user_agent,
    select_source_url,
)
from src.recording.plan import RecordPlan
from src.recording.scheduler import host_of
from src.recording.session import RecorderSession
from src.recording.signals import RoomObservation
from src.recording.sources.account import follow_monitor
from src.support import console, i18n, utils
from src.support.errors import get_error_line
from src.support.logger import set_room_context

type SourceSelector = Callable[..., str | None]


class RoomPoller:
    """配置中单个房间的检测线程；退出时负责清理运行列表与线程登记。"""

    def __init__(
        self,
        url_data: tuple[str, str, str],
        count_variable: int,
        *,
        room_state: dict[str, str] | None = None,
        select_source: SourceSelector | None = None,
        on_exit: Callable[[str], None] | None = None,
    ) -> None:
        self._url_data = url_data
        self._count_variable = count_variable
        self._room_state = room_state if room_state is not None else {"name": ""}
        self._select_source = select_source
        self._on_exit = on_exit
        self._thread_key = ""

    @property
    def url(self) -> str:
        return self._url_data[1]

    def start(self) -> bool:
        """启动轮询线程；线程创建失败时回滚运行列表并返回 False。"""
        self._thread_key = f"thread-{uuid.uuid4().hex[:12]}"
        thread = threading.Thread(target=self._run, name=self._thread_key, daemon=True)
        recording_state.create_var[self._thread_key] = thread
        try:
            thread.start()
        except RuntimeError as e:
            recording_state.create_var.pop(self._thread_key, None)
            remove_room_from_running(self.url)
            logger.warning(
                i18n.tr(
                    "录制线程启动失败，本轮跳过该房间（下轮会重试）: {url} {type_name}: {e}",
                    url=utils.mask_credentials(self.url),
                    type_name=type(e).__name__,
                    e=e,
                )
            )
            return False
        return True

    def _run(self) -> None:
        try:
            start_record(
                self._url_data,
                self._count_variable,
                room_state=self._room_state,
                select_source=self._select_source,
            )
        finally:
            remove_room_from_running(self.url)
            if self.url:
                prefix = self.url.split("?", 1)[0] + "?"
                recording_state.not_record_list[:] = [
                    u for u in recording_state.not_record_list if not u.startswith(prefix)
                ]
            if self._room_state.get("name"):
                try:
                    get_hub().room_stopped(self._room_state["name"], "房间已停止录制")
                except Exception:
                    pass
            with recording_state.record_state_lock:
                if recording_state.create_var.get(self._thread_key) is threading.current_thread():
                    recording_state.create_var.pop(self._thread_key, None)
            if self._on_exit is not None:
                self._on_exit(self.url)


def _interruptible_sleep(seconds: float, record_url: str | None = None) -> bool:
    """可中断等待：把一次性 time.sleep(n) 收敛为逐秒 tick，使退出/停止/注释能及时收敛。"""
    _remaining = int(seconds) if seconds > 0 else 0
    while _remaining > 0:
        if recording_state.exit_recording or not recording_state.recording_enabled:
            return True
        if record_url is not None and record_url in recording_state.url_comments:
            return True
        time.sleep(1)
        _remaining -= 1
    return False


def start_record(
    url_data: tuple[str, str, str],
    count_variable: int = -1,
    room_state: dict[str, str] | None = None,
    select_source: Callable[..., str | None] | None = None,
) -> None:
    """单个直播间的录制线程主体：循环「解析平台 → 选源 → ffmpeg/直下」，直到 URL 被注释、。"""
    set_room_context(f"序号{count_variable}")
    _select_source = select_source or select_source_url
    last_follow_revision = 0
    first_live_at: float | None = None
    while True:
        record_name = ""
        record_host = ""
        try:
            record_finished = False
            run_once = False
            start_pushed = False
            new_record_url = ""  # Shopee 平台专用：记录带 uid 的完整 URL 用于更新配置
            record_danmaku_args: dict[str, Any] | None = None  # 弹幕参数(平台相关)，传入 check_subprocess 启停弹幕
            count_time = time.time()
            record_quality_zh, record_url, anchor_name = url_data
            record_quality = get_quality_code(record_quality_zh)
            record_host = host_of(record_url)

            proxy_address = settings.proxy_addr
            platform = "未知平台"

            if settings.proxy_addr:
                proxy_address = None
                if settings.enable_proxy_platform_list:
                    for _proxy_platform in settings.enable_proxy_platform_list:
                        if _proxy_platform and _proxy_platform.strip() in record_url:
                            proxy_address = settings.proxy_addr
                            break

            if not proxy_address and settings.extra_enable_proxy_platform_list:
                for pt in settings.extra_enable_proxy_platform_list:
                    if pt and pt.strip() in record_url:
                        proxy_address = settings.proxy_addr_bak or None

            while True:
                record_danmaku_args = None
                if recording_state.exit_recording:
                    logger.debug(
                        i18n.tr(
                            "检测到退出标志，录制线程退出: {record_url}",
                            record_url=utils.mask_credentials(record_url),
                        )
                    )
                    return
                if not recording_state.recording_enabled:
                    logger.debug(
                        i18n.tr(
                            "录制已停止，房间线程退出: {record_url}",
                            record_url=utils.mask_credentials(record_url),
                        )
                    )
                    return
                if record_url in recording_state.url_comments:
                    print(
                        i18n.tr(
                            "[{record_url}]已被注释,本条线程将会退出",
                            record_url=utils.mask_credentials(record_url),
                        )
                    )
                    clear_record_info(record_name, record_url)
                    return
                if recording_state.scheduler is not None and not recording_state.scheduler.allow(record_host):
                    _backoff = recording_state.scheduler.backoff_seconds(record_host)
                    _backoff = min(_backoff, max(30.0, float(settings.delay_default)))
                    logger.debug(
                        i18n.tr(
                            "[{record_host}] 并发熔断中，跳过本轮探测，退避 {_backoff}s",
                            record_host=utils.mask_credentials(record_host),
                            _backoff=f"{_backoff:.0f}",
                        )
                    )
                    _interruptible_sleep(_backoff, record_url)
                    continue
                try:
                    follow_revision, follow_room = follow_monitor.get_room(record_url)
                    if follow_room:
                        last_follow_revision = follow_revision
                    _resolved = _resolve_platform_stream(record_url, proxy_address, record_quality)
                    if _resolved is None:
                        _interruptible_sleep(max(30.0, float(settings.delay_default)), record_url)
                        continue
                    platform, port_info, record_danmaku_args, new_record_url = _resolved

                    if anchor_name:
                        if "主播:" in anchor_name:
                            anchor_split: list[str] = anchor_name.split("主播:")
                            if len(anchor_split) > 1 and anchor_split[1].strip():
                                anchor_name = anchor_split[1].strip()
                            else:
                                anchor_name = cast(str, port_info.get("anchor_name", ""))
                    else:
                        anchor_name = cast(str, port_info.get("anchor_name", ""))

                    if not port_info.get("anchor_name", ""):
                        print(
                            i18n.tr(
                                "序号{count_variable} 网址内容获取失败,进行重试中...获取失败的地址是:{url_data}",
                                count_variable=count_variable,
                                url_data=utils.mask_credentials(str(url_data)),
                            )
                        )
                        record_error(record_host)
                        _interruptible_sleep(max(30.0, float(settings.delay_default)), record_url)
                        continue
                    else:
                        record_success(record_host)
                        anchor_name = clean_name(anchor_name)

                        latest_anchor_name = clean_name(cast(str, port_info.get("anchor_name", "")))
                        if (
                            settings.auto_update_anchor_name
                            and platform != "自定义录制直播"
                            and anchor_name
                            and latest_anchor_name
                            and latest_anchor_name != "空白昵称"
                            and anchor_name != latest_anchor_name
                        ):
                            template_enabled = bool(settings.record_directory_template.strip())
                            if (
                                template_enabled or rename_anchor_directory(anchor_name, latest_anchor_name, platform)
                            ) and update_anchor_name(record_url, latest_anchor_name):
                                logger.info(
                                    i18n.tr(
                                        "主播名已变更，配置已更新: {anchor_name} -> {latest_anchor_name}",
                                        anchor_name=anchor_name,
                                        latest_anchor_name=latest_anchor_name,
                                    )
                                    if template_enabled
                                    else i18n.tr(
                                        "主播名已变更，配置与录制文件同步更新: {anchor_name} -> {latest_anchor_name}",
                                        anchor_name=anchor_name,
                                        latest_anchor_name=latest_anchor_name,
                                    )
                                )
                                _stale_record_name = f"序号{count_variable} {anchor_name}"
                                anchor_name = latest_anchor_name
                                with recording_state.record_state_lock:
                                    recording_state.recording.discard(_stale_record_name)
                                    recording_state.recording_time_list.pop(_stale_record_name, None)
                                try:
                                    get_hub().room_stopped(_stale_record_name, "主播改名")
                                except Exception:
                                    pass
                        record_name = f"序号{count_variable} {anchor_name}"
                        set_room_context(record_name)

                        if record_url in recording_state.url_comments:
                            print(i18n.tr("[{anchor_name}]已被注释,本条线程将会退出", anchor_name=anchor_name))
                            clear_record_info(record_name, record_url)
                            return

                        if not url_data[-1] and not run_once:
                            if new_record_url:
                                recording_state.need_update_line_list.append(
                                    f"{record_url}|{new_record_url},主播: {anchor_name.strip()}"
                                )
                                recording_state.not_record_list.append(new_record_url)
                            else:
                                recording_state.need_update_line_list.append(
                                    f"{record_url}|{record_url},主播: {anchor_name.strip()}"
                                )
                            run_once = True

                        push_at = datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")
                        if not port_info.get("is_live", False):
                            first_live_at = None
                            if len(recording_state.recording) == 0:
                                print(i18n.tr("\r{record_name} 等待直播... ", record_name=record_name))

                            if start_pushed:
                                if settings.over_show_push:
                                    push_content = msg_push.render_push_content(
                                        "直播间状态更新：{ROOM_NAME} 直播已结束！时间：{TIME}", record_name, push_at
                                    )
                                    if settings.over_push_message_text:
                                        push_content = msg_push.render_push_template(
                                            settings.over_push_message_text, record_name, push_at
                                        )

                                    threading.Thread(
                                        target=push_message,
                                        args=(record_name, record_url, push_content.replace(r"\n", "\n")),
                                        daemon=True,
                                    ).start()
                                start_pushed = False

                        else:
                            if first_live_at is None:
                                first_live_at = time.time()
                            print(i18n.tr("\r{record_name} 正在直播中...", record_name=record_name))

                            if settings.live_status_push and not start_pushed:
                                if settings.begin_show_push:
                                    push_content = msg_push.render_push_content(
                                        "直播间状态更新：{ROOM_NAME} 正在直播中，时间：{TIME}", record_name, push_at
                                    )
                                    if settings.begin_push_message_text:
                                        push_content = msg_push.render_push_template(
                                            settings.begin_push_message_text, record_name, push_at
                                        )

                                    threading.Thread(
                                        target=push_message,
                                        args=(record_name, record_url, push_content.replace(r"\n", "\n")),
                                        daemon=True,
                                    ).start()
                                start_pushed = True

                            if settings.disable_record:
                                _sleep_remaining = settings.push_check_seconds
                                while _sleep_remaining > 0:
                                    if (
                                        not recording_state.recording_enabled
                                        or recording_state.exit_recording
                                        or record_url in recording_state.url_comments
                                    ):
                                        break
                                    time.sleep(1)
                                    _sleep_remaining -= 1
                                continue

                            platform_cookie = {
                                "抖音直播": settings.dy_cookie,
                                "TikTok直播": settings.tiktok_cookie,
                                "快手直播": settings.ks_cookie,
                                "虎牙直播": settings.hy_cookie,
                                "斗鱼直播": settings.douyu_cookie,
                                "YY直播": settings.yy_cookie,
                                "B站直播": settings.bili_cookie,
                                "小红书直播": settings.xhs_cookie,
                                "bigo": settings.bigo_cookie,
                                "blued": settings.blued_cookie,
                                "SOOP(原AfreecaTV)": settings.sooplive_cookie,
                                "网易CC直播": settings.netease_cookie,
                                "千度热播": settings.qiandurebo_cookie,
                                "PandaTV": settings.pandatv_cookie,
                                "猫耳FM直播": settings.maoerfm_cookie,
                                "WinkTV": settings.winktv_cookie,
                                "TTingLive(原Flextv)": settings.flextv_cookie,
                                "Look直播": settings.look_cookie,
                                "TwitCasting": settings.twitcasting_cookie,
                                "百度直播": settings.baidu_cookie,
                                "微博直播": settings.weibo_cookie,
                                "酷狗直播": settings.kugou_cookie,
                                "LiveMe": settings.liveme_cookie,
                                "shopee": settings.shopee_cookie,
                                "花椒直播": settings.huajiao_cookie,
                            }.get(platform, "")

                            real_url = _select_source(port_info, proxy_address, platform, cookies=platform_cookie)
                            if not real_url:
                                logger.warning(
                                    i18n.tr("{anchor_name} 本轮未获取到可用流地址，跳过录制", anchor_name=anchor_name)
                                )
                                _interruptible_sleep(max(random.randint(-5, 5) + settings.delay_default, 0), record_url)
                                continue
                            user_agent = get_record_user_agent(platform) or MOBILE_UA
                            headers = get_record_headers(platform, record_url, cookies=platform_cookie)
                            plan = RecordPlan(
                                observation=RoomObservation(
                                    room_key=record_url,
                                    url=record_url,
                                    quality=record_quality_zh,
                                    platform=platform,
                                    live=True,
                                    observed_at=time.monotonic(),
                                    anchor_name=anchor_name,
                                    title=cast(str, port_info.get("title", "")),
                                    port_info=port_info,
                                    danmaku_args=record_danmaku_args,
                                    first_live_at=first_live_at,
                                ),
                                source_url=real_url,
                                user_agent=user_agent,
                                headers=headers,
                                quality_code=record_quality,
                                record_name=record_name,
                                proxy_address=proxy_address,
                                cookies=platform_cookie,
                            )
                            result = RecorderSession().record_once(plan)
                            if result.retry:
                                _interruptible_sleep(max(random.randint(-5, 5) + settings.delay_default, 0), record_url)
                                continue
                            if result.comment_end:
                                return
                            if result.started:
                                record_finished = True
                            count_time = time.time()

                except Exception as e:
                    logger.error(
                        i18n.tr(
                            "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                            type_name=type(e).__name__,
                            e=e,
                            get_error_line=get_error_line(e),
                        )
                    )
                    record_error(record_host)

                num = random.randint(-5, 5) + settings.delay_default
                if num < 0:
                    num = 0
                x = num

                if sum(recording_state.error_window) >= 5:
                    x = x + 60
                    console.color_obj.print_colored("\r瞬时错误太多,延迟加60秒", console.color_obj.YELLOW)

                if record_finished:
                    count_time_end = time.time() - count_time
                    if count_time_end < 60:
                        x = 30
                    record_finished = False

                while x:
                    follow_revision, follow_room = follow_monitor.get_room(record_url)
                    if follow_room and follow_revision != last_follow_revision:
                        last_follow_revision = follow_revision
                        break
                    if (
                        record_url in recording_state.url_comments
                        or recording_state.exit_recording
                        or not recording_state.recording_enabled
                    ):
                        break
                    x = x - 1
                    if settings.loop_time:
                        print(i18n.tr("\r{anchor_name}循环等待{x}秒 ", anchor_name=anchor_name, x=x), end="")
                    time.sleep(1)
                if settings.loop_time:
                    print("\r检测直播间中...", end="")
        except Exception as e:
            logger.error(
                i18n.tr(
                    "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                    type_name=type(e).__name__,
                    e=e,
                    get_error_line=get_error_line(e),
                )
            )
            record_error(record_host)
            time.sleep(2)
        finally:
            if room_state is not None and record_name:
                room_state["name"] = record_name
