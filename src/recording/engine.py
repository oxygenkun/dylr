"""录制引擎：进程启动、配置热加载与房间线程调度。"""

import atexit
import builtins
import os
import re
import signal
import subprocess
import sys
import threading
import time
import types
import uuid

from loguru import logger

from src import initialize_runtime
from src.app.graceful_shutdown import shutdown_manager
from src.app.log_archive import archive_runtime_logs
from src.app.startup_cleanup import purge_stale_bytecode_caches
from src.config import files as config_files
from src.config.io import (
    _safe_float,
    _safe_int,
    backup_file_start,
    delete_line,
    read_config_bool,
    read_config_value,
    update_file,
)
from src.config.store import ConfigError, ConfigStore, ensure_config_migrated
from src.monitors.registry import douyin_dm_monitor, follow_monitor
from src.network import policy
from src.network.http import close_clients
from src.network.proxy import detect_system_proxy
from src.notifications.notify import adjust_max_request, remove_room_from_running
from src.recording import state
from src.recording.process import _get_error_line, cleanup_all_ffmpeg_processes
from src.recording.scheduler import ConcurrencyScheduler
from src.recording.session import (
    _MAX_RECORD_SECONDS,
    _interruptible_sleep,
    _shutdown_postprocess_executor,
    start_record,
)
from src.recording.status import display_info
from src.runtime.ffmpeg import check_ffmpeg, ffmpeg_path, should_prepend_bundled_ffmpeg_dir
from src.streams import spider
from src.streams.dispatch import CLEAN_URL_HOST_LIST, PLATFORM_HOST, _stream_path_suffix
from src.streams.selection import contains_url
from src.support import i18n, utils
from src.support.i18n import resolve_language as _i18n_resolve
from src.support.i18n import set_language as _i18n_set_language
from src.support.i18n import translated_print
from src.support.logger import set_file_logging

platforms: str = (
    "\n国内站点：抖音|快手|虎牙|斗鱼|YY|B站|小红书|bigo|blued|网易CC|千度热播|猫耳FM|Look直播|TwitCasting|百度|微博|"
    "酷狗|花椒|流星|Acfun|畅聊|映客|音播|知乎|嗨秀|VV星球|17Live|浪Live|飘飘|六间房|乐嗨|花猫|淘宝|京东|咪咕|连接|来秀"
    "\n海外站点：TikTok|SOOP(原AfreecaTV)|PandaTV|WinkTV|TTingLive(原Flextv)|PopkonTV|TwitchTV|LiveMe|ShowRoom|CHZZK|Shopee|"
    "YouTube|Faceit|Picarto"
)


def safe_exit(_signum: int, _frame: types.FrameType | None) -> None:
    """信号处理器：置退出标志 → 排空后处理池 → 清理 ffmpeg 与 HTTP 连接池 → 退出进程。"""
    if shutdown_manager.stopping.is_set():
        return
    state.exit_recording = True
    shutdown_manager.shutdown()
    state.color_obj.print_colored("\n正在安全退出...", state.color_obj.YELLOW)
    _shutdown_postprocess_executor()
    cleanup_all_ffmpeg_processes()
    close_clients()
    sys.exit(0)


def initialize_process(*, cli: bool = False) -> None:
    """初始化外部运行时和退出钩子；CLI 另行安装信号处理器。"""
    global language, skip_proxy_check, _last_ssl_disable_platforms_raw
    initialize_runtime()
    if cli:
        signal.signal(signal.SIGINT, safe_exit)
        signal.signal(signal.SIGTERM, safe_exit)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, safe_exit)
    atexit.register(archive_runtime_logs, reopen_streams=False)
    atexit.register(cleanup_all_ffmpeg_processes)
    atexit.register(close_clients)
    atexit.register(shutdown_manager.shutdown)
    current_path = os.environ.get("PATH", "")
    bundled_ffmpeg = os.path.normpath(ffmpeg_path)
    if (
        bundled_ffmpeg
        and bundled_ffmpeg not in current_path.split(os.pathsep)
        and should_prepend_bundled_ffmpeg_dir(bundled_ffmpeg, current_path)
    ):
        os.environ["PATH"] = bundled_ffmpeg + os.pathsep + current_path

    print("-----------------------------------------------------")
    print("|                DouyinLiveRecorder                 |")
    print("-----------------------------------------------------")
    print(i18n.tr("版本号: {version}", version=state.version))
    print("GitHub: https://github.com/ihmily/DouyinLiveRecorder")
    print(i18n.tr("支持平台: {platforms}", platforms=platforms))
    print(".....................................................")
    os.makedirs(os.path.dirname(state.config_file), exist_ok=True)
    if ensure_config_migrated(state.config_file, state.legacy_config_file):
        print(i18n.tr("已将 config.ini 迁移为 config.toml（旧文件已改名，不再读取）"))
    try:
        config.read(state.config_file)
    except ConfigError as e:
        logger.error(i18n.tr("配置文件不可用，本轮使用默认值且不回写: {e}", e=e))
    language = read_config_value(config, "录制设置", "language", "")
    set_file_logging(read_config_bool(config, "录制设置", "是否启用日志文件(是/否)", True))
    language = _i18n_resolve(language)
    _i18n_set_language(language)
    skip_proxy_check = read_config_bool(config, "录制设置", "是否跳过代理检测(是/否)", False)
    state.enable_https_recording = _read_https_recording_config(config)
    policy.verify_stream_tls = not state.enable_https_recording
    if config.has_option("录制设置", "是否禁用SSL证书验证(是/否)"):
        if read_config_bool(config, "录制设置", "是否禁用SSL证书验证(是/否)", False):
            print("提示: 「是否禁用SSL证书验证(是/否)」已整合进「是否启用https录制」，旧配置项不再生效")
    _ssl_disable_platforms = _apply_ssl_platform_exempt(config)
    builtins.print = translated_print  # type: ignore[assignment]
    try:
        if skip_proxy_check:
            state.global_proxy = True
        else:
            proxy = detect_system_proxy()
            state.global_proxy = proxy is not None
            if proxy is not None:
                print(i18n.tr("系统代理: {proxy}", proxy=utils.mask_credentials(proxy.url)))
    except Exception as err:
        print("An unexpected error occurred:", err)
    threading.Thread(target=backup_file_start, daemon=True).start()
    _last_ssl_disable_platforms_raw = ",".join(sorted(_ssl_disable_platforms))


