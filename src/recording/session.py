"""单房间录制、FFmpeg 命令与进程守护、录后处理。"""

import datetime
import hashlib
import ntpath
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import httpx
from loguru import logger

from src.app.graceful_shutdown import shutdown_manager
from src.config import paths as config_paths
from src.config import settings
from src.danmaku.plan import DanmakuPlan
from src.danmaku.service import DanmakuService
from src.network import policy
from src.network import urls as network_urls
from src.notifications.notify import clear_record_info, record_error, record_success, run_script
from src.recording import state as recording_state
from src.recording.pipeline.dispatch import OVERSEAS_PLATFORM_HOST, _stream_path_suffix
from src.recording.pipeline.selection import (
    MOBILE_UA,
    clean_name,
    clear_ffmpeg_reject,
    get_record_headers,
    get_record_user_agent,
    mark_ffmpeg_reject,
)
from src.recording.pipeline.stream import code_to_zh
from src.recording.pipeline.stream import is_downgrade as _is_downgrade
from src.recording.plan import RecordPlan, RecordResult
from src.recording.postprocess import converts_mp4, generate_subtitles, get_startup_info
from src.recording.process import (
    _terminate_ffmpeg_process,
    register_ffmpeg_process,
    unregister_ffmpeg_process,
)
from src.recording.scheduler import host_of
from src.recording.signals import RoomObservation
from src.support import console, i18n, utils
from src.support.errors import get_error_line


def _shutdown_postprocess_executor() -> None:
    """退出时限时排空本模块持有的后处理池。"""
    with _postprocess_executor_lock:
        pool = _postprocess_executor
    if pool is None:
        return
    if _run_bounded_drain(lambda: pool.shutdown(wait=True, cancel_futures=False)):
        return
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception as e:
        logger.warning(i18n.tr("后处理线程池收尾失败: {type_name}: {e}", type_name=type(e).__name__, e=e))


_POSTPROCESS_DRAIN_BUDGET_SECONDS = 15.0


