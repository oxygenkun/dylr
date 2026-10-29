from __future__ import annotations

import re
import threading
import time
from typing import Optional, TextIO

from src.support import i18n
from src.support.logger import logger

_OPEN_RETRY_INTERVAL = 10.0
_CLOSE_LOCK_TIMEOUT = 5.0


def _format_ts(seconds: float) -> str:
    """把相对秒数 seconds 格式化为 SRT 时间戳字符串 "HH:MM:SS,mmm"；负数按 0 处理，毫秒四舍五入并逐级进位。"""
    if seconds < 0:
        seconds = 0.0
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    if ms == 1000:  # 四舍五入进位
        ms = 0
        s += 1
        if s == 60:
            s = 0
            m += 1
            if m == 60:
                m = 0
                h += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_SRT_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0085\u2028\u2029]")


def _sanitize_srt_text(text: str) -> str:
    """清洗弹幕文本，避免污染 SRT 结构：user_name/message 均为外部可控输入，换行会截断字幕块、。"""
    return _SRT_CONTROL_CHARS_RE.sub(" ", text).replace("\r", " ").replace("\n", " ").replace("-->", "->")


class SrtWriter:
    def __init__(
        self,
        base_filename: str,
        segment_seconds: Optional[float] = 1800.0,
        display_duration: float = 1.5,
    ) -> None:
        """初始化写入器：base_filename 为录像文件去扩展名前缀（如 .../主播名_2026-08-12_21-00-00），。"""
        self._base = base_filename
        self._seg_seconds = segment_seconds if segment_seconds and segment_seconds > 0 else None
        self._display_duration = display_duration

        self._lock = threading.Lock()
        self._t0: Optional[float] = None  # monotonic 基准（start() 锚定的录像起点）
        self._current_seg = -1
        self._index = 0  # 当前片内序号
        self._fp: Optional[TextIO] = None
        self._last_end: Optional[float] = None  # 上一条结束时间，用于密集弹幕错开
        self._last_open_attempt = 0.0  # WD-03：句柄打开失败的退避基准（monotonic）
        self._last_open_error = ""  # L-18：最近一次句柄打开失败的异常类型名（供重试失败告警）
        self._closed = False
        self._discarded_after_count = 0

    def start(self, now: Optional[float] = None) -> None:
        """锚定时间轴 T0（录像起点）并立即创建第 0 片 SRT 文件（哪怕尚无弹幕）；。"""
        if now is None:
            now = time.monotonic()
        with self._lock:
            if self._t0 is not None:
                return
            self._t0 = now
            self._current_seg = 0
            self._index = 0
            self._open_segment(0)

    def _ensure_started(self, now: float) -> None:
        """兜底初始化（需持锁调用）：未调用过 start() 时以 now 作为 T0 并打开第 0 片文件，无返回值。"""
        if self._t0 is None:
            self._t0 = now
            self._current_seg = 0
            self._index = 0
            self._open_segment(0)

    def _open_segment(self, seg: int) -> None:
        """以追加模式打开第 seg 片 SRT 文件（UTF-8），并重置片内序号与上一条结束时间。"""
        if self._closed:
            self._fp = None
            return
        path = self._segment_path(seg)
        try:
            self._fp = open(path, "a", encoding="utf-8")
            self._last_open_error = ""
        except OSError as e:
            self._last_open_error = type(e).__name__
            self._fp = None
        self._index = 0
        self._last_end = None

    def _segment_path(self, seg: int) -> str:
        """根据分片序号 seg 返回 SRT 文件路径：单文件模式为 "{base}.srt"，否则为 "{base}_{seg:03d}.srt"。"""
        if self._seg_seconds is None:
            return f"{self._base}.srt"
        return f"{self._base}_{seg:03d}.srt"

    def write(self, user_name: str, message: str, now: Optional[float] = None) -> None:
        """写入一条弹幕 "user_name: message"：按 now（monotonic，缺省内部取）算出片号并按需切片，。"""
        if now is None:
            now = time.monotonic()
        with self._lock:
            if self._closed:
                self._discarded_after_count += 1
                if self._discarded_after_count == 1:
                    logger.debug(i18n.tr("[弹幕采集]SRT 文件已关闭，后续到达的弹幕一律丢弃"))
                return
            self._ensure_started(now)
            assert self._t0 is not None  # _ensure_started 保证已初始化
            rel = now - self._t0  # 距 T0 总秒数
            if self._seg_seconds is not None:
                seg = int(rel // self._seg_seconds)
                if seg != self._current_seg:
                    self._close_locked()
                    self._current_seg = seg
                    self._open_segment(seg)
                start = rel - seg * self._seg_seconds  # 片内时间
            else:
                start = rel

            if self._fp is None:
                _now = time.monotonic()
                if _now - self._last_open_attempt >= _OPEN_RETRY_INTERVAL:
                    self._last_open_attempt = _now
                    self._open_segment(self._current_seg)
                    if self._fp is None:
                        logger.warning(
                            i18n.tr(
                                "SRT 字幕文件打开失败（弹幕暂时无法落盘，将按间隔重试）: {path} - {type_name}",
                                path=self._segment_path(self._current_seg),
                                type_name=self._last_open_error or "OSError",
                            )
                        )

            end = start + self._display_duration
            if self._last_end is not None and end <= self._last_end:
                end = self._last_end + self._display_duration
            if self._seg_seconds is not None:
                end = max(start, min(end, float(self._seg_seconds)))
            self._last_end = end

            self._index += 1
            line = (
                f"{self._index}\n"
                f"{_format_ts(start)} --> {_format_ts(end)}\n"
                f"{_sanitize_srt_text(user_name)}: {_sanitize_srt_text(message)}\n\n"
            )
            if self._fp is not None:
                self._fp.write(line)
                self._fp.flush()

    def _close_locked(self) -> None:
        """需持锁调用的内部关闭：flush 并关闭当前文件句柄后置空，异常忽略，无返回值。"""
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    def close(self) -> None:
        """对外关闭接口：加锁后落盘并关闭当前 SRT 文件，可重复调用。"""
        if not self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT):
            logger.warning(
                i18n.tr(
                    "[弹幕采集]SRT 关闭等待锁超时（{seconds}s），跳过本次关闭以免阻塞录制线程",
                    seconds=_CLOSE_LOCK_TIMEOUT,
                )
            )
            return
        try:
            self._close_locked()
            self._closed = True
        finally:
            self._lock.release()