def check_ffmpeg_existence() -> bool:
    """打印 ffmpeg 版本并检测可用性（缺失时触发下载安装）；返回是否可用。"""
    try:
        result = subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True, text=True, errors="replace")
        lines = result.stdout.splitlines()
        version_line = lines[0] if lines else "unknown"
        built_line = lines[1] if len(lines) > 1 else ""
        print(version_line)
        if built_line:
            print(built_line)
    except subprocess.CalledProcessError as e:
        logger.error(
            i18n.tr(
                "ffmpeg 版本检查命令执行失败: {type_name}: {e}",
                type_name=type(e).__name__,
                e=e,
            )
        )
    except FileNotFoundError:
        pass
    return check_ffmpeg()


config: ConfigStore = ConfigStore(state.config_file)


def _hot_reload_config() -> bool:
    """配置文件变化时重新解析并校验；未变化时零 I/O。"""
    try:
        return config.reload_if_changed()
    except Exception as e:
        logger.warning(i18n.tr("发生 I/O 或配置解析错误: {err}", err=f"{type(e).__name__}: {e}"))
        return False


def _read_https_recording_config(config_parser: ConfigStore) -> bool:
    if config_parser.has_option("录制设置", "是否启用https录制"):
        return read_config_bool(config_parser, "录制设置", "是否启用https录制", False)
    if config_parser.has_option("录制设置", "是否强制启用https录制"):
        legacy = read_config_bool(config_parser, "录制设置", "是否强制启用https录制", False)
        return read_config_bool(config_parser, "录制设置", "是否启用https录制", legacy)
    return read_config_bool(config_parser, "录制设置", "是否启用https录制", False)


SSL_DISABLE_REQUIRED_PLATFORMS: tuple[str, ...] = ("虎牙直播", "B站直播")


def _sync_ssl_disable_platforms(config_parser: ConfigStore) -> set[str]:
    """解析「禁用SSL证书验证的平台」并自动追加必需项；返回合并后的集合。"""
    raw = read_config_value(config_parser, "录制设置", "禁用SSL证书验证的平台(逗号分隔)", "")
    kept = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    appended = [p for p in SSL_DISABLE_REQUIRED_PLATFORMS if p not in kept]
    if appended:
        merged = kept + appended
        new_value = ",".join(merged)
        from src.api.config import update_config_line

        if not update_config_line(state.config_file, "录制设置", "禁用SSL证书验证的平台(逗号分隔)", new_value):
            logger.warning(
                i18n.tr(
                    "自动追加需禁用SSL验证的平台 {appended} 写回配置失败（已忽略，内存态仍生效）", appended=appended
                )
            )
        config_parser.set("录制设置", "禁用SSL证书验证的平台(逗号分隔)", new_value)
        print(i18n.tr("提示: 已自动追加需禁用SSL证书验证的平台: {appended}", appended=",".join(appended)))
    return set(kept + appended)


def _apply_ssl_platform_exempt(config_parser: ConfigStore) -> set[str]:
    """解析平台豁免并落到 policy（幂等、只追加不移除）；两个调用点共用。"""
    platforms = _sync_ssl_disable_platforms(config_parser)
    if config_parser.has_option("录制设置", "虎牙是否禁用SSL证书验证(是/否)"):
        if read_config_bool(config_parser, "录制设置", "虎牙是否禁用SSL证书验证(是/否)", False):
            platforms.add("虎牙直播")
    for _p in platforms:
        policy.stream_tls_overrides[_p] = False
    return platforms