def _run_bounded_drain(drain: Callable[[], None], budget: float = _POSTPROCESS_DRAIN_BUDGET_SECONDS) -> bool:
    done = threading.Event()

    def _target() -> None:
        try:
            drain()
        except Exception as e:
            logger.warning(i18n.tr("后处理线程池收尾失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        finally:
            done.set()

    worker = threading.Thread(target=_target, name="postprocess-drain", daemon=True)
    worker.start()
    if done.wait(budget):
        return True
    logger.warning(
        i18n.tr(
            "后处理线程池排空超过 {budget} 秒，已放弃等待并继续退出（在途转封装可能留下半截产物）",
            budget=int(budget),
        )
    )
    return False


def direct_download_stream(
    source_url: str,
    save_path: str,
    record_name: str,
    live_url: str,
    platform: str,
    cookies: str | None = None,
) -> bool:
    """httpx 流式直下 FLV（不经 ffmpeg）：请求头/cookie/UA 与 ffmpeg 路径同源；。"""
    _downloaded = 0
    try:
        with shutdown_manager.download(), open(save_path, "wb") as f:
            headers: dict[str, str] = {}
            header_params = get_record_headers(platform, live_url, cookies=cookies)
            if header_params:
                headers.update(header_params)
            headers["User-Agent"] = get_record_user_agent(platform) or MOBILE_UA

            with httpx.Client(timeout=15, verify=policy.should_verify_stream_tls(platform), trust_env=False) as client:
                with client.stream("GET", source_url, headers=headers, follow_redirects=True) as response:
                    if response.status_code != 200:
                        logger.error(
                            i18n.tr(
                                "请求直播流失败: {source_url} - 状态码: {status_code}",
                                source_url=utils.mask_credentials(source_url),
                                status_code=response.status_code,
                            )
                        )
                        return False

                    for chunk in response.iter_bytes():
                        if (
                            live_url in recording_state.url_comments
                            or recording_state.exit_recording
                            or not recording_state.recording_enabled
                        ):
                            console.color_obj.print_colored(
                                i18n.tr("[{record_name}]录制时已被注释或停止录制,下载中断", record_name=record_name),
                                console.color_obj.YELLOW,
                            )
                            clear_record_info(record_name, live_url)
                            return False

                        if chunk:
                            _ = f.write(chunk)
                            _downloaded += len(chunk)
                    print()
                    if _downloaded < _MIN_VALID_RECORD_BYTES:
                        logger.error(
                            i18n.tr(
                                "直下直播流响应体过小（{size} 字节），按失败处理: {masked_url}",
                                size=_downloaded,
                                masked_url=utils.mask_credentials(source_url),
                            )
                        )
                        return False
                    return True
    except Exception as e:
        logger.error(
            i18n.tr(
                "FLV下载错误: {source_url} - {type_name}: {e} 发生错误的行数: {get_error_line}",
                source_url=utils.mask_credentials(source_url),
                type_name=type(e).__name__,
                e=e,
                get_error_line=get_error_line(e),
            )
        )
        return False
    finally:
        if _downloaded == 0:
            try:
                os.remove(save_path)
                logger.debug(i18n.tr("已清理零字节的直下残留文件: {save_path}", save_path=save_path))
            except OSError:
                pass


SEGMENT_FORMAT_BY_SUFFIX: dict[str, str] = {
    ".ts": "mpegts",
    ".flv": "flv",
    ".mkv": "matroska",
    ".mp4": "mp4",
    ".m4a": "ipod",
    ".mp3": "mp3",
}


_FFMPEG_FAST_FAIL_SECONDS = 20.0

_SEM_WAIT_TICK = 1.0

_RECORD_STALL_SECONDS = 10 * 60
_RECORD_STALL_PROBE_INTERVAL = 30.0
_WATCH_TICK_MAX_SECONDS = 300.0
_WATCH_TICK_NOMINAL_SECONDS = 1.0

_MIN_VALID_RECORD_BYTES = 1024

_POSTPROCESS_WORKERS = max(2, min(4, (os.cpu_count() or 4) // 2))
_postprocess_executor: ThreadPoolExecutor | None = None
_postprocess_executor_lock = threading.Lock()


def _get_postprocess_executor() -> ThreadPoolExecutor:
    global _postprocess_executor
    with _postprocess_executor_lock:
        if _postprocess_executor is None:
            _postprocess_executor = ThreadPoolExecutor(
                max_workers=_POSTPROCESS_WORKERS, thread_name_prefix="postprocess"
            )
        return _postprocess_executor


def _submit_postprocess(func: Callable[..., Any], path: str, delete_origin: bool, record_name: str = "") -> None:
    """提交一个录后转码任务（含删除源文件开关）；线程池满时任务排队而非无限新建线程。"""
    if shutdown_manager.stopping.is_set():
        return
    try:
        _ = _get_postprocess_executor().submit(func, path, delete_origin)
    except RuntimeError as e:
        logger.warning(
            i18n.tr(
                "[{record_name}] 转码任务提交失败（{type_name}）: {e}",
                record_name=record_name or os.path.basename(path),
                type_name=type(e).__name__,
                e=e,
            )
        )


_FFMPEG_ERRNO_HINTS: dict[int, str] = {
    -22: " (EINVAL：无效参数，常见于容器/编码不匹配（如 HEVC 写进 ipod 容器）或输入选项解析失败)",
    -11: " (EAGAIN：输出写入被阻塞，多为磁盘 IO 或管道背压)",
    -2: " (ENOENT：输入地址或输出路径不存在)",
    -109: " (EBADF：连接被对端重置，多为 CDN 断流)",
    1: " (ffmpeg 常规错误，详见上方 ffmpeg 输出)",
    2: " (命令行参数错误)",
    255: " (ffmpeg 初始化失败/被中断)",
}


def _describe_return_code(return_code: int) -> str:
    """退出码归一化为有符号 errno：仅超出 32 位有符号正区间时做补码换算，正常小退出码原样返回。"""
    signed_rc = return_code - (1 << 32) if return_code > 0x7FFFFFFF else return_code
    return f"{signed_rc}{_FFMPEG_ERRNO_HINTS.get(signed_rc, '')}"


_SEG_INDEX_RE = re.compile(r"_\d+\.[A-Za-z0-9]+$")

_SEGMENT_TEMPLATE_RE = re.compile(r"_%0?\dd\.[A-Za-z0-9]+$")


def _is_segmented_output(save_file_path: str) -> bool:
    """输出路径是否为 ffmpeg 分段模板（决定产物求和方式、弹幕分片与录后转码的匹配形状）。"""
    return _SEGMENT_TEMPLATE_RE.search(save_file_path) is not None


def _segment_files(save_file_path: str) -> list[str] | None:
    """分段模板 → 实际落盘的分段文件：纯字符串前缀/后缀 + 序号正则，**绝不用 glob/fnmatch**。"""
    directory = os.path.dirname(save_file_path)
    template = os.path.basename(save_file_path)
    base_stem = template.rsplit("_", maxsplit=1)[0]
    prefix = base_stem + "_"
    suffix = "." + template.rsplit(".", maxsplit=1)[-1]
    matched: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(prefix) and name.endswith(suffix) and _SEG_INDEX_RE.search(name):
                    matched.append(entry.path)
    except OSError:
        return None
    return matched


def _record_output_bytes(save_file_path: str, split_video_by_time: bool) -> int | None:
    """统计本次录制实际落盘字节数：分段模板含 %03d，直接 stat 恒失败，须按 _000/_001… 求和；。"""
    if not split_video_by_time:
        try:
            return os.path.getsize(save_file_path)
        except OSError:
            return -1
    seg_paths = _segment_files(save_file_path)
    if seg_paths is None:
        return None
    total = 0
    found = False
    for seg_path in seg_paths:
        found = True
        try:
            total += os.path.getsize(seg_path)
        except OSError:
            continue
    return total if found else -1


def _generate_time_subtitles(record_name: str, subs_file_path: str, thread_name: str) -> None:
    try:
        generate_subtitles(record_name, subs_file_path)
    finally:
        with recording_state.record_state_lock:
            if recording_state.create_var.get(thread_name) is threading.current_thread():
                recording_state.create_var.pop(thread_name, None)


def check_subprocess(
    record_name: str,
    record_url: str,
    ffmpeg_command: list[str],
    save_type: str,
    script_command: str | None = None,
    platform: str | None = None,
    danmaku_args: Any = None,
) -> bool:
    """启动并守护一次 ffmpeg 录制：ffmpeg_command 末位为输出路径；save_type 决定转码与弹幕/字幕；。"""
    _rec_sem = recording_state.recording_semaphore
    while not _rec_sem.acquire(timeout=_SEM_WAIT_TICK):
        if (
            record_url in recording_state.url_comments
            or recording_state.exit_recording
            or not recording_state.recording_enabled
        ):
            logger.debug(
                i18n.tr(
                    "[{record_name}]等待录制槽位期间检测到停止信号，放弃本轮录制",
                    record_name=record_name,
                )
            )
            clear_record_info(record_name, record_url)
            return True
    process: subprocess.Popen[bytes] | None = None
    danmaku_collector: Any = None
    _converged = False
    try:
        save_file_path = ffmpeg_command[-1]
        _split_output = _is_segmented_output(save_file_path)
        _proc_started_mono = time.monotonic()
        with shutdown_manager.launch():
            process = subprocess.Popen(
                ffmpeg_command,
                stdin=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=get_startup_info(os.name),
                start_new_session=(os.name != "nt"),
            )
            shutdown_manager.track(process)

        register_ffmpeg_process(process)

        subs_file_path = save_file_path.rsplit(".", maxsplit=1)[0]

        for _seg_placeholder in ("_%02d", "_%03d"):
            subs_file_path = subs_file_path.replace(_seg_placeholder, "")

        _danmaku_active = settings.enable_danmaku or settings.enable_danmaku_monitor
        if (
            _danmaku_active
            and platform is not None
            and platform in settings.danmaku_platforms
            and "音频" not in save_type
        ):
            if not danmaku_args:
                logger.debug(
                    i18n.tr(
                        "[{record_name}]弹幕跳过: 平台={platform} 未获取到弹幕参数(danmaku_args 为空),请查看上游日志",
                        record_name=record_name,
                        platform=platform,
                    )
                )
            else:
                try:
                    danmaku_plan = DanmakuPlan(
                        platform=platform,
                        write_srt=settings.enable_danmaku,
                        monitor_only=not settings.enable_danmaku,
                        split_seconds=settings.danmaku_split_time if _split_output else None,
                        room_name=record_name,
                        base_filename=subs_file_path,
                        args=danmaku_args,
                    )
                    danmaku_collector = DanmakuService().create_session(danmaku_plan)
                    if danmaku_collector is not None:
                        danmaku_collector.start()
                except Exception as e:
                    logger.warning(
                        i18n.tr(
                            "[{record_name}]弹幕采集启动失败,不影响录制: {type_name}: {e}",
                            record_name=record_name,
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )
                    if danmaku_collector is not None:
                        danmaku_collector.stop()
                    danmaku_collector = None

        subs_thread_name = f"subs_{Path(subs_file_path).name}"
        if settings.create_time_file and not _split_output and "音频" not in save_type:
            with recording_state.record_state_lock:
                recording_state.create_var[subs_thread_name] = threading.Thread(
                    target=_generate_time_subtitles,
                    args=(record_name, subs_file_path, subs_thread_name),
                    name=subs_thread_name,
                    daemon=True,
                )
                try:
                    recording_state.create_var[subs_thread_name].start()
                except RuntimeError:
                    recording_state.create_var.pop(subs_thread_name, None)
                    raise

        _watch_prev_wall = time.time()
        _watch_elapsed = 0.0

        def _watch_seconds() -> float:
            nonlocal _watch_prev_wall, _watch_elapsed
            raw = time.time()
            delta = raw - _watch_prev_wall
            _watch_prev_wall = raw
            if 0.0 <= delta <= _WATCH_TICK_MAX_SECONDS:
                _watch_elapsed += delta
            else:
                _watch_elapsed += _WATCH_TICK_NOMINAL_SECONDS
            return _watch_elapsed

        _proc_started_watch = _watch_seconds()
        _stall_last_probe = _proc_started_watch
        _stall_last_size = -1
        _stall_since = _proc_started_watch

        _record_limit_seconds = settings.max_record_seconds

        def _watchdog_hit() -> str:
            nonlocal _stall_last_probe, _stall_last_size, _stall_since
            now = _watch_seconds()
            if _record_limit_seconds > 0 and now - _proc_started_watch > _record_limit_seconds:
                if _split_output:
                    logger.warning(
                        i18n.tr(
                            "[{record_name}] 达到单次录制时长上限（{limit}），结束本轮并分段续录",
                            record_name=record_name,
                            limit=f"{_record_limit_seconds}s",
                        )
                    )
                else:
                    logger.warning(
                        i18n.tr(
                            "[{record_name}] 达到单次录制时长上限（{limit}），结束本轮并在下轮新开一个文件",
                            record_name=record_name,
                            limit=f"{_record_limit_seconds}s",
                        )
                    )
                return "limit"
            if now - _stall_last_probe < _RECORD_STALL_PROBE_INTERVAL:
                return ""
            _stall_last_probe = now
            size = _record_output_bytes(save_file_path, _split_output)
            if size is None:
                return ""
            if size < 0:
                if now - _stall_since > _RECORD_STALL_SECONDS:
                    logger.warning(
                        i18n.tr(
                            "[{record_name}] 输出文件连续 {minutes} 分钟无增长，判定为停滞并终止",
                            record_name=record_name,
                            minutes=int(_RECORD_STALL_SECONDS // 60),
                        )
                    )
                    return "stall"
                return ""
            if size > _stall_last_size:
                _stall_last_size = size
                _stall_since = now
                return ""
            if _stall_last_size >= 0 and now - _stall_since > _RECORD_STALL_SECONDS:
                logger.warning(
                    i18n.tr(
                        "[{record_name}] 输出文件连续 {minutes} 分钟无增长，判定为停滞并终止",
                        record_name=record_name,
                        minutes=int(_RECORD_STALL_SECONDS // 60),
                    )
                )
                return "stall"
            return ""

        _reached_record_limit = False

        while process.poll() is None:
            _watchdog = _watchdog_hit()
            if _watchdog == "stall":
                _converged = True
                if danmaku_collector is not None:
                    danmaku_collector.stop()
                _ = _terminate_ffmpeg_process(process)
                unregister_ffmpeg_process(process)
                clear_record_info(record_name, record_url)
                record_error(host_of(record_url))
                return False
            if _watchdog == "limit":
                _converged = True
                _ = _terminate_ffmpeg_process(process)
                unregister_ffmpeg_process(process)
                _reached_record_limit = True
                break
            if (
                record_url in recording_state.url_comments
                or recording_state.exit_recording
                or not recording_state.recording_enabled
            ):
                console.color_obj.print_colored(
                    i18n.tr("[{record_name}]录制时已被注释或停止录制,本条线程将会退出", record_name=record_name),
                    console.color_obj.YELLOW,
                )
                _converged = True
                clear_record_info(record_name, record_url)

                if danmaku_collector is not None:
                    danmaku_collector.stop()

                success = _terminate_ffmpeg_process(process)
                if not success:
                    logger.warning(
                        i18n.tr("[{record_name}] ffmpeg 进程可能没有完全终止，请检查系统进程", record_name=record_name)
                    )

                unregister_ffmpeg_process(process)
                return True
            time.sleep(1)
    finally:
        _rec_sem.release()
        if danmaku_collector is not None:
            danmaku_collector.stop()
        if process is not None and not _converged:
            if process.poll() is None:
                _ = _terminate_ffmpeg_process(process)
            unregister_ffmpeg_process(process)
            clear_record_info(record_name, record_url)
        if process is not None:
            shutdown_manager.forget(process)

    proc = cast("subprocess.Popen[bytes]", process)

    try:
        _ = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning(i18n.tr("[{record_name}] ffmpeg 进程 30 秒内未退出，强制终止", record_name=record_name))
        try:
            proc.kill()
            _ = proc.wait(timeout=10)
        except Exception as e:
            logger.warning(
                i18n.tr(
                    "[{record_name}] 强制终止 ffmpeg 失败: {type_name}: {e}",
                    record_name=record_name,
                    type_name=type(e).__name__,
                    e=e,
                )
            )
    return_code = proc.returncode
    if return_code is None:
        logger.warning(i18n.tr("[{record_name}] 无法获取 ffmpeg 退出码，按失败处理", record_name=record_name))
        return_code = -1
    stop_time = time.strftime("%Y-%m-%d %H:%M:%S")
    if return_code == 0 or _reached_record_limit:
        _produced_bytes = _record_output_bytes(save_file_path, _split_output)
        if _produced_bytes is None:
            _produced_bytes = -1
        if 0 <= _produced_bytes < _MIN_VALID_RECORD_BYTES:
            logger.warning(
                i18n.tr(
                    "[{record_name}] ffmpeg 退出码为 0 但产物为空（{size} 字节），按失败处理",
                    record_name=record_name,
                    size=_produced_bytes,
                )
            )
            record_error(host_of(record_url))
            with recording_state.record_state_lock:
                recording_state.recording.discard(record_name)
                recording_state.recording_time_list.pop(record_name, None)
            unregister_ffmpeg_process(proc)
            return False
        if settings.converts_to_mp4 and save_type == "TS":
            if _split_output:
                _ts_segments = _segment_files(save_file_path)
                if _ts_segments is None:
                    logger.warning(
                        i18n.tr(
                            "分段目录不可读，跳过转换: {directory}",
                            directory=utils.mask_credentials(os.path.dirname(save_file_path)),
                        )
                    )
                for ts_path in _ts_segments or ():
                    _submit_postprocess(converts_mp4, str(ts_path), settings.delete_origin_file, record_name)
            else:
                _submit_postprocess(converts_mp4, save_file_path, settings.delete_origin_file, record_name)
        print(i18n.tr("\n{record_name} {stop_time} 直播录制完成\n", record_name=record_name, stop_time=stop_time))

        if script_command:
            logger.debug("开始执行脚本命令!")
            if "python" in script_command:
                params = [
                    f'--record_name "{record_name}"',
                    f'--save_file_path "{save_file_path}"',
                    f"--save_type {save_type}",
                    f"--split_video_by_time {_split_output}",
                    f"--converts_to_mp4 {settings.converts_to_mp4}",
                ]
            else:
                params = [
                    f'"{record_name.split(" ", maxsplit=1)[-1]}"',
                    f'"{save_file_path}"',
                    save_type,
                    f"split_video_by_time:{_split_output}",
                    f"converts_to_mp4:{settings.converts_to_mp4}",
                ]
            script_command = script_command.strip() + " " + " ".join(params)
            run_script(script_command)
            logger.debug("脚本命令执行结束!")
        record_success(host_of(record_url))
        try:
            clear_ffmpeg_reject(ffmpeg_command[ffmpeg_command.index("-i") + 1], platform)
        except ValueError:
            logger.debug(
                i18n.tr("[{record_name}] ffmpeg 命令缺少 -i 输入参数，跳过探针退避清除", record_name=record_name)
            )

    else:
        console.color_obj.print_colored(
            i18n.tr(
                "\n{record_name} {stop_time} 直播录制出错,返回码: {return_code}\n",
                record_name=record_name,
                stop_time=stop_time,
                return_code=_describe_return_code(return_code),
            ),
            console.color_obj.RED,
        )
        record_error(host_of(record_url))
        _output_side_failure = not os.path.isdir(os.path.dirname(save_file_path) or ".")
        if time.monotonic() - _proc_started_mono <= _FFMPEG_FAST_FAIL_SECONDS and not _output_side_failure:
            try:
                _stream_url = ffmpeg_command[ffmpeg_command.index("-i") + 1]
                mark_ffmpeg_reject(_stream_url, platform)
            except ValueError:
                logger.debug(
                    i18n.tr("[{record_name}] ffmpeg 命令缺少 -i 输入参数，跳过探针退避标记", record_name=record_name)
                )

    with recording_state.record_state_lock:
        recording_state.recording.discard(record_name)
        recording_state.recording_time_list.pop(record_name, None)
    unregister_ffmpeg_process(proc)
    return False


def _build_ffmpeg_output_args(
    save_file_path: str,
    record_save_type: str,
    split_video_by_time: bool,
    split_time: str,
    is_audio: bool = False,
) -> list[str]:
    if is_audio:
        if "MP3" in record_save_type:
            if split_video_by_time:
                return [
                    "-map",
                    "0:a",
                    "-c:a",
                    "libmp3lame",
                    "-ab",
                    "320k",
                    "-f",
                    "segment",
                    "-segment_time",
                    split_time,
                    "-segment_format",
                    SEGMENT_FORMAT_BY_SUFFIX[".mp3"],
                    "-reset_timestamps",
                    "1",
                    save_file_path,
                ]
            return ["-map", "0:a", "-c:a", "libmp3lame", "-ab", "320k", save_file_path]
        if split_video_by_time:
            return [
                "-map",
                "0:a",
                "-c:a",
                "aac",
                "-bsf:a",
                "aac_adtstoasc",
                "-ab",
                "320k",
                "-f",
                "segment",
                "-segment_time",
                split_time,
                "-segment_format",
                SEGMENT_FORMAT_BY_SUFFIX[".m4a"],
                "-reset_timestamps",
                "1",
                save_file_path,
            ]
        return [
            "-map",
            "0:a",
            "-c:a",
            "aac",
            "-bsf:a",
            "aac_adtstoasc",
            "-ab",
            "320k",
            "-movflags",
            "+faststart",
            save_file_path,
        ]

    if record_save_type == "FLV":
        if split_video_by_time:
            return [
                "-map",
                "0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-f",
                "segment",
                "-segment_time",
                split_time,
                "-segment_format",
                SEGMENT_FORMAT_BY_SUFFIX[".flv"],
                "-reset_timestamps",
                "1",
                save_file_path,
            ]
        return [
            "-map",
            "0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-f",
            "flv",
            save_file_path,
        ]
    if record_save_type == "MKV":
        if split_video_by_time:
            return [
                "-flags",
                "global_header",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-map",
                "0",
                "-f",
                "segment",
                "-segment_time",
                split_time,
                "-segment_format",
                SEGMENT_FORMAT_BY_SUFFIX[".mkv"],
                "-reset_timestamps",
                "1",
                save_file_path,
            ]
        return [
            "-flags",
            "global_header",
            "-map",
            "0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-f",
            "matroska",
            save_file_path,
        ]
    if record_save_type == "MP4":
        if split_video_by_time:
            return [
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-map",
                "0",
                "-f",
                "segment",
                "-segment_time",
                split_time,
                "-segment_format",
                SEGMENT_FORMAT_BY_SUFFIX[".mp4"],
                "-reset_timestamps",
                "1",
                "-segment_format_options",
                "movflags=+frag_keyframe+empty_moov",
                save_file_path,
            ]
        return ["-map", "0", "-c:v", "copy", "-c:a", "copy", "-f", "mp4", save_file_path]

    if split_video_by_time:
        return [
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-map",
            "0",
            "-f",
            "segment",
            "-segment_time",
            split_time,
            "-segment_format",
            SEGMENT_FORMAT_BY_SUFFIX[".ts"],
            "-reset_timestamps",
            "1",
            save_file_path,
        ]
    return ["-c:v", "copy", "-c:a", "copy", "-map", "0", "-f", "mpegts", save_file_path]


_EXTENSION_BY_SAVE_TYPE: dict[str, str] = {"FLV": "flv", "MKV": "mkv", "MP4": "mp4"}
_SEGMENT_NOW_FORMAT_BY_SAVE_TYPE: dict[str, str] = {
    "FLV": "%y%m%d_%H%M%S",
    "MKV": "%Y-%m-%d_%H-%M-%S",
    "MP4": "%Y-%m-%d_%H-%M-%S",
    "TS": "%Y-%m-%d_%H-%M-%S",
}
_DEFAULT_SEGMENT_NOW_FORMAT = "%Y-%m-%d_%H-%M-%S"
_DIRECTORY_PLACEHOLDER = re.compile(r"\{[^{}]*\}|[{}]")


def _live_date(value: object, first_live_at: float | None, now: datetime.datetime) -> str:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        try:
            timestamp = float(value)
            if timestamp >= 10**12:
                timestamp /= 1000
            if 0 < timestamp <= now.timestamp():
                return datetime.datetime.fromtimestamp(timestamp).date().isoformat()
        except OSError, OverflowError, ValueError:
            pass
    return (datetime.datetime.fromtimestamp(first_live_at) if first_live_at is not None else now).date().isoformat()


def _render_record_directory(
    template: str, root: Path, observation: RoomObservation, now: datetime.datetime, live_title: str
) -> str:
    prefix, *parts = template.replace("\\", "/").rstrip("/").split("/")
    if prefix != "{record_dir}" or any(part in ("", ".", "..") for part in parts):
        raise ValueError

    info = observation.port_info or {}
    anchor_id = info.get("anchor_id")
    if type(anchor_id) not in (str, int) or not anchor_id or anchor_id == "0":
        digest = hashlib.sha256(observation.room_key.encode()).hexdigest()[:16]
        anchor_id = f"room-{digest}"
    values = {
        "{user_id}": str(anchor_id),
        "{username}": observation.anchor_name,
        "{live_date}": _live_date(info.get("live_started_at"), observation.first_live_at, now),
        "{live_title}": live_title or "无标题",
    }
    path = root
    for part in parts:
        name = clean_name(_DIRECTORY_PLACEHOLDER.sub(lambda match: values[match[0]], part))
        path /= f"{name}_" if ntpath.isreserved(name) else name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError
    return path.as_posix()


def _record_directory(observation: RoomObservation, now: datetime.datetime, live_title: str) -> str:
    root = (settings.video_save_path or config_paths.default_path).replace("\\", "/")
    template = settings.record_directory_template.strip()
    if template:
        try:
            return _render_record_directory(template, Path(root), observation, now, live_title)
        except KeyError, ValueError:
            logger.warning(i18n.tr("录制目录模板无效，已使用原有目录规则"))

    anchor_name = observation.anchor_name
    full_path = f"{root.rstrip('/')}/{observation.platform}"
    date_dir = now.strftime("%y%m%d")
    if settings.folder_by_author:
        full_path = f"{full_path}/{anchor_name}"
    if settings.folder_by_time:
        full_path = f"{full_path}/{date_dir}"
    if settings.folder_by_title and live_title:
        folder = f"{live_title}_{anchor_name}" if settings.folder_by_time else f"{date_dir}_{live_title}"
        full_path = f"{full_path}/{folder}"
    return full_path


def _sanitize_output_name_base(base: str) -> str:
    """输出文件名模板的 `%` 兜底清洗：segment muxer 把输出路径当 printf 模板，除末尾 `_%03d`/。"""
    if "%" not in base:
        return base
    cleaned = base.replace("%", "")
    logger.warning(i18n.tr("输出文件名模板含非法 %% 占位符，已剔除后继续录制: {name}", name=base))
    return cleaned


def _build_record_output_path(
    full_path: str,
    anchor_name: str,
    title_in_name: str,
    now: str,
    record_save_type: str,
    split_video_by_time: bool,
    is_audio: bool = False,
) -> str:
    """输出文件路径构造：音频走 .mp3/.m4a 与 _%02d/_00，视频按保存类型查表、未知值回落 ts。"""
    if is_audio:
        extension = "mp3" if "MP3" in record_save_type else "m4a"
        name_format = "_%02d" if split_video_by_time else "_00"
        base_name = _sanitize_output_name_base(f"{anchor_name}_{title_in_name}{now}")
        return f"{full_path}/{base_name}{name_format}.{extension}"

    extension = _EXTENSION_BY_SAVE_TYPE.get(record_save_type, "ts")
    if split_video_by_time:
        seg_now = time.strftime(
            _SEGMENT_NOW_FORMAT_BY_SAVE_TYPE.get(record_save_type, _DEFAULT_SEGMENT_NOW_FORMAT), time.localtime()
        )
        base_name = _sanitize_output_name_base(f"{anchor_name}_{title_in_name}{seg_now}")
        return f"{full_path}/{base_name}_%03d.{extension}"
    plain_suffix = "_00" if record_save_type == "FLV" else ""
    base_name = _sanitize_output_name_base(f"{anchor_name}_{title_in_name}{now}")
    return f"{full_path}/{base_name}{plain_suffix}.{extension}"


def _ffmpeg_network_tuning(is_overseas: bool) -> dict[str, str]:
    """海外平台 CDN 往返延迟高、GOP 探测更慢，统一放大超时与探测窗口，避免握手阶段超时退出。"""
    if is_overseas:
        return {
            "rw_timeout": "50000000",
            "analyzeduration": "40000000",
            "probesize": "20000000",
            "bufsize": "15000k",
            "max_muxing_queue_size": "2048",
        }
    return {
        "rw_timeout": "15000000",
        "analyzeduration": "20000000",
        "probesize": "10000000",
        "bufsize": "8000k",
        "max_muxing_queue_size": "1024",
    }


def _build_ffmpeg_input_args(
    real_url: str,
    user_agent: str,
    tuning: dict[str, str],
    headers: dict[str, str] | None = None,
    tls_verify: bool = True,
    proxy_address: str | None = None,
) -> list[str]:
    """ffmpeg 输入侧参数构造：把 -reconnect* / -headers / -tls_verify / -http_proxy 收敛为纯函数。"""
    command = [
        "ffmpeg",
        "-y",
        "-rw_timeout",
        tuning["rw_timeout"],
        "-loglevel",
        "error",
        "-hide_banner",
        "-user_agent",
        user_agent,
        "-protocol_whitelist",
        "rtmp,crypto,http,https,tcp,tls,udp,rtp,httpproxy",
        "-analyzeduration",
        tuning["analyzeduration"],
        "-probesize",
        tuning["probesize"],
        "-fflags",
        "+discardcorrupt",
        "-reconnect_delay_max",
        "60",
        "-reconnect_streamed",
        "1",
        "-reconnect_at_eof",
        "1",
        # 直播输入不加速率钳制：-re 按时间戳节流，HLS 断点/回跳时会让 ffmpeg 空等，
        # 表现为「进程还在、输出文件不再增长」（2026-11-03 实测 50s 只落 34.6s）。
        "-i",
        real_url,
        "-bufsize",
        tuning["bufsize"],
        "-sn",
        "-dn",
        "-max_muxing_queue_size",
        tuning["max_muxing_queue_size"],
        "-correct_ts_overflow",
        "1",
        "-avoid_negative_ts",
        "1",
    ]

    if _stream_path_suffix(real_url) == ".m3u8":
        _eof_idx = command.index("-reconnect_at_eof")
        del command[_eof_idx : _eof_idx + 2]

    if headers:
        header_blob = "\r\n".join(f"{k}:{v}" for k, v in headers.items())
        _i_idx = command.index("-i")
        command[_i_idx:_i_idx] = ["-headers", header_blob]

    if not tls_verify and (real_url or "").startswith("https://"):
        command.insert(1, "-tls_verify")
        command.insert(2, "0")

    if proxy_address:
        command.insert(1, "-http_proxy")
        command.insert(2, proxy_address)

    return command


def _run_ffmpeg_record(
    record_name: str,
    record_url: str,
    record_host: str,
    ffmpeg_command: list[str],
    record_save_type: str,
    custom_script: str | None,
    platform: str,
    record_danmaku_args: dict[str, Any] | None,
) -> tuple[bool, bool]:
    """五条保存类型分支共用的「起一次 ffmpeg 录制」执行骨架（原为五份复制粘贴）。"""
    try:
        comment_end = check_subprocess(
            record_name,
            record_url,
            ffmpeg_command,
            record_save_type,
            custom_script,
            platform=platform,
            danmaku_args=record_danmaku_args,
        )
    except OSError as e:
        logger.error(
            i18n.tr(
                "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                type_name=type(e).__name__,
                e=e,
                get_error_line=get_error_line(e),
            )
        )
        with recording_state.record_state_lock:
            recording_state.recording.discard(record_name)
            recording_state.recording_time_list.pop(record_name, None)
        record_error(record_host)
        return False, False
    return True, comment_end


def _convert_after_record(save_file_path: str, split_video_by_time: bool) -> None:
    """录后转 MP4 统一出口：关闭转码直接返回；分段按「前缀 + _<数字序号>.<ext>」精确匹配；。"""
    if not settings.converts_to_mp4:
        return
    try:
        if not split_video_by_time:
            _submit_postprocess(converts_mp4, save_file_path, settings.delete_origin_file)
            return
        extension = os.path.basename(save_file_path).rsplit(".", maxsplit=1)[-1]
        _seg_paths = _segment_files(save_file_path)
        if _seg_paths is None:
            logger.warning(
                i18n.tr(
                    "分段目录不可读，跳过转换: {directory}",
                    directory=utils.mask_credentials(os.path.dirname(save_file_path)),
                )
            )
            return
        seg_files = sorted(_seg_paths)
        if not seg_files:
            logger.warning(
                i18n.tr(
                    "未找到分段文件（{extension}），跳过转换: {seg_pattern}",
                    extension=extension,
                    seg_pattern=f"{os.path.basename(save_file_path).rsplit('_', maxsplit=1)[0]}_*.{extension}",
                )
            )
        for seg_file in seg_files:
            _submit_postprocess(converts_mp4, str(seg_file), settings.delete_origin_file)
    except Exception as e:
        logger.error(i18n.tr("转码失败: {type_name}: {e} ", type_name=type(e).__name__, e=e))


class RecorderSession:
    """一次录制尝试的执行体：路径、FFmpeg/直下、录后处理。"""

    def record_once(self, plan: RecordPlan) -> RecordResult:
        observation = plan.observation
        record_url = observation.url
        record_name = plan.record_name
        record_host = host_of(record_url)
        record_quality = plan.quality_code
        record_quality_zh = observation.quality
        anchor_name = observation.anchor_name
        platform = observation.platform
        port_info = cast(dict[str, Any], observation.port_info or {})
        record_danmaku_args = cast(dict[str, Any] | None, observation.danmaku_args)
        real_url = plan.source_url
        proxy_address = plan.proxy_address
        platform_cookie = plan.cookies
        record_finished = False

        recorded_at = datetime.datetime.today()
        now = recorded_at.strftime("%y%m%d_%H%M%S")
        title = port_info.get("title") or observation.title
        live_title = clean_name(title) if isinstance(title, str) and title else ""
        title_in_name = live_title + "_" if live_title and settings.filename_by_title else ""

        try:
            full_path = _record_directory(observation, recorded_at, live_title)
            os.makedirs(full_path, exist_ok=True)
        except Exception as e:
            logger.error(
                i18n.tr(
                    "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                    type_name=type(e).__name__,
                    e=e,
                    get_error_line=get_error_line(e),
                )
            )
            return RecordResult(retry=True)

        if platform not in ("自定义录制直播", "虎牙直播"):
            if settings.enable_https_recording and real_url.startswith("http://"):
                real_url = real_url.replace("http://", "https://")
            elif not settings.enable_https_recording and real_url.startswith("https://"):
                if not any(pt_host in record_url for pt_host in OVERSEAS_PLATFORM_HOST):
                    real_url = real_url.replace("https://", "http://")

            http_record_list = ["shopee", "咪咕直播"]
            if platform in http_record_list:
                real_url = real_url.replace("https://", "http://")

        is_overseas = any(pt_host in record_url for pt_host in OVERSEAS_PLATFORM_HOST)
        tuning = _ffmpeg_network_tuning(is_overseas)
        ffmpeg_command = _build_ffmpeg_input_args(
            real_url,
            plan.user_agent,
            tuning,
            headers=dict(plan.headers) if plan.headers is not None else None,
            tls_verify=policy.should_verify_stream_tls(platform),
            proxy_address=proxy_address,
        )

        only_flv_record = False
        only_flv_platform_list = ["shopee", "花椒直播"]
        if platform in only_flv_platform_list:
            logger.debug(i18n.tr("提示: {platform} 将强制使用FLV格式录制", platform=platform))
            only_flv_record = True

        if not only_flv_record:
            with recording_state.record_state_lock:
                recording_state.recording.add(record_name)
                start_record_time = datetime.datetime.now()
                actual_quality_value = port_info.get("actual_quality")
                actual_quality_code: str | None = (
                    actual_quality_value if isinstance(actual_quality_value, str) else None
                )
                actual_quality_zh = code_to_zh(actual_quality_code) if actual_quality_code else ""
                if actual_quality_code and _is_downgrade(record_quality, actual_quality_code):
                    logger.warning(
                        i18n.tr(
                            "{record_name} 画质降级：设置 {record_quality_zh}({record_quality})"
                            " 实际 {actual_quality_zh}({actual_quality_code})",
                            record_name=record_name,
                            record_quality_zh=record_quality_zh,
                            record_quality=record_quality,
                            actual_quality_zh=actual_quality_zh,
                            actual_quality_code=actual_quality_code,
                        )
                    )
                recording_state.recording_time_list[record_name] = [
                    start_record_time,
                    record_quality_zh,
                    actual_quality_zh,
                ]
        rec_info_anchor_name = anchor_name
        rec_info_full_path = full_path
        if settings.show_url:
            re_plat = ("WinkTV", "PandaTV", "ShowRoom", "CHZZK", "YouTube")
            if platform in re_plat:
                logger.info(
                    i18n.tr(
                        "{platform} | {anchor_name} | 直播源地址: {m3u8_url}",
                        platform=platform,
                        anchor_name=anchor_name,
                        m3u8_url=utils.mask_credentials(str(port_info.get("m3u8_url") or "")),
                    )
                )
            else:
                logger.info(
                    i18n.tr(
                        "{platform} | {anchor_name} | 直播源地址: {real_url}",
                        platform=platform,
                        anchor_name=anchor_name,
                        real_url=utils.mask_credentials(real_url),
                    )
                )

        only_audio_record = False
        only_audio_platform_list = ["猫耳FM直播", "Look直播"]
        if platform in only_audio_platform_list:
            only_audio_record = True

        record_save_type = settings.video_save_type

        if real_url == port_info.get("flv_url") and port_info.get("flv_url"):
            codec = network_urls.query_values(cast(str, port_info["flv_url"]), "codec")
            if (
                isinstance(codec, list)
                and bool(codec)
                and codec[0] == "h265"
                and record_save_type.upper() not in ("TS", "MKV", "MP4")
            ):
                logger.warning(i18n.tr("FLV is not supported for h265 codec, use TS format instead"))
                record_save_type = "TS"

        if only_audio_record or any(i in record_save_type for i in ["MP3", "M4A"]):
            save_file_path = _build_record_output_path(
                full_path,
                anchor_name,
                title_in_name,
                now,
                record_save_type,
                settings.split_video_by_time,
                is_audio=True,
            )
            if settings.split_video_by_time:
                print(
                    i18n.tr(
                        "\r{anchor_name} 准备开始录制音频: {save_file_path}",
                        anchor_name=anchor_name,
                        save_file_path=save_file_path,
                    )
                )
            ffmpeg_command.extend(
                _build_ffmpeg_output_args(
                    save_file_path,
                    record_save_type,
                    settings.split_video_by_time,
                    settings.split_time,
                    is_audio=True,
                )
            )
            started, comment_end = _run_ffmpeg_record(
                record_name,
                record_url,
                record_host,
                ffmpeg_command,
                record_save_type,
                settings.custom_script,
                platform,
                record_danmaku_args,
            )
            if started:
                record_finished = True
            if comment_end:
                return RecordResult(started=record_finished, comment_end=True)

        elif only_flv_record:
            logger.info(
                i18n.tr(
                    "Use Direct Downloader to Download FLV Stream: {record_url}",
                    record_url=utils.mask_credentials(record_url),
                )
            )
            filename = f"{anchor_name}_{title_in_name}{now}_00.flv"
            save_file_path = f"{full_path}/{filename}"
            print(
                i18n.tr(
                    "\r{anchor_name} 准备开始录制视频: {full_path}/{filename}",
                    anchor_name=rec_info_anchor_name,
                    full_path=rec_info_full_path,
                    filename=filename,
                )
            )

            subs_file_path = save_file_path.rsplit(".", maxsplit=1)[0]
            subs_thread_name = f"subs_{Path(subs_file_path).name}"

            try:
                flv_url = port_info.get("flv_url")
                if isinstance(flv_url, str) and flv_url:
                    with recording_state.record_state_lock:
                        recording_state.recording.add(record_name)
                        start_record_time = datetime.datetime.now()
                        actual_quality_value = port_info.get("actual_quality")
                        actual_quality_code = actual_quality_value if isinstance(actual_quality_value, str) else None
                        actual_quality_zh = code_to_zh(actual_quality_code) if actual_quality_code else ""
                        if actual_quality_code and _is_downgrade(record_quality, actual_quality_code):
                            logger.warning(
                                i18n.tr(
                                    "{record_name} 画质降级：设置 {record_quality_zh}({record_quality})"
                                    " 实际 {actual_quality_zh}({actual_quality_code})",
                                    record_name=record_name,
                                    record_quality_zh=record_quality_zh,
                                    record_quality=record_quality,
                                    actual_quality_zh=actual_quality_zh,
                                    actual_quality_code=actual_quality_code,
                                )
                            )
                        recording_state.recording_time_list[record_name] = [
                            start_record_time,
                            record_quality_zh,
                            actual_quality_zh,
                        ]

                    if settings.create_time_file:
                        with recording_state.record_state_lock:
                            recording_state.create_var[subs_thread_name] = threading.Thread(
                                target=_generate_time_subtitles,
                                args=(record_name, subs_file_path, subs_thread_name),
                                name=subs_thread_name,
                                daemon=True,
                            )
                            try:
                                recording_state.create_var[subs_thread_name].start()
                            except RuntimeError:
                                recording_state.create_var.pop(subs_thread_name, None)

                    download_success = direct_download_stream(
                        flv_url,
                        save_file_path,
                        record_name,
                        record_url,
                        platform,
                        cookies=platform_cookie,
                    )

                    if download_success:
                        record_finished = True
                        print(
                            i18n.tr(
                                "\n{anchor_name} {strftime} 直播录制完成\n",
                                anchor_name=anchor_name,
                                strftime=time.strftime("%Y-%m-%d %H:%M:%S"),
                            )
                        )
                        record_success(record_host)
                    elif (
                        record_url not in recording_state.url_comments
                        and not recording_state.exit_recording
                        and recording_state.recording_enabled
                    ):
                        record_error(record_host)

                    clear_record_info(record_name, record_url)
                else:
                    logger.debug(i18n.tr("未找到FLV直播流，跳过录制"))
                    clear_record_info(record_name, record_url)
            except Exception as e:
                clear_record_info(record_name, record_url)
                console.color_obj.print_colored(
                    i18n.tr(
                        "\n{anchor_name} {strftime} 直播录制出错,请检查网络\n",
                        anchor_name=anchor_name,
                        strftime=time.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                    console.color_obj.RED,
                )
                logger.error(
                    i18n.tr(
                        "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                        type_name=type(e).__name__,
                        e=e,
                        get_error_line=get_error_line(e),
                    )
                )
                record_error(record_host)

        elif record_save_type in ("FLV", "MKV", "MP4"):
            save_file_path = _build_record_output_path(
                full_path,
                anchor_name,
                title_in_name,
                now,
                record_save_type,
                settings.split_video_by_time,
            )
            print(
                i18n.tr(
                    "\r{anchor_name} 准备开始录制视频: {full_path}/{filename}",
                    anchor_name=rec_info_anchor_name,
                    full_path=rec_info_full_path,
                    filename=os.path.basename(save_file_path),
                )
            )
            ffmpeg_command.extend(
                _build_ffmpeg_output_args(
                    save_file_path, record_save_type, settings.split_video_by_time, settings.split_time
                )
            )
            started, comment_end = _run_ffmpeg_record(
                record_name,
                record_url,
                record_host,
                ffmpeg_command,
                record_save_type,
                settings.custom_script,
                platform,
                record_danmaku_args,
            )
            if started:
                record_finished = True
            _convert_if_flv = started and record_save_type == "FLV"
            if comment_end:
                if _convert_if_flv:
                    _convert_after_record(save_file_path, settings.split_video_by_time)
                return RecordResult(started=record_finished, comment_end=True)
            if _convert_if_flv:
                _convert_after_record(save_file_path, settings.split_video_by_time)

        else:
            save_file_path = _build_record_output_path(
                full_path,
                anchor_name,
                title_in_name,
                now,
                record_save_type,
                settings.split_video_by_time,
            )
            print(
                i18n.tr(
                    "\r{anchor_name} 准备开始录制视频: {full_path}/{filename}",
                    anchor_name=rec_info_anchor_name,
                    full_path=rec_info_full_path,
                    filename=os.path.basename(save_file_path),
                )
            )
            ffmpeg_command.extend(
                _build_ffmpeg_output_args(
                    save_file_path, record_save_type, settings.split_video_by_time, settings.split_time
                )
            )
            started, comment_end = _run_ffmpeg_record(
                record_name,
                record_url,
                record_host,
                ffmpeg_command,
                record_save_type,
                settings.custom_script,
                platform,
                record_danmaku_args,
            )
            if started:
                record_finished = True
            if comment_end:
                _convert_after_record(save_file_path, settings.split_video_by_time)
                return RecordResult(started=record_finished, comment_end=True)

        return RecordResult(started=record_finished)
