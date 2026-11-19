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

from loguru import logger

from src.api.config import update_config_line
from src.app import version as app_version
from src.app.graceful_shutdown import shutdown_manager
from src.app.log_archive import archive_runtime_logs
from src.app.startup_cleanup import purge_stale_bytecode_caches
from src.config import files as config_files
from src.config import paths as config_paths
from src.config import settings
from src.config.io import (
    backup_file_start,
    delete_line,
    read_config_bool,
    read_config_value,
    update_file,
)
from src.config.store import ConfigError, ConfigStore, ensure_config_migrated
from src.network import policy
from src.network.http import close_clients
from src.network.proxy import detect_system_proxy
from src.notifications.notify import adjust_max_request
from src.platforms.live import spider
from src.recording import state as recording_state
from src.recording.pipeline.dispatch import CLEAN_URL_HOST_LIST, PLATFORM_HOST, _stream_path_suffix
from src.recording.pipeline.selection import contains_url
from src.recording.process import cleanup_all_ffmpeg_processes
from src.recording.scheduler import ConcurrencyScheduler
from src.recording.service import RecordingService
from src.recording.session import _shutdown_postprocess_executor
from src.recording.sources.account import douyin_dm_monitor, follow_monitor
from src.recording.sources.room import _interruptible_sleep
from src.recording.status import display_info
from src.runtime.ffmpeg import check_ffmpeg, ffmpeg_path, should_prepend_bundled_ffmpeg_dir
from src.runtime.node import initialize_node_runtime
from src.support import console, i18n, utils
from src.support.errors import get_error_line
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
    recording_state.exit_recording = True
    shutdown_manager.shutdown()
    console.color_obj.print_colored("\n正在安全退出...", console.color_obj.YELLOW)
    _shutdown_postprocess_executor()
    cleanup_all_ffmpeg_processes()
    close_clients()
    sys.exit(0)


def initialize_process(*, cli: bool = False) -> None:
    """初始化外部运行时和退出钩子；CLI 另行安装信号处理器。"""
    global language, skip_proxy_check, _last_ssl_disable_platforms_raw
    initialize_node_runtime()
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
    print("|                        dylr                       |")
    print("-----------------------------------------------------")
    print(i18n.tr("版本号: {version}", version=app_version.version))
    print("GitHub: https://github.com/oxygenkun/dylr")
    print("上游: https://github.com/ihmily/DouyinLiveRecorder")
    print(i18n.tr("支持平台: {platforms}", platforms=platforms))
    print(".....................................................")
    os.makedirs(os.path.dirname(config_paths.config_file), exist_ok=True)
    if ensure_config_migrated(config_paths.config_file, config_paths.legacy_config_file):
        print(i18n.tr("已将 config.ini 迁移为 config.toml（旧文件已改名，不再读取）"))
    try:
        config.read(config_paths.config_file)
    except ConfigError as e:
        logger.error(i18n.tr("配置文件不可用，本轮使用默认值且不回写: {e}", e=e))
    language = read_config_value(config, "录制设置", "language", "")
    set_file_logging(read_config_bool(config, "录制设置", "是否启用日志文件(是/否)", True))
    language = _i18n_resolve(language)
    _i18n_set_language(language)
    skip_proxy_check = read_config_bool(config, "录制设置", "是否跳过代理检测(是/否)", False)
    settings.enable_https_recording = settings.read_https_recording(config)
    policy.verify_stream_tls = not settings.enable_https_recording
    if config.has_option("录制设置", "是否禁用SSL证书验证(是/否)"):
        if read_config_bool(config, "录制设置", "是否禁用SSL证书验证(是/否)", False):
            print("提示: 「是否禁用SSL证书验证(是/否)」已整合进「是否启用https录制」，旧配置项不再生效")
    _ssl_disable_platforms = _apply_ssl_platform_exempt(config)
    builtins.print = translated_print  # type: ignore[assignment]
    try:
        if skip_proxy_check:
            recording_state.global_proxy = True
        else:
            proxy = detect_system_proxy()
            recording_state.global_proxy = proxy is not None
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


config: ConfigStore = ConfigStore(config_paths.config_file)


def _hot_reload_config() -> bool:
    """配置文件变化时重新解析并校验；未变化时零 I/O。"""
    try:
        return config.reload_if_changed()
    except Exception as e:
        logger.warning(i18n.tr("发生 I/O 或配置解析错误: {err}", err=f"{type(e).__name__}: {e}"))
        return False


SSL_DISABLE_REQUIRED_PLATFORMS: tuple[str, ...] = ("虎牙直播", "B站直播")