_last_ssl_disable_platforms_raw = ""
language = i18n.DEFAULT_LANGUAGE
skip_proxy_check = False


def main(non_interactive: bool = False) -> None:
    """程序主入口（CLI 直跑与 web.py 守护线程共用）：校验 ffmpeg 后死循环「热加载 config.toml →。"""
    follow_monitor.start()
    douyin_dm_monitor.start()
    global language
    global _last_ssl_disable_platforms_raw

    if not check_ffmpeg_existence():
        logger.error("缺少ffmpeg无法进行录制，程序退出")
        return

    if os.path.isfile(state.url_config_file):
        config_files.remove_duplicate_lines(state.url_config_file)

    try:
        os.makedirs(state.default_path, exist_ok=True)
    except OSError as e:
        logger.debug(i18n.tr("创建默认下载目录失败: {type_name}: {e}", type_name=type(e).__name__, e=e))

    purge_stale_bytecode_caches(
        state.script_path,
        state.version,
        read_config_bool(config, "录制设置", "是否启动时清理陈旧字节码缓存(是/否)", True),
    )

    if state.scheduler is None:
        state.scheduler = ConcurrencyScheduler(configured_limit=state.max_request)
        state.semaphore = state.scheduler.network_semaphore
        state.recording_semaphore = state.scheduler.recording_semaphore

    while True:
        try:
            if not os.path.isfile(state.config_file):
                with open(state.config_file, "w", encoding="utf-8") as file:
                    pass

            if _hot_reload_config():
                set_file_logging(read_config_bool(config, "录制设置", "是否启用日志文件(是/否)", True))

            state.ini_URL_content = ""
            if os.path.isfile(state.url_config_file):
                with open(state.url_config_file, encoding=state.text_encoding, errors="ignore") as file:
                    state.ini_URL_content = file.read().strip()

            if not state.ini_URL_content.strip():
                if non_interactive:
                    if _interruptible_sleep(5):
                        time.sleep(3)
                    continue
                input_url = input("请输入要录制的主播直播间网址（尽量使用PC网页端的直播间地址）:\n")
                with open(state.url_config_file, "w", encoding=state.text_encoding) as file:
                    _ = file.write(input_url)
        except EOFError:
            logger.error(i18n.tr("无法从控制台读取直播间网址（无可用标准输入），等待配置文件写入"))
            if _interruptible_sleep(5):
                time.sleep(3)
            continue
        except (OSError, ConfigError) as err:
            logger.error(i18n.tr("发生 I/O 或配置解析错误: {err}", err=err))
            _interruptible_sleep(3)

        state.video_save_path = read_config_value(config, "录制设置", "直播保存路径(不填则默认)", "")
        state.folder_by_author = read_config_bool(config, "录制设置", "保存文件夹是否以作者区分", True)
        state.folder_by_time = read_config_bool(config, "录制设置", "保存文件夹是否以时间区分", False)
        state.folder_by_title = read_config_bool(config, "录制设置", "保存文件夹是否以标题区分", False)
        state.filename_by_title = read_config_bool(config, "录制设置", "保存文件名是否包含标题", False)
        state.clean_emoji = read_config_bool(config, "录制设置", "是否去除名称中的表情符号", True)
        state.auto_update_anchor_name = read_config_bool(config, "录制设置", "是否自动更新主播名(是/否)", True)
        state.video_save_type = read_config_value(
            config, "录制设置", "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频", "ts"
        )
        state.video_record_quality = read_config_value(config, "录制设置", "原画|超清|高清|标清|流畅", "原画")
        state.hls_collection_enabled = read_config_bool(config, "录制设置", "是否启用HLS采集(是/否)", True)
        hls_exclude_platforms_str = read_config_value(config, "录制设置", "HLS采集排除平台(逗号分隔)", "")
        state.hls_collection_exclude_platforms = (
            [p.strip() for p in hls_exclude_platforms_str.replace("，", ",").split(",") if p.strip()]
            if hls_exclude_platforms_str
            else []
        )
        state.use_proxy = read_config_bool(config, "录制设置", "是否使用代理ip(是/否)", True)
        state.proxy_addr_bak = read_config_value(config, "录制设置", "代理地址", "")
        state.proxy_addr = None if not state.use_proxy else state.proxy_addr_bak
        new_max_request = _safe_int(read_config_value(config, "录制设置", "同一时间访问网络的线程数", 3), 3)
        if new_max_request != state.max_request:
            state.max_request = new_max_request
            if state.scheduler is not None:
                state.scheduler.set_configured_limit(new_max_request)
            logger.debug(i18n.tr("并发线程数配置更新为 {max_request}", max_request=state.max_request))
        new_recording_limit = _safe_int(read_config_value(config, "录制设置", "最大同时录制数(0为不限制)", 0), 0)
        if state.scheduler is not None and new_recording_limit != state.scheduler.recording_limit:
            state.scheduler.set_recording_limit(new_recording_limit)
        if state.scheduler is not None:
            state.scheduler.set_dynamic_mode(new_recording_limit == 0)
        state.delay_default = max(30, _safe_int(read_config_value(config, "录制设置", "循环时间(秒)", 120), 120))
        state.local_delay_default = max(
            0, _safe_int(read_config_value(config, "录制设置", "排队读取网址时间(秒)", 0), 0)
        )
        state.loop_time = read_config_bool(config, "录制设置", "是否显示循环秒数", False)
        state.show_url = read_config_bool(config, "录制设置", "是否显示直播源地址", False)
        _new_language = _i18n_resolve(read_config_value(config, "录制设置", "language", ""))
        if _new_language != language:
            language = _new_language
            _i18n_set_language(_new_language)
            print(i18n.tr("语言已切换: {_new_language}", _new_language=_new_language))
        state.split_video_by_time = read_config_bool(config, "录制设置", "分段录制是否开启", False)
        state.enable_https_recording = _read_https_recording_config(config)
        policy.verify_stream_tls = not state.enable_https_recording
        _ssl_disable_raw = read_config_value(config, "录制设置", "禁用SSL证书验证的平台(逗号分隔)", "")
        if _ssl_disable_raw != _last_ssl_disable_platforms_raw:
            _last_ssl_disable_platforms_raw = _ssl_disable_raw
            _apply_ssl_platform_exempt(config)
        state.disk_space_limit = _safe_float(read_config_value(config, "录制设置", "录制空间剩余阈值(gb)", 1.0), 1.0)
        _split_time_raw = _safe_int(read_config_value(config, "录制设置", "视频分段时间(秒)", 1800), 1800)
        state.split_time = str(_split_time_raw if _split_time_raw > 0 else 1800)
        _max_record_raw = _safe_int(
            read_config_value(config, "录制设置", "单次录制时长上限(秒,0为不限制)", _MAX_RECORD_SECONDS),
            _MAX_RECORD_SECONDS,
        )
        state.max_record_seconds = _max_record_raw if _max_record_raw > 0 else 0
        state.converts_to_mp4 = read_config_bool(config, "录制设置", "录制完成后自动转为mp4格式", False)
        state.converts_to_h264 = read_config_bool(config, "录制设置", "mp4格式重新编码为h264", False)
        state.delete_origin_file = read_config_bool(config, "录制设置", "追加格式后删除原文件", False)
        state.create_time_file = read_config_bool(config, "录制设置", "生成时间字幕文件", False)
        state.is_run_script = read_config_bool(config, "录制设置", "是否录制完成后执行自定义脚本", False)
        state.custom_script = (
            read_config_value(config, "录制设置", "自定义脚本执行命令", "") if state.is_run_script else None
        )
        state.enable_proxy_platform = read_config_value(
            config,
            "录制设置",
            "使用代理录制的平台(逗号分隔)",
            "tiktok, soop, pandalive, winktv, flextv, popkontv, twitch, liveme, showroom, chzzk, shopee, shp, youtu, faceit",
        )
        state.enable_danmaku = read_config_bool(config, "录制设置", "是否录制弹幕(是/否)", False)
        state.enable_danmaku_monitor = read_config_bool(config, "录制设置", "是否弹幕监控(是/否)", False)
        state.danmaku_split_time = _safe_float(read_config_value(config, "录制设置", "弹幕分片时长(秒)", 1800), 1800.0)
        danmaku_platforms_str = read_config_value(
            config, "录制设置", "弹幕录制平台(逗号分隔)", "斗鱼直播,B站直播,虎牙直播,抖音直播,TwitchTV"
        )
        state.danmaku_platforms = (
            [p.strip() for p in danmaku_platforms_str.replace("，", ",").split(",") if p.strip()]
            if danmaku_platforms_str
            else []
        )
        state.enable_proxy_platform_list = (
            state.enable_proxy_platform.replace("，", ",").split(",") if state.enable_proxy_platform else None
        )
        state.extra_enable_proxy = read_config_value(config, "录制设置", "额外使用代理录制的平台(逗号分隔)", "")
        state.extra_enable_proxy_platform_list = (
            state.extra_enable_proxy.replace("，", ",").split(",") if state.extra_enable_proxy else None
        )
        state.live_status_push = read_config_value(config, "推送配置", "直播状态推送渠道", "")
        state.dingtalk_api_url = read_config_value(config, "推送配置", "钉钉推送接口链接", "")
        state.xizhi_api_url = read_config_value(config, "推送配置", "微信推送接口链接", "")
        state.bark_msg_api = read_config_value(config, "推送配置", "bark推送接口链接", "")
        state.bark_msg_level = read_config_value(config, "推送配置", "bark推送中断级别", "active")
        state.bark_level_overrides = (
            dict(config.items("Bark主播中断级别")) if config.has_section("Bark主播中断级别") else {}
        )
        state.bark_msg_ring = read_config_value(config, "推送配置", "bark推送铃声", "bell")
        state.dingtalk_phone_num = read_config_value(config, "推送配置", "钉钉通知@对象(填手机号)", "")
        state.dingtalk_is_atall = read_config_bool(config, "推送配置", "钉钉通知@全体(是/否)", False)
        state.tg_token = read_config_value(config, "推送配置", "tgapi令牌", "")
        state.tg_chat_id = read_config_value(config, "推送配置", "tg聊天id(个人或者群组id)", "")
        state.email_host = read_config_value(config, "推送配置", "SMTP邮件服务器", "")
        state.open_smtp_ssl = read_config_bool(config, "推送配置", "是否使用SMTP服务SSL加密(是/否)", True)
        state.smtp_port = read_config_value(config, "推送配置", "SMTP邮件服务器端口", "")
        state.login_email = read_config_value(config, "推送配置", "邮箱登录账号", "")
        state.email_password = read_config_value(config, "推送配置", "发件人密码(授权码)", "")
        state.sender_email = read_config_value(config, "推送配置", "发件人邮箱", "")
        state.sender_name = read_config_value(config, "推送配置", "发件人显示昵称", "")
        state.to_email = read_config_value(config, "推送配置", "收件人邮箱", "")
        state.ntfy_api = read_config_value(config, "推送配置", "ntfy推送地址", "")
        state.ntfy_tags = read_config_value(config, "推送配置", "ntfy推送标签", "tada")
        state.ntfy_email = read_config_value(config, "推送配置", "ntfy推送邮箱", "")
        state.pushplus_token = read_config_value(config, "推送配置", "pushplus推送token", "")
        state.push_message_title = read_config_value(config, "推送配置", "自定义推送标题", "直播间状态更新通知")
        state.begin_push_message_text = read_config_value(config, "推送配置", "自定义开播推送内容", "")
        state.over_push_message_text = read_config_value(config, "推送配置", "自定义关播推送内容", "")
        state.disable_record = read_config_bool(config, "推送配置", "只推送通知不录制(是/否)", False)
        state.push_check_seconds = max(
            30, _safe_int(read_config_value(config, "推送配置", "直播推送检测频率(秒)", 1800), 1800)
        )
        state.begin_show_push = read_config_bool(config, "推送配置", "开播推送开启(是/否)", True)
        state.over_show_push = read_config_bool(config, "推送配置", "关播推送开启(是/否)", False)
        state.sooplive_username = read_config_value(config, "账号密码", "sooplive账号", "")
        state.sooplive_password = read_config_value(config, "账号密码", "sooplive密码", "")
        state.flextv_username = read_config_value(config, "账号密码", "flextv账号", "")
        state.flextv_password = read_config_value(config, "账号密码", "flextv密码", "")
        state.popkontv_username = read_config_value(config, "账号密码", "popkontv账号", "")
        state.popkontv_partner_code = read_config_value(config, "账号密码", "partner_code", "P-00001")
        state.popkontv_password = read_config_value(config, "账号密码", "popkontv密码", "")
        state.twitcasting_account_type = read_config_value(config, "账号密码", "twitcasting账号类型", "normal")
        state.twitcasting_username = read_config_value(config, "账号密码", "twitcasting账号", "")
        state.twitcasting_password = read_config_value(config, "账号密码", "twitcasting密码", "")
        state.popkontv_access_token = read_config_value(config, "Authorization", "popkontv_token", "")
        state.dy_cookie = read_config_value(config, "Cookie", "抖音cookie", "")
        follow_mode = read_config_value(config, "录制设置", "抖音直播检测方式(逐个/关注优先)", "逐个")
        follow_interval = int(read_config_value(config, "录制设置", "抖音关注轮询间隔(秒)", 60))
        follow_proxy = (
            state.proxy_addr_bak if state.use_proxy and "douyin" in (state.enable_proxy_platform_list or []) else ""
        )
        follow_monitor.configure(follow_mode == "关注优先", state.dy_cookie, follow_interval, follow_proxy)
        if state.first_run and follow_mode == "关注优先" and state.dy_cookie:
            follow_monitor.wait_initial(timeout=20)
        douyin_dm_monitor.configure(
            enabled=read_config_bool(config, "录制设置", "抖音私信分享自动录制(是/否)", False),
            cookie=state.dy_cookie,
            proxy=follow_proxy,
            interval=_safe_int(read_config_value(config, "录制设置", "抖音私信轮询间隔(秒)", 60), 60),
            daily_limit=_safe_int(read_config_value(config, "录制设置", "抖音私信自动录制每日上限(个)", 20), 20),
            cooldown=_safe_int(read_config_value(config, "录制设置", "抖音私信自动录制冷却时间(秒)", 60), 60),
            quality=read_config_value(config, "录制设置", "抖音私信自动录制画质", "原画"),
            require_live=read_config_bool(config, "录制设置", "抖音私信自动录制仅在播(是/否)", False),
            lookback_minutes=_safe_int(read_config_value(config, "录制设置", "抖音私信自动录制回溯分钟", 30), 30),
        )
        state.ks_cookie = read_config_value(config, "Cookie", "快手cookie", "")
        state.tiktok_cookie = read_config_value(config, "Cookie", "tiktok_cookie", "")
        state.hy_cookie = read_config_value(config, "Cookie", "虎牙cookie", "")
        state.douyu_cookie = read_config_value(config, "Cookie", "斗鱼cookie", "")
        state.yy_cookie = read_config_value(config, "Cookie", "yy_cookie", "")
        state.bili_cookie = read_config_value(config, "Cookie", "B站cookie", "")
        state.xhs_cookie = read_config_value(config, "Cookie", "小红书cookie", "")
        state.bigo_cookie = read_config_value(config, "Cookie", "bigo_cookie", "")
        state.blued_cookie = read_config_value(config, "Cookie", "blued_cookie", "")
        state.sooplive_cookie = read_config_value(config, "Cookie", "sooplive_cookie", "")
        state.netease_cookie = read_config_value(config, "Cookie", "netease_cookie", "")
        state.qiandurebo_cookie = read_config_value(config, "Cookie", "千度热播_cookie", "")
        state.pandatv_cookie = read_config_value(config, "Cookie", "pandatv_cookie", "")
        state.maoerfm_cookie = read_config_value(config, "Cookie", "猫耳fm_cookie", "")
        state.winktv_cookie = read_config_value(config, "Cookie", "winktv_cookie", "")
        state.flextv_cookie = read_config_value(config, "Cookie", "flextv_cookie", "")
        state.look_cookie = read_config_value(config, "Cookie", "look_cookie", "")
        state.twitcasting_cookie = read_config_value(config, "Cookie", "twitcasting_cookie", "")
        state.baidu_cookie = read_config_value(config, "Cookie", "baidu_cookie", "")
        state.weibo_cookie = read_config_value(config, "Cookie", "weibo_cookie", "")
        state.kugou_cookie = read_config_value(config, "Cookie", "kugou_cookie", "")
        state.twitch_cookie = read_config_value(config, "Cookie", "twitch_cookie", "")
        state.liveme_cookie = read_config_value(config, "Cookie", "liveme_cookie", "")
        state.huajiao_cookie = read_config_value(config, "Cookie", "huajiao_cookie", "")
        state.liuxing_cookie = read_config_value(config, "Cookie", "liuxing_cookie", "")
        state.showroom_cookie = read_config_value(config, "Cookie", "showroom_cookie", "")
        state.acfun_cookie = read_config_value(config, "Cookie", "acfun_cookie", "")
        state.changliao_cookie = read_config_value(config, "Cookie", "changliao_cookie", "")
        state.yinbo_cookie = read_config_value(config, "Cookie", "yinbo_cookie", "")
        state.yingke_cookie = read_config_value(config, "Cookie", "yingke_cookie", "")
        state.zhihu_cookie = read_config_value(config, "Cookie", "zhihu_cookie", "")
        state.chzzk_cookie = read_config_value(config, "Cookie", "chzzk_cookie", "")
        state.haixiu_cookie = read_config_value(config, "Cookie", "haixiu_cookie", "")
        state.vvxqiu_cookie = read_config_value(config, "Cookie", "vvxqiu_cookie", "")
        state.yiqilive_cookie = read_config_value(config, "Cookie", "17live_cookie", "")
        state.langlive_cookie = read_config_value(config, "Cookie", "langlive_cookie", "")
        state.pplive_cookie = read_config_value(config, "Cookie", "pplive_cookie", "")
        state.six_room_cookie = read_config_value(config, "Cookie", "6room_cookie", "")
        state.lehaitv_cookie = read_config_value(config, "Cookie", "lehaitv_cookie", "")
        state.huamao_cookie = read_config_value(config, "Cookie", "huamao_cookie", "")
        state.shopee_cookie = read_config_value(config, "Cookie", "shopee_cookie", "")
        state.youtube_cookie = read_config_value(config, "Cookie", "youtube_cookie", "")
        state.taobao_cookie = read_config_value(config, "Cookie", "taobao_cookie", "")
        state.jd_cookie = read_config_value(config, "Cookie", "jd_cookie", "")
        state.faceit_cookie = read_config_value(config, "Cookie", "faceit_cookie", "")
        state.migu_cookie = read_config_value(config, "Cookie", "migu_cookie", "")
        state.lianjie_cookie = read_config_value(config, "Cookie", "lianjie_cookie", "")
        state.laixiu_cookie = read_config_value(config, "Cookie", "laixiu_cookie", "")
        state.picarto_cookie = read_config_value(config, "Cookie", "picarto_cookie", "")

        state.video_save_type_list = ("FLV", "MKV", "TS", "MP4", "MP3音频", "M4A音频", "MP3", "M4A")
        if state.video_save_type and state.video_save_type.upper() in state.video_save_type_list:
            state.video_save_type = state.video_save_type.upper()
        else:
            state.video_save_type = "TS"

        check_path = state.video_save_path or state.default_path
        try:
            os.makedirs(check_path, exist_ok=True)
            disk_free_gb = utils.check_disk_capacity(check_path, show=state.first_run)
        except Exception as e:
            logger.warning(
                i18n.tr("磁盘空间检测失败（跳过限制检查）: {type_name}: {e}", type_name=type(e).__name__, e=e)
            )
            disk_free_gb = float("inf")
        if disk_free_gb < state.disk_space_limit:
            if not state.disk_limited:
                state.disk_limited = True
                logger.warning(
                    i18n.tr(
                        "磁盘剩余空间低于 {disk_space_limit} GB，暂停拉起新录制并等待现有录制结束",
                        disk_space_limit=state.disk_space_limit,
                    )
                )
                state.exit_recording = True
                if not state.recording:
                    logger.warning(
                        i18n.tr(
                            "Disk space remaining is below {disk_space_limit} GB."
                            " Exiting program due to the disk space limit being reached.",
                            disk_space_limit=state.disk_space_limit,
                        )
                    )
                    _shutdown_postprocess_executor()
                    if non_interactive:
                        logger.warning("Web 模式：录制引擎已因磁盘空间不足停止，Web 面板继续服务")
                        return
                    sys.exit(-1)
        elif state.disk_limited:
            state.disk_limited = False
            state.exit_recording = False
            logger.warning(
                i18n.tr(
                    "磁盘空间已恢复（剩余 {disk_free_gb} GB），解除录制暂停并继续按配置拉起房间",
                    disk_free_gb=disk_free_gb,
                )
            )

        try:
            _url_comments: set[str] = set()
            _line_list: set[str] = set()
            _url_line_list: set[str] = set()
            _seen_urls: set[str] = set()
            state.url_tuples_list = []
            with open(state.url_config_file, encoding=state.text_encoding, errors="ignore") as file:
                _url_lines = file.readlines()
            for origin_line in _url_lines:
                try:
                    if origin_line in _line_list:
                        if delete_line(state.url_config_file, origin_line) is False:
                            logger.warning(
                                i18n.tr(
                                    "删除重复配置行失败: {origin_line}",
                                    origin_line=utils.mask_credentials(origin_line.strip()),
                                )
                            )
                    _line_list.add(origin_line)
                    line = origin_line.strip()
                    if len(line) < 18:
                        continue

                    line_spilt = line.split("主播: ")
                    if len(line_spilt) > 2:
                        middle = " ".join(line_spilt[1:-1])
                        line = (
                            update_file(state.url_config_file, line, f"{line_spilt[0]}主播: {middle} {line_spilt[-1]}")
                            or line
                        )

                    is_comment_line = line.startswith("#")
                    if is_comment_line:
                        line = line.lstrip("#")

                    if re.search("[,，]", line):
                        split_line = re.split("[,，]", line)
                    else:
                        split_line = [line, ""]

                    if len(split_line) == 2:
                        if contains_url(split_line[0]):
                            quality = state.video_record_quality
                            url, name = split_line
                        else:
                            quality, url = split_line
                            name = ""
                    elif contains_url(split_line[0]):
                        quality = state.video_record_quality
                        url = split_line[0]
                        name = "，".join(split_line[1:])
                    else:
                        quality, url = split_line[0], split_line[1]
                        name = "，".join(split_line[2:])

                    if quality not in (
                        "原画",
                        "蓝光",
                        "蓝光4M",
                        "蓝光8M",
                        "蓝光20M",
                        "蓝光30M",
                        "超清",
                        "高清",
                        "标清",
                        "流畅",
                    ):
                        quality = "原画"

                    if url in _url_line_list:
                        if delete_line(state.url_config_file, origin_line) is False:
                            logger.warning(
                                i18n.tr(
                                    "删除重复配置行失败: {origin_line}",
                                    origin_line=utils.mask_credentials(origin_line.strip()),
                                )
                            )
                    else:
                        _url_line_list.add(url)

                    url = "https://" + url if "://" not in url else url
                    url_host = url.split("/")[2]

                    if "live.shopee." in url_host or ".shp.ee" in url_host:
                        url_host = "live.shopee." if "live.shopee." in url_host else ".shp.ee"

                    if url_host in PLATFORM_HOST or _stream_path_suffix(url) in (".flv", ".m3u8"):
                        if url_host in CLEAN_URL_HOST_LIST:
                            url = update_file(state.url_config_file, old_str=url, new_str=url.split("?")[0]) or url

                        if url_host == "www.xiaohongshu.com":
                            try:
                                normalized = spider.parse_xhs_live_url(url)["normalized_url"]
                            except spider.XhsUrlError:
                                normalized = url
                            if normalized != url:
                                url = update_file(state.url_config_file, old_str=url, new_str=normalized) or url
                        _seen_urls.add(url)
                        _url_comments.discard(url)
                        if is_comment_line:
                            _url_comments.add(url)
                        else:
                            new_line = (quality, url, name)
                            state.url_tuples_list.append(new_line)
                    else:
                        if not origin_line.startswith("#"):
                            state.color_obj.print_colored(
                                i18n.tr(
                                    "\r{line} 本行包含未知链接.此条跳过",
                                    line=utils.mask_credentials(origin_line.strip()),
                                ),
                                state.color_obj.YELLOW,
                            )
                            _ = update_file(
                                state.url_config_file, old_str=origin_line, new_str=origin_line, start_str="#"
                            )

                except Exception as err:
                    logger.error(
                        i18n.tr(
                            "直播间配置行解析失败（已跳过该行）: {line} - {err}",
                            line=utils.mask_credentials(origin_line.strip()),
                            err=err,
                        )
                    )
                    continue
            while len(state.need_update_line_list):
                a = state.need_update_line_list.pop()
                replace_words = a.split("|")
                if replace_words[0] != replace_words[1]:
                    if replace_words[1].startswith("#"):
                        start_with = "#"
                        new_word = replace_words[1][1:]
                    else:
                        start_with = None
                        new_word = replace_words[1]
                    _ = update_file(
                        state.url_config_file, old_str=replace_words[0], new_str=new_word, start_str=start_with
                    )
            running_snapshot = list(state.running_list)
            for running_url in running_snapshot:
                if running_url not in _seen_urls and running_url not in _url_comments:
                    _url_comments.add(running_url)

            state.url_comments = _url_comments

            state.text_no_repeat_url = list(dict.fromkeys(state.url_tuples_list))

            if len(state.text_no_repeat_url) > 0:
                for url_tuple in state.text_no_repeat_url:
                    with state.record_state_lock:
                        state.monitoring = len(state.running_list)

                    if url_tuple[1] in state.not_record_list:
                        continue

                    if (
                        url_tuple[1] not in state.running_list
                        and not state.exit_recording
                        and not state.disk_limited
                        and state.recording_enabled
                    ):
                        print(
                            i18n.tr(
                                "\r{first_start}地址: {url_tuple}",
                                first_start="新增" if not state.first_start else "传入",
                                url_tuple=utils.mask_credentials(url_tuple[1]),
                            )
                        )
                        with state.record_state_lock:
                            state.monitoring += 1
                            state.running_list.append(url_tuple[1])
                            args = (url_tuple, state.monitoring)

                        def _room_thread_target(
                            _key: str, _args: tuple[tuple[str, str, str], int], _room_state: dict[str, str]
                        ) -> None:
                            """线程入口：_key 是 create_var 注册键，_args 是 start_record 参数元组，。"""
                            try:
                                start_record(*_args, room_state=_room_state)
                            finally:
                                remove_room_from_running(_args[0][1])
                                if _args[0][1]:
                                    _not_record_prefix = _args[0][1].split("?", 1)[0] + "?"
                                    state.not_record_list[:] = [
                                        u for u in state.not_record_list if not u.startswith(_not_record_prefix)
                                    ]
                                if _room_state.get("name"):
                                    try:
                                        from src.danmaku.monitor import get_hub

                                        get_hub().room_stopped(_room_state["name"], "房间已停止录制")
                                    except Exception:
                                        pass
                                with state.record_state_lock:
                                    if state.create_var.get(_key) is threading.current_thread():
                                        state.create_var.pop(_key, None)

                        thread_key = f"thread-{uuid.uuid4().hex[:12]}"
                        room_state: dict[str, str] = {"name": ""}
                        state.create_var[thread_key] = threading.Thread(
                            target=_room_thread_target,
                            name=thread_key,
                            args=(thread_key, args, room_state),
                        )
                        state.create_var[thread_key].daemon = True
                        try:
                            state.create_var[thread_key].start()
                        except RuntimeError as e:
                            state.create_var.pop(thread_key, None)
                            remove_room_from_running(url_tuple[1])
                            logger.warning(
                                i18n.tr(
                                    "录制线程启动失败，本轮跳过该房间（下轮会重试）: {url} {type_name}: {e}",
                                    url=utils.mask_credentials(url_tuple[1]),
                                    type_name=type(e).__name__,
                                    e=e,
                                )
                            )
                            continue
                        _interruptible_sleep(state.local_delay_default)
            if state.scheduler is not None:
                state.scheduler.set_active_count(state.monitoring)
            state.first_start = False

        except Exception as err:
            logger.error(
                i18n.tr(
                    "错误信息: {err} 发生错误的行数: {get_error_line}", err=err, get_error_line=_get_error_line(err)
                )
            )

        if state.first_run:
            t = threading.Thread(target=display_info, args=(), daemon=True)
            t.start()
            t2 = threading.Thread(target=adjust_max_request, args=(), daemon=True)
            t2.start()
            state.first_run = False

        time.sleep(3)
