import datetime
import sys
import time
from typing import cast

from loguru import logger

from src.app import version as app_version
from src.config import paths as config_paths
from src.config import settings
from src.recording import state as recording_state
from src.support import i18n, utils
from src.support.errors import get_error_line


def get_status() -> dict[str, object]:
    """汇总运行状态快照（版本、监控数、正在录制列表及时长/画质、累计与窗口错误数、磁盘剩余、。"""
    now = datetime.datetime.now()
    recording_snapshot: list[str] = []
    recording_times: dict[str, dict[str, str]] = {}
    monitoring_val: int = recording_state.monitoring
    running_val: list[str] = []
    error_val: int = recording_state.error_count
    try:
        with recording_state.record_state_lock:
            recording_snapshot = list(recording_state.recording)
            recording_times = {}
            for _name, _info in recording_state.recording_time_list.items():
                if _info and len(_info) > 1:
                    _start = cast(datetime.datetime, _info[0])
                    _quality = str(_info[1])
                    _actual_q = str(_info[2]) if len(_info) > 2 else ""
                    recording_times[_name] = {
                        "start_time": _start.strftime("%Y-%m-%d %H:%M:%S"),
                        "quality": _quality,
                        "actual_quality": _actual_q,
                        "duration": str(now - _start).split(".")[0],
                    }
                else:
                    recording_times[_name] = {
                        "start_time": "",
                        "quality": "",
                        "actual_quality": "",
                        "duration": "0:00:00",
                    }
            monitoring_val = recording_state.monitoring
            running_val = list(recording_state.running_list)
            error_val = recording_state.error_count
    except (RuntimeError, IndexError) as e:
        logger.warning(
            i18n.tr("获取录制状态失败（疑似新增无锁写入路径），返回空快照: {type_name}", type_name=type(e).__name__)
        )
    with recording_state.max_request_lock:
        recent_errors_val = sum(recording_state.error_window)
    try:
        disk_free_gb = utils.check_disk_capacity(config_paths.default_path)
    except Exception:
        disk_free_gb = -1.0
    if recording_state._recorder_thread is None:
        engine_alive = True
    else:
        engine_alive = recording_state._recorder_thread.is_alive()
    _proc_start = recording_state.process_start_time
    uptime = str(now - _proc_start).split(".")[0] if _proc_start else "0:00:00"
    return {
        "version": app_version.version,
        "monitoring": monitoring_val,
        "recording_count": len(recording_snapshot),
        "recording": [
            {
                "name": _n,
                "start_time": recording_times.get(_n, {}).get("start_time", ""),
                "quality": recording_times.get(_n, {}).get("quality", ""),
                "actual_quality": recording_times.get(_n, {}).get("actual_quality", ""),
                "duration": recording_times.get(_n, {}).get("duration", "0:00:00"),
            }
            for _n in recording_snapshot
        ],
        "running_list": running_val,
        "error_count": error_val,  # 累计错误数（进程启动起单调递增）
        "recent_errors": recent_errors_val,  # 近 error_window_size 次检测周期内的错误数（瞬时口径）
        "disk_free_gb": round(disk_free_gb, 2),
        "uptime": uptime,
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "engine_alive": engine_alive,
        "recording_enabled": recording_state.recording_enabled,
    }


def _live_network_capacity() -> int:
    """当前网络并发容量：调度器就绪时取自适应容量（随活跃任务数缩放、错误率温和降容），。"""
    scheduler = recording_state.scheduler
    if scheduler is not None:
        return scheduler.network_semaphore.capacity
    return settings.max_request


_DISPLAY_INTERVAL_SECONDS = 5.0
_DISPLAY_BACKOFF_MAX_SECONDS = 300.0


def _display_delay(consecutive_failures: int) -> float:
    if consecutive_failures <= 0:
        return _DISPLAY_INTERVAL_SECONDS
    return min(_DISPLAY_INTERVAL_SECONDS * (2.0 ** min(consecutive_failures, 16)), _DISPLAY_BACKOFF_MAX_SECONDS)


def display_info() -> None:
    """守护线程主体：每 5 秒清屏并打印监控数/并发数/画质/格式/累计错误数及各房间已录时长；死循环不返回。"""
    time.sleep(_DISPLAY_INTERVAL_SECONDS)
    consecutive_failures = 0
    while True:
        if sys.stdout is None:
            time.sleep(_DISPLAY_INTERVAL_SECONDS)
            continue
        try:
            _ = sys.stdout.flush()
            if sys.stdout.isatty():
                _ = sys.stdout.write("\033[2J\033[H")
                _ = sys.stdout.flush()
            print(i18n.tr("\r共监测{monitoring}个直播中", monitoring=recording_state.monitoring), end=" | ")
            print(
                i18n.tr(
                    "同一时间访问网络的线程数: {live_network_capacity}", live_network_capacity=_live_network_capacity()
                ),
                end=" | ",
            )
            print(i18n.tr("是否开启代理录制: {use_proxy}", use_proxy="是" if settings.use_proxy else "否"), end=" | ")
            if settings.split_video_by_time:
                print(i18n.tr("录制分段开启: {split_time}秒", split_time=settings.split_time), end=" | ")
            else:
                print("录制分段开启: 否", end=" | ")
            if settings.create_time_file:
                print("是否生成时间文件: 是", end=" | ")
            print(
                i18n.tr("录制视频质量为: {video_record_quality}", video_record_quality=settings.video_record_quality),
                end=" | ",
            )
            print(i18n.tr("录制视频格式为: {video_save_type}", video_save_type=settings.video_save_type), end=" | ")
            print(i18n.tr("累计错误数为: {error_count}", error_count=recording_state.error_count), end=" | ")
            now = time.strftime("%H:%M:%S", time.localtime())
            print(i18n.tr("当前时间: {now}", now=now))

            if len(recording_state.recording) == 0:
                time.sleep(_DISPLAY_INTERVAL_SECONDS)
                if recording_state.monitoring == 0:
                    print("\r没有正在监测和录制的直播")
                else:
                    print(
                        i18n.tr(
                            "\r没有正在录制的直播 循环监测间隔时间：{delay_default}秒",
                            delay_default=settings.delay_default,
                        )
                    )
            else:
                now_time = datetime.datetime.now()
                print("x" * 60)
                with recording_state.record_state_lock:
                    no_repeat_recording = list(set(recording_state.recording))
                print(
                    i18n.tr(
                        "正在录制{no_repeat_recording_count}个直播: ",
                        no_repeat_recording_count=len(no_repeat_recording),
                    )
                )
                for recording_live in no_repeat_recording:
                    with recording_state.record_state_lock:
                        _rt_info = recording_state.recording_time_list.get(recording_live, [now_time, ""])
                    rt = cast(datetime.datetime, _rt_info[0]) if _rt_info else now_time
                    qa = str(_rt_info[1]) if len(_rt_info) > 1 else ""
                    have_record_time = now_time - rt
                    print(
                        i18n.tr(
                            "{recording_live}[{qa}] 正在录制中 {have_record_time}",
                            recording_live=recording_live,
                            qa=qa,
                            have_record_time=str(have_record_time).split(".")[0],
                        )
                    )

                print("x" * 60)
                recording_state.start_display_time = now_time
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error(
                i18n.tr(
                    "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                    type_name=type(e).__name__,
                    e=e,
                    get_error_line=get_error_line(e),
                )
            )
        time.sleep(_display_delay(consecutive_failures))
