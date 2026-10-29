import datetime
import os
import re
import subprocess
import sys
import time

from loguru import logger

from src.app.graceful_shutdown import ShutdownRequested, shutdown_manager
from src.recording import state
from src.support import i18n, utils

_REENCODE_SECONDS_PER_MB = 1.5
_REMUX_SECONDS_PER_MB = 0.04
_MAX_CONVERT_TIMEOUT = 3600
_MB = 1024 * 1024


def _convert_timeout(source_path: str, re_encoding: bool, explicit: int | None) -> int:
    """按源文件体积推算本次转码的超时上限；显式传入的 timeout 优先（调用方可强制）。"""
    if explicit is not None:
        return explicit
    try:
        size_mb = os.path.getsize(source_path) / _MB
    except OSError:
        size_mb = 0.0
    rate = _REENCODE_SECONDS_PER_MB if re_encoding else _REMUX_SECONDS_PER_MB
    return int(min(_MAX_CONVERT_TIMEOUT, max(600.0, size_mb * rate)))


def _discard_partial_output(out_path: str, pre_existed: bool, source_path: str) -> bool:
    """删除**本次调用**生成的不完整产物（MIN-04）。返回是否真的删掉了。"""
    if not out_path or pre_existed or not os.path.exists(out_path):
        return False
    try:
        os.remove(out_path)
    except OSError as e:
        logger.warning(
            i18n.tr(
                "[后处理]删除本次生成的不完整产物失败: {out_path} - {type_name}: {e}",
                out_path=out_path,
                type_name=type(e).__name__,
                e=e,
            )
        )
        return False
    logger.warning(
        i18n.tr(
            "[后处理]已删除本次生成的不完整产物，源文件已保留: {out_path} / {source_path}",
            out_path=out_path,
            source_path=source_path,
        )
    )
    return True


_SEGMENT_TEMPLATE_RE = re.compile(r"%0(\d+)d")


def _output_matcher(file_pattern: str) -> re.Pattern[str]:
    """把 ffmpeg 的输出参数（单文件路径或含 %0Nd 占位符的分段模板）转成匹配**文件名**的正则。"""
    parts: list[str] = []
    pos = 0
    for m in _SEGMENT_TEMPLATE_RE.finditer(file_pattern):
        parts.append(re.escape(file_pattern[pos : m.start()]))
        digits = int(m.group(1))
        parts.append(r"\d{" + str(digits) + ",}")
        pos = m.end()
    parts.append(re.escape(file_pattern[pos:]))
    return re.compile("".join(parts))


def _match_outputs(out_spec: str) -> list[str]:
    """列出磁盘上所有匹配 out_spec（模板已展开为真实序号）的产物完整路径；目录不可读时抛 OSError。"""
    directory, file_pattern = os.path.split(out_spec)
    matcher = _output_matcher(file_pattern)
    return [
        os.path.join(directory, entry.name) for entry in os.scandir(directory or ".") if matcher.fullmatch(entry.name)
    ]


def _snapshot_outputs(out_spec: str) -> set[str] | None:
    """为**本次调用**建立产物快照；返回 None 表示快照不可得（目录列不出来）。"""
    try:
        return set(_match_outputs(out_spec))
    except OSError as e:
        logger.warning(
            i18n.tr(
                "[后处理]无法读取产物目录，本次跳过半成品清理: {out_path} - {type_name}: {e}",
                out_path=utils.mask_credentials(os.path.dirname(out_spec) or "."),
                type_name=type(e).__name__,
                e=e,
            )
        )
        return None


def _discard_new_outputs(out_spec: str, pre_existed: set[str], source_path: str) -> None:
    """删除 out_spec 匹配到的、本次调用新增的产物（快照之后才出现的文件）。"""
    try:
        current = _match_outputs(out_spec)
    except OSError as e:
        logger.warning(
            i18n.tr(
                "[后处理]失败后无法读取产物目录，半成品未清理: {out_path} - {type_name}: {e}",
                out_path=utils.mask_credentials(os.path.dirname(out_spec) or "."),
                type_name=type(e).__name__,
                e=e,
            )
        )
        return
    for path in current:
        if path in pre_existed:
            continue
        _discard_partial_output(path, False, source_path)


def _run_postprocess_command(command: list[str], out_spec: str, source_path: str, timeout: int = 600) -> None:
    """执行一条后处理 ffmpeg 命令；任何非正常退出（失败/超时/取消）后清理本次新增的半成品产物。"""
    pre_existed = _snapshot_outputs(out_spec)
    try:
        _run_ffmpeg_checked(command, timeout=timeout)
    except BaseException:
        if pre_existed is not None:
            _discard_new_outputs(out_spec, pre_existed, source_path)
        raise