def _sync_ssl_disable_platforms(config_parser: ConfigStore) -> set[str]:
    """解析「禁用SSL证书验证的平台」并自动追加必需项；返回合并后的集合。"""
    raw = read_config_value(config_parser, "录制设置", "禁用SSL证书验证的平台(逗号分隔)", "")
    kept = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    appended = [p for p in SSL_DISABLE_REQUIRED_PLATFORMS if p not in kept]
    if appended:
        merged = kept + appended
        new_value = ",".join(merged)
        if not update_config_line(config_paths.config_file, "录制设置", "禁用SSL证书验证的平台(逗号分隔)", new_value):
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

    if os.path.isfile(config_paths.url_config_file):
        config_files.remove_duplicate_lines(config_paths.url_config_file)

    try:
        os.makedirs(config_paths.default_path, exist_ok=True)
    except OSError as e:
        logger.debug(i18n.tr("创建默认下载目录失败: {type_name}: {e}", type_name=type(e).__name__, e=e))

    purge_stale_bytecode_caches(
        config_paths.script_path,
        app_version.version,
        read_config_bool(config, "录制设置", "是否启动时清理陈旧字节码缓存(是/否)", True),
    )

    if recording_state.scheduler is None:
        recording_state.scheduler = ConcurrencyScheduler(configured_limit=settings.max_request)
        recording_state.semaphore = recording_state.scheduler.network_semaphore
        recording_state.recording_semaphore = recording_state.scheduler.recording_semaphore
    if recording_state.recording_service is None:
        recording_state.recording_service = RecordingService()

    while True:
        try:
            if not os.path.isfile(config_paths.config_file):
                with open(config_paths.config_file, "w", encoding="utf-8") as file:
                    pass

            if _hot_reload_config():
                set_file_logging(read_config_bool(config, "录制设置", "是否启用日志文件(是/否)", True))

            config_files.ini_URL_content = ""
            if os.path.isfile(config_paths.url_config_file):
                with open(config_paths.url_config_file, encoding=config_paths.text_encoding, errors="ignore") as file:
                    config_files.ini_URL_content = file.read().strip()

            if not config_files.ini_URL_content.strip():
                if non_interactive:
                    if _interruptible_sleep(5):
                        time.sleep(3)
                    continue
                input_url = input("请输入要录制的主播直播间网址（尽量使用PC网页端的直播间地址）:\n")
                with open(config_paths.url_config_file, "w", encoding=config_paths.text_encoding) as file:
                    _ = file.write(input_url)
        except EOFError:
            logger.error(i18n.tr("无法从控制台读取直播间网址（无可用标准输入），等待配置文件写入"))
            if _interruptible_sleep(5):
                time.sleep(3)
            continue
        except (OSError, ConfigError) as err:
            logger.error(i18n.tr("发生 I/O 或配置解析错误: {err}", err=err))
            _interruptible_sleep(3)

        previous_max_request = settings.max_request
        settings.load(config)
        if settings.max_request != previous_max_request:
            if recording_state.scheduler is not None:
                recording_state.scheduler.set_configured_limit(settings.max_request)
            logger.debug(i18n.tr("并发线程数配置更新为 {max_request}", max_request=settings.max_request))
        if recording_state.scheduler is not None:
            if settings.recording_limit != recording_state.scheduler.recording_limit:
                recording_state.scheduler.set_recording_limit(settings.recording_limit)
            recording_state.scheduler.set_dynamic_mode(settings.recording_limit == 0)

        _new_language = _i18n_resolve(settings.language)
        if _new_language != language:
            language = _new_language
            _i18n_set_language(_new_language)
            print(i18n.tr("语言已切换: {_new_language}", _new_language=_new_language))
        policy.verify_stream_tls = not settings.enable_https_recording
        _ssl_disable_raw = read_config_value(config, "录制设置", "禁用SSL证书验证的平台(逗号分隔)", "")
        if _ssl_disable_raw != _last_ssl_disable_platforms_raw:
            _last_ssl_disable_platforms_raw = _ssl_disable_raw
            _apply_ssl_platform_exempt(config)

        follow_proxy = (
            settings.proxy_addr_bak
            if settings.use_proxy and "douyin" in (settings.enable_proxy_platform_list or [])
            else ""
        )
        follow_enabled = settings.douyin_follow_mode == "关注优先"
        follow_monitor.configure(follow_enabled, settings.dy_cookie, settings.douyin_follow_interval, follow_proxy)
        if recording_state.first_run and follow_enabled and settings.dy_cookie:
            follow_monitor.wait_initial(timeout=20)
        douyin_dm_monitor.configure(
            enabled=settings.douyin_dm_enabled,
            cookie=settings.dy_cookie,
            proxy=follow_proxy,
            interval=settings.douyin_dm_interval,
            daily_limit=settings.douyin_dm_daily_limit,
            cooldown=settings.douyin_dm_cooldown,
            quality=settings.douyin_dm_quality,
            require_live=settings.douyin_dm_require_live,
            lookback_minutes=settings.douyin_dm_lookback_minutes,
        )

        check_path = settings.video_save_path or config_paths.default_path
        try:
            os.makedirs(check_path, exist_ok=True)
            disk_free_gb = utils.check_disk_capacity(check_path, show=recording_state.first_run)
        except Exception as e:
            logger.warning(
                i18n.tr("磁盘空间检测失败（跳过限制检查）: {type_name}: {e}", type_name=type(e).__name__, e=e)
            )
            disk_free_gb = float("inf")
        if disk_free_gb < settings.disk_space_limit:
            if not recording_state.disk_limited:
                recording_state.disk_limited = True
                logger.warning(
                    i18n.tr(
                        "磁盘剩余空间低于 {disk_space_limit} GB，暂停拉起新录制并等待现有录制结束",
                        disk_space_limit=settings.disk_space_limit,
                    )
                )
                recording_state.exit_recording = True
                if not recording_state.recording:
                    logger.warning(
                        i18n.tr(
                            "Disk space remaining is below {disk_space_limit} GB."
                            " Exiting program due to the disk space limit being reached.",
                            disk_space_limit=settings.disk_space_limit,
                        )
                    )
                    _shutdown_postprocess_executor()
                    if non_interactive:
                        logger.warning("Web 模式：录制引擎已因磁盘空间不足停止，Web 面板继续服务")
                        return
                    sys.exit(-1)
        elif recording_state.disk_limited:
            recording_state.disk_limited = False
            recording_state.exit_recording = False
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
            recording_state.url_tuples_list = []
            with open(config_paths.url_config_file, encoding=config_paths.text_encoding, errors="ignore") as file:
                _url_lines = file.readlines()
            for origin_line in _url_lines:
                try:
                    if origin_line in _line_list:
                        if delete_line(config_paths.url_config_file, origin_line) is False:
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
                            update_file(
                                config_paths.url_config_file, line, f"{line_spilt[0]}主播: {middle} {line_spilt[-1]}"
                            )
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
                            quality = settings.video_record_quality
                            url, name = split_line
                        else:
                            quality, url = split_line
                            name = ""
                    elif contains_url(split_line[0]):
                        quality = settings.video_record_quality
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
                        if delete_line(config_paths.url_config_file, origin_line) is False:
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
                            url = (
                                update_file(config_paths.url_config_file, old_str=url, new_str=url.split("?")[0]) or url
                            )

                        if url_host == "www.xiaohongshu.com":
                            try:
                                normalized = spider.parse_xhs_live_url(url)["normalized_url"]
                            except spider.XhsUrlError:
                                normalized = url
                            if normalized != url:
                                url = update_file(config_paths.url_config_file, old_str=url, new_str=normalized) or url
                        _seen_urls.add(url)
                        _url_comments.discard(url)
                        if is_comment_line:
                            _url_comments.add(url)
                        else:
                            new_line = (quality, url, name)
                            recording_state.url_tuples_list.append(new_line)
                    else:
                        if not origin_line.startswith("#"):
                            console.color_obj.print_colored(
                                i18n.tr(
                                    "\r{line} 本行包含未知链接.此条跳过",
                                    line=utils.mask_credentials(origin_line.strip()),
                                ),
                                console.color_obj.YELLOW,
                            )
                            _ = update_file(
                                config_paths.url_config_file, old_str=origin_line, new_str=origin_line, start_str="#"
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
            while len(recording_state.need_update_line_list):
                a = recording_state.need_update_line_list.pop()
                replace_words = a.split("|")
                if replace_words[0] != replace_words[1]:
                    if replace_words[1].startswith("#"):
                        start_with = "#"
                        new_word = replace_words[1][1:]
                    else:
                        start_with = None
                        new_word = replace_words[1]
                    _ = update_file(
                        config_paths.url_config_file, old_str=replace_words[0], new_str=new_word, start_str=start_with
                    )
            running_snapshot = list(recording_state.running_list)
            for running_url in running_snapshot:
                if running_url not in _seen_urls and running_url not in _url_comments:
                    _url_comments.add(running_url)

            recording_state.url_comments = _url_comments

            recording_state.text_no_repeat_url = list(dict.fromkeys(recording_state.url_tuples_list))

            if recording_state.recording_service is not None:
                recording_state.recording_service.sync_rooms(recording_state.text_no_repeat_url)
            if recording_state.scheduler is not None:
                recording_state.scheduler.set_active_count(recording_state.monitoring)
            recording_state.first_start = False

        except Exception as err:
            logger.error(
                i18n.tr("错误信息: {err} 发生错误的行数: {get_error_line}", err=err, get_error_line=get_error_line(err))
            )

        if recording_state.first_run:
            t = threading.Thread(target=display_info, args=(), daemon=True)
            t.start()
            t2 = threading.Thread(target=adjust_max_request, args=(), daemon=True)
            t2.start()
            recording_state.first_run = False

        time.sleep(3)
