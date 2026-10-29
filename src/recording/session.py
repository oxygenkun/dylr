"""单房间录制、FFmpeg 命令与进程守护、录后处理。"""

import datetime
import os
import random
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

from src import get_danmaku_collector
from src.app.graceful_shutdown import shutdown_manager
from src.config.io import update_anchor_name
from src.monitors.registry import follow_monitor
from src.network import policy
from src.network import urls as network_urls
from src.notifications import msg_push
from src.notifications.notify import clear_record_info, push_message, record_error, record_success, run_script
from src.recording import state
from src.recording.postprocess import converts_mp4, generate_subtitles, get_startup_info
from src.recording.process import (
    _get_error_line,
    _terminate_ffmpeg_process,
    register_ffmpeg_process,
    unregister_ffmpeg_process,
)
from src.recording.scheduler import host_of
from src.streams.dispatch import OVERSEAS_PLATFORM_HOST, _resolve_platform_stream, _stream_path_suffix
from src.streams.selection import (
    MOBILE_UA,
    clean_name,
    clear_ffmpeg_reject,
    get_quality_code,
    get_record_headers,
    get_record_user_agent,
    mark_ffmpeg_reject,
    select_source_url,
)
from src.support import i18n, utils
from src.support.logger import set_room_context


def _interruptible_sleep(seconds: float, record_url: str | None = None) -> bool:
    """可中断等待：把一次性 time.sleep(n) 收敛为逐秒 tick，使「退出 / 停止录制 / URL 被注释」。"""
    _remaining = int(seconds) if seconds > 0 else 0
    while _remaining > 0:
        if state.exit_recording or not state.recording_enabled:
            return True
        if record_url is not None and record_url in state.url_comments:
            return True
        time.sleep(1)
        _remaining -= 1
    return False


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

            with httpx.Client(timeout=15, verify=policy.should_verify_stream_tls(platform)) as client:
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
                        if live_url in state.url_comments or state.exit_recording or not state.recording_enabled:
                            state.color_obj.print_colored(
                                i18n.tr("[{record_name}]录制时已被注释或停止录制,下载中断", record_name=record_name),
                                state.color_obj.YELLOW,
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
                get_error_line=_get_error_line(e),
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

_MAX_RECORD_SECONDS = 6 * 60 * 60  # 「单次录制时长上限(秒)」的配置默认值
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
        with state.record_state_lock:
            if state.create_var.get(thread_name) is threading.current_thread():
                state.create_var.pop(thread_name, None)


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
    _rec_sem = state.recording_semaphore
    while not _rec_sem.acquire(timeout=_SEM_WAIT_TICK):
        if record_url in state.url_comments or state.exit_recording or not state.recording_enabled:
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
                startupinfo=get_startup_info(state.os_type),
                start_new_session=(os.name != "nt"),
            )
            shutdown_manager.track(process)

        register_ffmpeg_process(process)

        subs_file_path = save_file_path.rsplit(".", maxsplit=1)[0]

        for _seg_placeholder in ("_%02d", "_%03d"):
            subs_file_path = subs_file_path.replace(_seg_placeholder, "")

        _danmaku_active = state.enable_danmaku or state.enable_danmaku_monitor
        if _danmaku_active and platform is not None and platform in state.danmaku_platforms and "音频" not in save_type:
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
                    danmaku_collector = get_danmaku_collector(
                        platform=platform,
                        danmaku_args=danmaku_args,
                        base_filename=subs_file_path,
                        segment_seconds=state.danmaku_split_time if _split_output else None,
                        room_name=record_name,
                        write_srt=state.enable_danmaku,
                    )
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
        if state.create_time_file and not _split_output and "音频" not in save_type:
            with state.record_state_lock:
                state.create_var[subs_thread_name] = threading.Thread(
                    target=_generate_time_subtitles,
                    args=(record_name, subs_file_path, subs_thread_name),
                    name=subs_thread_name,
                    daemon=True,
                )
                try:
                    state.create_var[subs_thread_name].start()
                except RuntimeError:
                    state.create_var.pop(subs_thread_name, None)
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

        _record_limit_seconds = state.max_record_seconds

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
            if record_url in state.url_comments or state.exit_recording or not state.recording_enabled:
                state.color_obj.print_colored(
                    i18n.tr("[{record_name}]录制时已被注释或停止录制,本条线程将会退出", record_name=record_name),
                    state.color_obj.YELLOW,
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
            with state.record_state_lock:
                state.recording.discard(record_name)
                state.recording_time_list.pop(record_name, None)
            unregister_ffmpeg_process(proc)
            return False
        if state.converts_to_mp4 and save_type == "TS":
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
                    _submit_postprocess(converts_mp4, str(ts_path), state.delete_origin_file, record_name)
            else:
                _submit_postprocess(converts_mp4, save_file_path, state.delete_origin_file, record_name)
        print(i18n.tr("\n{record_name} {stop_time} 直播录制完成\n", record_name=record_name, stop_time=stop_time))

        if script_command:
            logger.debug("开始执行脚本命令!")
            if "python" in script_command:
                params = [
                    f'--record_name "{record_name}"',
                    f'--save_file_path "{save_file_path}"',
                    f"--save_type {save_type}",
                    f"--split_video_by_time {_split_output}",
                    f"--converts_to_mp4 {state.converts_to_mp4}",
                ]
            else:
                params = [
                    f'"{record_name.split(" ", maxsplit=1)[-1]}"',
                    f'"{save_file_path}"',
                    save_type,
                    f"split_video_by_time:{_split_output}",
                    f"converts_to_mp4:{state.converts_to_mp4}",
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
        state.color_obj.print_colored(
            i18n.tr(
                "\n{record_name} {stop_time} 直播录制出错,返回码: {return_code}\n",
                record_name=record_name,
                stop_time=stop_time,
                return_code=_describe_return_code(return_code),
            ),
            state.color_obj.RED,
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

    with state.record_state_lock:
        state.recording.discard(record_name)
        state.recording_time_list.pop(record_name, None)
    unregister_ffmpeg_process(proc)
    return False


def rename_anchor_directory(old_name: str, new_name: str, platform: str) -> bool:
    """主播名变更后同步文件系统：把 {保存路径}/{platform}/{old_name} 改名为 new_name（目标已存在。"""
    if not old_name or not new_name or old_name == new_name:
        return True
    try:
        save_root = state.video_save_path or state.default_path
        platform_dir = os.path.join(save_root, platform)
        if not os.path.isdir(platform_dir):
            return True
        old_dir = os.path.join(platform_dir, old_name)
        new_dir = os.path.join(platform_dir, new_name)
        if os.path.isdir(old_dir):
            if os.path.isdir(new_dir):
                _merge_anchor_directory(old_dir, new_dir)
            else:
                os.rename(old_dir, new_dir)
        _rename_prefixed_entries(platform_dir, old_name, new_name)
        return True
    except OSError as e:
        logger.warning(
            i18n.tr(
                "重命名主播目录失败（下轮重试）: {old_name} -> {new_name}: {e}",
                old_name=old_name,
                new_name=new_name,
                e=e,
            )
        )
        return False


def _merge_anchor_directory(old_dir: str, new_dir: str) -> None:
    """把 old_dir 内全部条目移入 new_dir（同名冲突保留双方并告警）；全部移入且无残留时删除旧目录，。"""
    for entry in list(os.scandir(old_dir)):
        dst = os.path.join(new_dir, entry.name)
        if os.path.exists(dst):
            logger.warning(i18n.tr("合并主播目录时发现同名文件（双方保留，请手动整理）: {dst}", dst=dst))
            continue
        try:
            os.rename(entry.path, dst)
        except OSError as e:
            logger.warning(
                i18n.tr(
                    "合并主播目录条目失败（下轮重试）: {path} -> {dst}: {type_name}: {e}",
                    path=entry.path,
                    dst=dst,
                    type_name=type(e).__name__,
                    e=e,
                )
            )
    try:
        if not os.listdir(old_dir):
            os.rmdir(old_dir)
    except OSError as e:
        logger.warning(
            i18n.tr(
                "删除旧主播目录失败（已忽略）: {old_dir}: {type_name}: {e}",
                old_dir=old_dir,
                type_name=type(e).__name__,
                e=e,
            )
        )


def _rename_prefixed_entries(base_dir: str, old_name: str, new_name: str) -> None:
    """递归把 base_dir 下以 "{old_name}_" 开头的文件改名为 "{new_name}_" 前缀，。"""
    try:
        entries = list(os.scandir(base_dir))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                _rename_prefixed_entries(entry.path, old_name, new_name)
                dir_name = entry.name
                if dir_name != new_name and dir_name.endswith(f"_{old_name}"):
                    os.rename(entry.path, os.path.join(base_dir, dir_name[: -len(old_name)] + new_name))
            elif entry.name.startswith(f"{old_name}_"):
                os.rename(entry.path, os.path.join(base_dir, f"{new_name}_{entry.name[len(old_name) + 1 :]}"))
        except OSError as e:
            logger.warning(
                i18n.tr(
                    "主播名变更重命名失败（已跳过，不影响其余文件）: {path}: {type_name}: {e}",
                    path=entry.path,
                    type_name=type(e).__name__,
                    e=e,
                )
            )


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
        "-re",
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
                get_error_line=_get_error_line(e),
            )
        )
        with state.record_state_lock:
            state.recording.discard(record_name)
            state.recording_time_list.pop(record_name, None)
        record_error(record_host)
        return False, False
    return True, comment_end


def _convert_after_record(save_file_path: str, split_video_by_time: bool) -> None:
    """录后转 MP4 统一出口：关闭转码直接返回；分段按「前缀 + _<数字序号>.<ext>」精确匹配；。"""
    if not state.converts_to_mp4:
        return
    try:
        if not split_video_by_time:
            _submit_postprocess(converts_mp4, save_file_path, state.delete_origin_file)
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
            _submit_postprocess(converts_mp4, str(seg_file), state.delete_origin_file)
    except Exception as e:
        logger.error(i18n.tr("转码失败: {type_name}: {e} ", type_name=type(e).__name__, e=e))


def start_record(
    url_data: tuple[str, str, str], count_variable: int = -1, room_state: dict[str, str] | None = None
) -> None:
    """单个直播间的录制线程主体：循环「解析平台 → 选源 → ffmpeg/直下」，直到 URL 被注释、。"""
    set_room_context(f"序号{count_variable}")
    last_follow_revision = 0
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
            from src.streams.stream import code_to_zh
            from src.streams.stream import is_downgrade as _is_downgrade

            proxy_address = state.proxy_addr
            platform = "未知平台"

            if state.proxy_addr:
                proxy_address = None
                if state.enable_proxy_platform_list:
                    for _proxy_platform in state.enable_proxy_platform_list:
                        if _proxy_platform and _proxy_platform.strip() in record_url:
                            proxy_address = state.proxy_addr
                            break

            if not proxy_address and state.extra_enable_proxy_platform_list:
                for pt in state.extra_enable_proxy_platform_list:
                    if pt and pt.strip() in record_url:
                        proxy_address = state.proxy_addr_bak or None

            while True:
                record_danmaku_args = None
                if state.exit_recording:
                    logger.debug(
                        i18n.tr(
                            "检测到退出标志，录制线程退出: {record_url}",
                            record_url=utils.mask_credentials(record_url),
                        )
                    )
                    return
                if not state.recording_enabled:
                    logger.debug(
                        i18n.tr(
                            "录制已停止，房间线程退出: {record_url}",
                            record_url=utils.mask_credentials(record_url),
                        )
                    )
                    return
                if record_url in state.url_comments:
                    print(
                        i18n.tr(
                            "[{record_url}]已被注释,本条线程将会退出",
                            record_url=utils.mask_credentials(record_url),
                        )
                    )
                    clear_record_info(record_name, record_url)
                    return
                if state.scheduler is not None and not state.scheduler.allow(record_host):
                    _backoff = state.scheduler.backoff_seconds(record_host)
                    _backoff = min(_backoff, max(30.0, float(state.delay_default)))
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
                        _interruptible_sleep(max(30.0, float(state.delay_default)), record_url)
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
                        _interruptible_sleep(max(30.0, float(state.delay_default)), record_url)
                        continue
                    else:
                        record_success(record_host)
                        anchor_name = clean_name(anchor_name)

                        latest_anchor_name = clean_name(cast(str, port_info.get("anchor_name", "")))
                        if (
                            state.auto_update_anchor_name
                            and platform != "自定义录制直播"
                            and anchor_name
                            and latest_anchor_name
                            and latest_anchor_name != "空白昵称"
                            and anchor_name != latest_anchor_name
                        ):
                            if rename_anchor_directory(
                                anchor_name, latest_anchor_name, platform
                            ) and update_anchor_name(record_url, latest_anchor_name):
                                logger.info(
                                    i18n.tr(
                                        "主播名已变更，配置与录制文件同步更新: {anchor_name} -> {latest_anchor_name}",
                                        anchor_name=anchor_name,
                                        latest_anchor_name=latest_anchor_name,
                                    )
                                )
                                _stale_record_name = f"序号{count_variable} {anchor_name}"
                                anchor_name = latest_anchor_name
                                with state.record_state_lock:
                                    state.recording.discard(_stale_record_name)
                                    state.recording_time_list.pop(_stale_record_name, None)
                                try:
                                    from src.danmaku.monitor import get_hub

                                    get_hub().room_stopped(_stale_record_name, "主播改名")
                                except Exception:
                                    pass
                        record_name = f"序号{count_variable} {anchor_name}"
                        set_room_context(record_name)

                        if record_url in state.url_comments:
                            print(i18n.tr("[{anchor_name}]已被注释,本条线程将会退出", anchor_name=anchor_name))
                            clear_record_info(record_name, record_url)
                            return

                        if not url_data[-1] and not run_once:
                            if new_record_url:
                                state.need_update_line_list.append(
                                    f"{record_url}|{new_record_url},主播: {anchor_name.strip()}"
                                )
                                state.not_record_list.append(new_record_url)
                            else:
                                state.need_update_line_list.append(
                                    f"{record_url}|{record_url},主播: {anchor_name.strip()}"
                                )
                            run_once = True

                        push_at = datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")
                        if not port_info.get("is_live", False):
                            if len(state.recording) == 0:
                                print(i18n.tr("\r{record_name} 等待直播... ", record_name=record_name))

                            if start_pushed:
                                if state.over_show_push:
                                    push_content = msg_push.render_push_content(
                                        "直播间状态更新：{ROOM_NAME} 直播已结束！时间：{TIME}", record_name, push_at
                                    )
                                    if state.over_push_message_text:
                                        push_content = msg_push.render_push_template(
                                            state.over_push_message_text, record_name, push_at
                                        )

                                    threading.Thread(
                                        target=push_message,
                                        args=(record_name, record_url, push_content.replace(r"\n", "\n")),
                                        daemon=True,
                                    ).start()
                                start_pushed = False

                        else:
                            print(i18n.tr("\r{record_name} 正在直播中...", record_name=record_name))

                            if state.live_status_push and not start_pushed:
                                if state.begin_show_push:
                                    push_content = msg_push.render_push_content(
                                        "直播间状态更新：{ROOM_NAME} 正在直播中，时间：{TIME}", record_name, push_at
                                    )
                                    if state.begin_push_message_text:
                                        push_content = msg_push.render_push_template(
                                            state.begin_push_message_text, record_name, push_at
                                        )

                                    threading.Thread(
                                        target=push_message,
                                        args=(record_name, record_url, push_content.replace(r"\n", "\n")),
                                        daemon=True,
                                    ).start()
                                start_pushed = True

                            if state.disable_record:
                                _sleep_remaining = state.push_check_seconds
                                while _sleep_remaining > 0:
                                    if (
                                        not state.recording_enabled
                                        or state.exit_recording
                                        or record_url in state.url_comments
                                    ):
                                        break
                                    time.sleep(1)
                                    _sleep_remaining -= 1
                                continue

                            platform_cookie = {
                                "抖音直播": state.dy_cookie,
                                "TikTok直播": state.tiktok_cookie,
                                "快手直播": state.ks_cookie,
                                "虎牙直播": state.hy_cookie,
                                "斗鱼直播": state.douyu_cookie,
                                "YY直播": state.yy_cookie,
                                "B站直播": state.bili_cookie,
                                "小红书直播": state.xhs_cookie,
                                "bigo": state.bigo_cookie,
                                "blued": state.blued_cookie,
                                "SOOP(原AfreecaTV)": state.sooplive_cookie,
                                "网易CC直播": state.netease_cookie,
                                "千度热播": state.qiandurebo_cookie,
                                "PandaTV": state.pandatv_cookie,
                                "猫耳FM直播": state.maoerfm_cookie,
                                "WinkTV": state.winktv_cookie,
                                "TTingLive(原Flextv)": state.flextv_cookie,
                                "Look直播": state.look_cookie,
                                "TwitCasting": state.twitcasting_cookie,
                                "百度直播": state.baidu_cookie,
                                "微博直播": state.weibo_cookie,
                                "酷狗直播": state.kugou_cookie,
                                "LiveMe": state.liveme_cookie,
                                "shopee": state.shopee_cookie,
                                "花椒直播": state.huajiao_cookie,
                            }.get(platform, "")

                            real_url = select_source_url(port_info, proxy_address, platform, cookies=platform_cookie)
                            if not real_url:
                                logger.warning(
                                    i18n.tr("{anchor_name} 本轮未获取到可用流地址，跳过录制", anchor_name=anchor_name)
                                )
                                _interruptible_sleep(max(random.randint(-5, 5) + state.delay_default, 0), record_url)
                                continue
                            full_path = f"{state.default_path}/{platform}"
                            now = datetime.datetime.today().strftime("%y%m%d_%H%M%S")
                            date_dir = datetime.datetime.today().strftime("%y%m%d")
                            live_title = cast(str, port_info.get("title", ""))
                            title_in_name = ""
                            if live_title:
                                live_title = clean_name(live_title)
                                title_in_name = live_title + "_" if state.filename_by_title else ""

                            try:
                                if len(state.video_save_path) > 0:
                                    if not state.video_save_path.endswith(("/", "\\")):
                                        full_path = f"{state.video_save_path}/{platform}"
                                    else:
                                        full_path = f"{state.video_save_path}{platform}"

                                full_path = full_path.replace("\\", "/")
                                if state.folder_by_author:
                                    full_path = f"{full_path}/{anchor_name}"
                                if state.folder_by_time:
                                    full_path = f"{full_path}/{date_dir}"
                                if state.folder_by_title and port_info.get("title"):
                                    if state.folder_by_time:
                                        full_path = f"{full_path}/{live_title}_{anchor_name}"
                                    else:
                                        full_path = f"{full_path}/{date_dir}_{live_title}"
                                os.makedirs(full_path, exist_ok=True)
                            except Exception as e:
                                logger.error(
                                    i18n.tr(
                                        "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                                        type_name=type(e).__name__,
                                        e=e,
                                        get_error_line=_get_error_line(e),
                                    )
                                )
                                _interruptible_sleep(max(random.randint(-5, 5) + state.delay_default, 0), record_url)
                                continue

                            if platform not in ("自定义录制直播", "虎牙直播"):
                                if state.enable_https_recording and real_url.startswith("http://"):
                                    real_url = real_url.replace("http://", "https://")
                                elif not state.enable_https_recording and real_url.startswith("https://"):
                                    if not any(pt_host in record_url for pt_host in OVERSEAS_PLATFORM_HOST):
                                        real_url = real_url.replace("https://", "http://")

                                http_record_list = ["shopee", "咪咕直播"]
                                if platform in http_record_list:
                                    real_url = real_url.replace("https://", "http://")

                            user_agent = get_record_user_agent(platform) or MOBILE_UA

                            is_overseas = any(pt_host in record_url for pt_host in OVERSEAS_PLATFORM_HOST)
                            tuning = _ffmpeg_network_tuning(is_overseas)
                            headers = get_record_headers(platform, record_url, cookies=platform_cookie)
                            ffmpeg_command = _build_ffmpeg_input_args(
                                real_url,
                                user_agent,
                                tuning,
                                headers=headers,
                                tls_verify=policy.should_verify_stream_tls(platform),
                                proxy_address=proxy_address,
                            )

                            only_flv_record = False
                            only_flv_platform_list = ["shopee", "花椒直播"]
                            if platform in only_flv_platform_list:
                                logger.debug(i18n.tr("提示: {platform} 将强制使用FLV格式录制", platform=platform))
                                only_flv_record = True

                            if not only_flv_record:
                                with state.record_state_lock:
                                    state.recording.add(record_name)
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
                                    state.recording_time_list[record_name] = [
                                        start_record_time,
                                        record_quality_zh,
                                        actual_quality_zh,
                                    ]
                            rec_info_anchor_name = anchor_name
                            rec_info_full_path = full_path
                            if state.show_url:
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

                            record_save_type = state.video_save_type

                            if real_url == port_info.get("flv_url") and port_info.get("flv_url"):
                                codec = network_urls.query_values(cast(str, port_info["flv_url"]), "codec")
                                if (
                                    isinstance(codec, list)
                                    and bool(codec)
                                    and codec[0] == "h265"
                                    and record_save_type.upper() not in ("TS", "MKV", "MP4")
                                ):
                                    logger.warning(
                                        i18n.tr("FLV is not supported for h265 codec, use TS format instead")
                                    )
                                    record_save_type = "TS"

                            if only_audio_record or any(i in record_save_type for i in ["MP3", "M4A"]):
                                save_file_path = _build_record_output_path(
                                    full_path,
                                    anchor_name,
                                    title_in_name,
                                    now,
                                    record_save_type,
                                    state.split_video_by_time,
                                    is_audio=True,
                                )
                                if state.split_video_by_time:
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
                                        state.split_video_by_time,
                                        state.split_time,
                                        is_audio=True,
                                    )
                                )
                                started, comment_end = _run_ffmpeg_record(
                                    record_name,
                                    record_url,
                                    record_host,
                                    ffmpeg_command,
                                    record_save_type,
                                    state.custom_script,
                                    platform,
                                    record_danmaku_args,
                                )
                                if started:
                                    record_finished = True
                                if comment_end:
                                    return

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
                                        with state.record_state_lock:
                                            state.recording.add(record_name)
                                            start_record_time = datetime.datetime.now()
                                            actual_quality_value = port_info.get("actual_quality")
                                            actual_quality_code = (
                                                actual_quality_value if isinstance(actual_quality_value, str) else None
                                            )
                                            actual_quality_zh = (
                                                code_to_zh(actual_quality_code) if actual_quality_code else ""
                                            )
                                            if actual_quality_code and _is_downgrade(
                                                record_quality, actual_quality_code
                                            ):
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
                                            state.recording_time_list[record_name] = [
                                                start_record_time,
                                                record_quality_zh,
                                                actual_quality_zh,
                                            ]

                                        if state.create_time_file:
                                            with state.record_state_lock:
                                                state.create_var[subs_thread_name] = threading.Thread(
                                                    target=_generate_time_subtitles,
                                                    args=(record_name, subs_file_path, subs_thread_name),
                                                    name=subs_thread_name,
                                                    daemon=True,
                                                )
                                                try:
                                                    state.create_var[subs_thread_name].start()
                                                except RuntimeError:
                                                    state.create_var.pop(subs_thread_name, None)

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
                                            record_url not in state.url_comments
                                            and not state.exit_recording
                                            and state.recording_enabled
                                        ):
                                            record_error(record_host)

                                        clear_record_info(record_name, record_url)
                                    else:
                                        logger.debug(i18n.tr("未找到FLV直播流，跳过录制"))
                                        clear_record_info(record_name, record_url)
                                except Exception as e:
                                    clear_record_info(record_name, record_url)
                                    state.color_obj.print_colored(
                                        i18n.tr(
                                            "\n{anchor_name} {strftime} 直播录制出错,请检查网络\n",
                                            anchor_name=anchor_name,
                                            strftime=time.strftime("%Y-%m-%d %H:%M:%S"),
                                        ),
                                        state.color_obj.RED,
                                    )
                                    logger.error(
                                        i18n.tr(
                                            "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                                            type_name=type(e).__name__,
                                            e=e,
                                            get_error_line=_get_error_line(e),
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
                                    state.split_video_by_time,
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
                                        save_file_path, record_save_type, state.split_video_by_time, state.split_time
                                    )
                                )
                                started, comment_end = _run_ffmpeg_record(
                                    record_name,
                                    record_url,
                                    record_host,
                                    ffmpeg_command,
                                    record_save_type,
                                    state.custom_script,
                                    platform,
                                    record_danmaku_args,
                                )
                                if started:
                                    record_finished = True
                                _convert_if_flv = started and record_save_type == "FLV"
                                if comment_end:
                                    if _convert_if_flv:
                                        _convert_after_record(save_file_path, state.split_video_by_time)
                                    return
                                if _convert_if_flv:
                                    _convert_after_record(save_file_path, state.split_video_by_time)

                            else:
                                save_file_path = _build_record_output_path(
                                    full_path,
                                    anchor_name,
                                    title_in_name,
                                    now,
                                    record_save_type,
                                    state.split_video_by_time,
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
                                        save_file_path, record_save_type, state.split_video_by_time, state.split_time
                                    )
                                )
                                started, comment_end = _run_ffmpeg_record(
                                    record_name,
                                    record_url,
                                    record_host,
                                    ffmpeg_command,
                                    record_save_type,
                                    state.custom_script,
                                    platform,
                                    record_danmaku_args,
                                )
                                if started:
                                    record_finished = True
                                if comment_end:
                                    _convert_after_record(save_file_path, state.split_video_by_time)
                                    return

                            count_time = time.time()

                except Exception as e:
                    logger.error(
                        i18n.tr(
                            "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                            type_name=type(e).__name__,
                            e=e,
                            get_error_line=_get_error_line(e),
                        )
                    )
                    record_error(record_host)

                num = random.randint(-5, 5) + state.delay_default
                if num < 0:
                    num = 0
                x = num

                if sum(state.error_window) >= 5:
                    x = x + 60
                    state.color_obj.print_colored("\r瞬时错误太多,延迟加60秒", state.color_obj.YELLOW)

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
                    if record_url in state.url_comments or state.exit_recording or not state.recording_enabled:
                        break
                    x = x - 1
                    if state.loop_time:
                        print(i18n.tr("\r{anchor_name}循环等待{x}秒 ", anchor_name=anchor_name, x=x), end="")
                    time.sleep(1)
                if state.loop_time:
                    print("\r检测直播间中...", end="")
        except Exception as e:
            logger.error(
                i18n.tr(
                    "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                    type_name=type(e).__name__,
                    e=e,
                    get_error_line=_get_error_line(e),
                )
            )
            record_error(record_host)
            time.sleep(2)
        finally:
            if room_state is not None and record_name:
                room_state["name"] = record_name