def get_startup_info(system_type: str) -> object | None:
    """Windows 下 subprocess.STARTUPINFO 仅存在于 Windows typeshed，Linux/macOS 上 mypy 无法解析该名字。"""
    if system_type != "nt":
        return None
    if sys.platform == "win32":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return startup_info
    return None


def _run_ffmpeg_checked(command: list[str], timeout: int = 600) -> str:
    """同步执行 ffmpeg 命令 command（超过 timeout 秒则杀掉子进程并抛 TimeoutExpired，避免转码卡死。"""
    with shutdown_manager.start_process(
        command,
        popen_factory=subprocess.Popen,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        startupinfo=get_startup_info(state.os_type),
    ) as process:
        try:
            out, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            shutdown_manager.stop_process(process)
            process.communicate()
            shutdown_manager.forget(process)
            raise
        shutdown_manager.forget(process)
        if shutdown_manager.stopping.is_set():
            raise ShutdownRequested("Conversion interrupted; retain original file")
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command, output=out)
        return out.decode("utf-8", errors="replace")


def segment_video(
    converts_file_path: str,
    segment_save_file_path: str,
    segment_format: str,
    segment_time: str,
    is_original_delete: bool = True,
) -> None:
    """用 ffmpeg 把 converts_file_path 按 segment_time 秒切分为 segment_format 容器，。"""
    try:
        if os.path.exists(converts_file_path) and os.path.getsize(converts_file_path) > 0:
            ffmpeg_command = [
                "ffmpeg",
                "-i",
                converts_file_path,
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-map",
                "0",
                "-f",
                "segment",
                "-segment_time",
                segment_time,
                "-segment_format",
                segment_format,
                "-reset_timestamps",
                "1",
                *(["-segment_format_options", "movflags=+frag_keyframe+empty_moov"] if segment_format == "mp4" else []),
                "-n",
                segment_save_file_path,
            ]
            _run_postprocess_command(ffmpeg_command, segment_save_file_path, converts_file_path)
            if is_original_delete and not shutdown_manager.stopping.is_set():
                time.sleep(1)
                if os.path.exists(converts_file_path):
                    os.remove(converts_file_path)
    except subprocess.TimeoutExpired as e:
        logger.error(i18n.tr("ffmpeg 转封装/转码超时（{type_name}）: {e}", type_name=type(e).__name__, e=e))
    except subprocess.CalledProcessError as e:
        logger.error(i18n.tr("Error occurred during conversion: {e}", e=e))
    except Exception as e:
        logger.error(i18n.tr("An unknown error occurred: {e}", e=e))


def converts_mp4(converts_file_path: str, is_original_delete: bool = True, timeout: int | None = None) -> None:
    """把 converts_file_path 转封装为同名 .mp4（全局 converts_to_h264 开启时重编码为 h264）；。"""
    out_path = ""
    try:
        if os.path.exists(converts_file_path) and os.path.getsize(converts_file_path) > 0:
            out_path = converts_file_path.rsplit(".", maxsplit=1)[0] + ".mp4"
            re_encoding = bool(state.converts_to_h264)
            if re_encoding:
                state.color_obj.print_colored("正在转码为MP4格式并重新编码为h264\n", state.color_obj.YELLOW)
                ffmpeg_command = [
                    "ffmpeg",
                    "-i",
                    converts_file_path,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    "-vf",
                    "format=yuv420p",
                    "-c:a",
                    "copy",
                    "-f",
                    "mp4",
                    "-n",
                    out_path,
                ]
            else:
                state.color_obj.print_colored("正在转码为MP4格式\n", state.color_obj.YELLOW)
                ffmpeg_command = [
                    "ffmpeg",
                    "-i",
                    converts_file_path,
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-f",
                    "mp4",
                    "-n",
                    out_path,
                ]
            _run_postprocess_command(
                ffmpeg_command,
                out_path,
                converts_file_path,
                timeout=_convert_timeout(converts_file_path, re_encoding, timeout),
            )
            if is_original_delete:
                time.sleep(1)
                if os.path.exists(converts_file_path):
                    os.remove(converts_file_path)
    except subprocess.TimeoutExpired as e:
        logger.error(i18n.tr("ffmpeg 转 MP4 超时（{type_name}）: {e}", type_name=type(e).__name__, e=e))
    except subprocess.CalledProcessError as e:
        logger.error(i18n.tr("Error occurred during conversion: {e}", e=e))
    except Exception as e:
        logger.error(i18n.tr("An unknown error occurred: {e}", e=e))


def converts_m4a(converts_file_path: str, is_original_delete: bool = True) -> None:
    """把 converts_file_path 抽取音轨转为同名 320k .m4a；is_original_delete=True 时删除源文件，无返回值。"""
    out_path = ""
    try:
        if os.path.exists(converts_file_path) and os.path.getsize(converts_file_path) > 0:
            out_path = converts_file_path.rsplit(".", maxsplit=1)[0] + ".m4a"
            m4a_command = [
                "ffmpeg",
                "-i",
                converts_file_path,
                "-n",
                "-vn",
                "-c:a",
                "aac",
                "-bsf:a",
                "aac_adtstoasc",
                "-ab",
                "320k",
                out_path,
            ]
            _run_postprocess_command(m4a_command, out_path, converts_file_path)
            if is_original_delete:
                time.sleep(1)
                if os.path.exists(converts_file_path):
                    os.remove(converts_file_path)
    except subprocess.TimeoutExpired as e:
        logger.error(i18n.tr("ffmpeg 抽音频超时（{type_name}）: {e}", type_name=type(e).__name__, e=e))
    except subprocess.CalledProcessError as e:
        logger.error(i18n.tr("Error occurred during conversion: {e}", e=e))
    except Exception as e:
        logger.error(i18n.tr("An unknown error occurred: {e}", e=e))


_SUB_WRITE_WARN_WINDOW = 60
_SUB_WRITE_RETRY_INTERVAL = 10


def generate_subtitles(record_name: str, ass_filename: str, sub_format: str = "srt") -> None:
    """后台逐秒追加写"时间字幕"：record_name 用于判断该房间是否仍在录制（不在则结束），。"""
    index_time = 0
    today = datetime.datetime.now()
    re_datetime = today.strftime("%Y-%m-%d %H:%M:%S")
    sub_out_path = f"{ass_filename}.{sub_format.lower()}"

    sub_next_warn_at = 0  # 初值 0 → 首次失败立刻可见，之后每 _SUB_WRITE_WARN_WINDOW 秒一条
    sub_next_retry_at = 0  # index_time < 该值时跳过 open()（退避中）
    sub_window_failures = 0  # 距上次告警以来丢弃的条数
    sub_total_failures = 0  # 本线程生命周期内丢弃的总条数，恢复时一并告知后归零

    def transform_int_to_time(seconds: int) -> str:
        """内部工具：把 seconds 秒转为 "HH:MM:SS" 形式的字符串。"""
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    while True:
        index_time += 1
        txt = (
            str(index_time)
            + "\n"
            + transform_int_to_time(index_time)
            + ",000 --> "
            + transform_int_to_time(index_time + 1)
            + ",000"
            + "\n"
            + re_datetime
            + "\n\n"
        )

        if index_time < sub_next_retry_at:
            sub_window_failures += 1
            sub_total_failures += 1
        else:
            try:
                with open(sub_out_path, "a", encoding=state.text_encoding) as f:
                    _ = f.write(txt)
            except OSError as e:
                sub_window_failures += 1
                sub_total_failures += 1
                sub_next_retry_at = index_time + _SUB_WRITE_RETRY_INTERVAL
                if index_time >= sub_next_warn_at:
                    logger.warning(
                        i18n.tr(
                            "[时间字幕]写入失败，{retry}s 后重试打开；本窗口丢失 {lost} 条、累计 {total} 条: {out_path} - {type_name}: {e}",
                            retry=_SUB_WRITE_RETRY_INTERVAL,
                            lost=sub_window_failures,
                            total=sub_total_failures,
                            out_path=utils.mask_credentials(sub_out_path),
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )
                    sub_next_warn_at = index_time + _SUB_WRITE_WARN_WINDOW
                    sub_window_failures = 0
            else:
                if sub_total_failures:
                    logger.warning(
                        i18n.tr(
                            "[时间字幕]写入已恢复，故障期间共丢失 {total} 条: {out_path}",
                            total=sub_total_failures,
                            out_path=utils.mask_credentials(sub_out_path),
                        )
                    )
                    sub_total_failures = 0
                    sub_window_failures = 0
                    sub_next_warn_at = 0
                    sub_next_retry_at = 0

        with state.record_state_lock:
            still_recording = record_name in state.recording
        if not still_recording:
            return
        time.sleep(1)
        today = datetime.datetime.now()
        re_datetime = today.strftime("%Y-%m-%d %H:%M:%S")
