from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from typing import Any, TextIO

from src.support import i18n
from src.support.logger import logger, script_path

_STREAM_SAMPLE_PER_SEC = 10
_RECENT_BUFFER_SIZE = 500
_MAX_MESSAGES_PER_RESPONSE = 200
_ROTATE_BYTES = 5 * 1024 * 1024
_STATS_INTERVAL = 5.0
_RATE_WINDOW_SEC = 60.0

_SIDECAR_QUEUE_MAXSIZE = 20000
_SIDECAR_DRAIN_SECONDS = 5.0

_ROTATE_RETRY_BACKOFF_SECONDS = 300.0


_rotate_backoff_until: dict[str, float] = {}
_rotate_backoff_lock = threading.Lock()


def _rotate_in_backoff(path: str) -> bool:
    """该路径是否处于轮转退避期内；顺带回收已到期条目，避免字典随历史路径数无界增长。"""
    with _rotate_backoff_lock:
        until = _rotate_backoff_until.get(path)
        if until is None:
            return False
        if time.monotonic() >= until:
            _rotate_backoff_until.pop(path, None)
            return False
        return True


def _note_rotate_failure(path: str, err: OSError) -> None:
    """记一次轮转失败：置退避标记 + 留一条 warning（带异常类型与原文，用来区分「.1 被占用」与「跨卷」）。"""
    with _rotate_backoff_lock:
        _rotate_backoff_until[path] = time.monotonic() + _ROTATE_RETRY_BACKOFF_SECONDS
    logger.warning(
        i18n.tr(
            "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
            where="rotate",
            type_name=type(err).__name__,
            e=err,
        )
    )


def _append_sidecar_line(files: dict[str, TextIO], path: str, line: str) -> None:
    """把一行 JSON 追加到边车文件并按需轮转（保留一代 .1 备份）。独立成模块级函数是**给测试留的注入口**。"""
    fh: TextIO | None = None
    try:
        fh = files.get(path)
        if fh is None:
            fh = files[path] = open(path, "a", encoding="utf-8")
        if fh.tell() >= _ROTATE_BYTES and not _rotate_in_backoff(path):
            fh.close()
            files.pop(path, None)
            try:
                os.replace(path, path + ".1")
            except OSError as e:
                _note_rotate_failure(path, e)
            fh = files[path] = open(path, "a", encoding="utf-8")
        fh.write(line + "\n")
        fh.flush()
    except Exception as e:
        logger.debug(i18n.tr("弹幕边车文件写入失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass
        files.pop(path, None)


def _close_sidecar_handle(files: dict[str, TextIO], path: str) -> None:
    """关闭某路径的边车句柄（日志归档改名前调用）。"""
    fh = files.pop(path, None)
    if fh is None:
        return
    try:
        fh.flush()
        fh.close()
    except Exception as e:
        logger.debug(i18n.tr("[弹幕监控]关闭边车文件异常(忽略): {type_name}: {e}", type_name=type(e).__name__, e=e))


class _SidecarWriter:
    def __init__(self, maxsize: int = _SIDECAR_QUEUE_MAXSIZE) -> None:
        """指令载体为 (op, path, line)；op ∈ {"line", "close"}。"""
        self._queue: queue.Queue[tuple[str, str, str]] = queue.Queue(maxsize=maxsize)
        self._files: dict[str, TextIO] = {}
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._dropped = 0
        self._cond = threading.Condition()
        self._outstanding = 0
        self._suspended: set[str] = set()
        self._suspend_lock = threading.Lock()
        self._suppressed: dict[str, int] = {}

    def suspend(self, path: str) -> None:
        """进入归档窗口：置位该路径的抑制标记。置位后「已在队列中排队的 line 指令」与「此后新入队的。"""
        with self._suspend_lock:
            self._suspended.add(path)
            self._suppressed.pop(path, None)

    def resume(self, path: str) -> None:
        """退出归档窗口：解除抑制，下一条事件经写线程的惰性重开逻辑继续落盘。"""
        with self._suspend_lock:
            self._suspended.discard(path)
            self._suppressed.pop(path, None)

    def is_suspended(self, path: str) -> bool:
        """该路径当前是否处于抑制期（供观测/断言）。"""
        with self._suspend_lock:
            return path in self._suspended

    def submit(self, op: str, path: str, line: str = "") -> bool:
        """入队一条指令；队列满时记丢弃计数并返回 False（调用方不得被阻塞）。"""
        if op == "line" and self.is_suspended(path):
            with self._suspend_lock:
                suppressed = self._suppressed.get(path, 0) + 1
                self._suppressed[path] = suppressed
            if suppressed == 1 or suppressed % 1000 == 0:
                logger.debug(i18n.tr("[弹幕监控]归档窗口内丢弃 {count} 条边车事件", count=suppressed))
            return False
        self._ensure_thread()
        with self._cond:
            try:
                self._queue.put_nowait((op, path, line))
            except queue.Full:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 1000 == 0:
                    logger.warning(i18n.tr("[弹幕监控]边车写入队列已满，累计丢弃 {count} 条事件", count=self._dropped))
                return False
            self._outstanding += 1
        return True

    def drain(self, timeout: float = _SIDECAR_DRAIN_SECONDS) -> bool:
        """等待已入队的指令全部处理完（带超时）；返回是否排空。"""
        with self._cond:
            return self._cond.wait_for(lambda: self._outstanding == 0, timeout)

    def open_handle(self, path: str) -> TextIO | None:
        """当前是否仍持有某路径的打开句柄（仅供观测/断言，不做 IO）。"""
        return self._files.get(path)

    def _ensure_thread(self) -> None:
        """惰性起线程；守护线程随进程结束回收，故无需显式 stop（drain 已覆盖需要落盘的时机）。"""
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="danmaku-monitor-sidecar", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        """写线程主体：任何单条指令异常都不得让线程退出（否则监控流永久静默断档）。"""
        while True:
            op, path, line = self._queue.get()
            try:
                if op == "close":
                    _close_sidecar_handle(self._files, path)
                elif self.is_suspended(path):
                    pass
                else:
                    _append_sidecar_line(self._files, path, line)
            except Exception as e:
                logger.debug(
                    i18n.tr(
                        "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                        where="sidecar",
                        type_name=type(e).__name__,
                        e=e,
                    )
                )
            finally:
                with self._cond:
                    self._outstanding -= 1
                    self._cond.notify_all()


_sidecar: _SidecarWriter | None = None
_sidecar_lock = threading.Lock()


def _get_sidecar() -> _SidecarWriter:
    global _sidecar
    with _sidecar_lock:
        if _sidecar is None:
            _sidecar = _SidecarWriter()
        return _sidecar


class DanmakuMonitorHub:
    def __init__(self, log_path: str | None = None) -> None:
        """初始化枢纽。log_path 为 JSONL 边车文件路径，None 表示禁用文件输出。"""
        self._lock = threading.Lock()
        self._log_path: str | None = self._prepare_log_path(log_path)
        self._rooms: dict[str, dict[str, Any]] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_BUFFER_SIZE)
        self._seq = 0
        self._stats_thread: threading.Thread | None = None
        self._stats_stop = threading.Event()

    @property
    def _file(self) -> TextIO | None:
        """本枢纽边车路径当前是否仍有打开句柄（写线程独占，这里只读它的登记表）。"""
        if not self._log_path:
            return None
        _get_sidecar().drain()
        return _get_sidecar().open_handle(self._log_path)

    def flush(self, timeout: float = _SIDECAR_DRAIN_SECONDS) -> bool:
        """等待本枢纽已入队的边车指令全部落盘（带超时）；返回是否排空。"""
        return _get_sidecar().drain(timeout)

    def room_started(self, room: str, platform: str) -> None:
        """房间开始采集（collector.start）：重置该房间统计并写 conn/started 事件。"""
        try:
            with self._lock:
                self._rooms[room] = self._default_state(platform)
                seq = self._next_seq()
                self._write_line(
                    {
                        "ev": "conn",
                        "seq": seq,
                        "room": room,
                        "platform": platform,
                        "state": "started",
                        "ts": time.time(),
                    }
                )
                self._ensure_stats_thread()
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                    where="room_started",
                    type_name=type(e).__name__,
                    e=e,
                )
            )

    def room_connected(self, room: str) -> None:
        """弹幕连接就绪（collector.on_ready）：置连接状态并写 conn/ready 事件。"""
        try:
            with self._lock:
                state = self._rooms.get(room)
                if state is not None:
                    state["connected"] = True
                self._write_line(
                    {
                        "ev": "conn",
                        "seq": self._next_seq(),
                        "room": room,
                        "state": "ready",
                        "ts": time.time(),
                    }
                )
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                    where="room_connected",
                    type_name=type(e).__name__,
                    e=e,
                )
            )

    def room_closed(self, room: str, reason: str = "") -> None:
        """弹幕连接关闭（collector.on_close / stop）：清除连接状态并写 conn/closed 事件。"""
        try:
            with self._lock:
                state = self._rooms.get(room)
                if state is not None:
                    state["connected"] = False
                self._write_line(
                    {
                        "ev": "conn",
                        "seq": self._next_seq(),
                        "room": room,
                        "state": "closed",
                        "reason": reason,
                        "ts": time.time(),
                    }
                )
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                    where="room_closed",
                    type_name=type(e).__name__,
                    e=e,
                )
            )

    def room_stopped(self, room: str, reason: str | None = None) -> None:
        """房间停止监控（main.py 的录制线程在 outer try 的 finally 里调用，覆盖录制态/轮询态/解析失败态）。"""
        if reason is None:
            reason = i18n.tr("房间已停止监控")
        try:
            with self._lock:
                if self._rooms.pop(room, None) is None:
                    return  # 未注册过的房间不打事件
                self._write_line(
                    {
                        "ev": "conn",
                        "seq": self._next_seq(),
                        "room": room,
                        "state": "stopped",
                        "reason": reason,
                        "ts": time.time(),
                    }
                )
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                    where="room_stopped",
                    type_name=type(e).__name__,
                    e=e,
                )
            )

    def room_message(self, room: str, msg_type: str, user: str, text: str, data: Any = None) -> None:
        """收到一条弹幕消息（collector.on_message 转发，msg_type 取 DanmakuMessageType.value）。"""
        try:
            with self._lock:
                state = self._rooms.get(room)
                if state is None:
                    state = self._default_state("未知")
                    self._rooms[room] = state
                now = time.time()
                state["last_msg_at"] = now
                if msg_type == "chat":
                    state["msg_total"] = int(state["msg_total"]) + 1
                    self._bump_rate(state, now)
                    if self._allow_stream_sample(state):
                        self._emit_message(room, "chat", user, text)
                elif msg_type in ("gift", "superChat"):
                    state["gift_total"] = int(state["gift_total"]) + 1
                    self._emit_message(room, msg_type, user, text)
                elif msg_type == "online":
                    if isinstance(data, (int, float)):
                        state["online"] = int(data)
                    elif data is not None and str(data).strip().isdigit():
                        state["online"] = int(str(data).strip())
                    else:
                        state["online"] = self._parse_online(text, int(state["online"]))
                self._ensure_stats_thread()
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                    where="room_message",
                    type_name=type(e).__name__,
                    e=e,
                )
            )

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        """生成监控快照：rooms 为各房间统计（含人读时间），messages 为 seq 大于。"""
        try:
            with self._lock:
                now = time.time()
                rooms: list[dict[str, Any]] = []
                for name, state in self._rooms.items():
                    state["msg_rate"] = self._rate_of(state, now)
                    rooms.append(
                        {
                            "name": name,
                            "platform": state["platform"],
                            "connected": bool(state["connected"]),
                            "started_at": time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(float(state["started_at"]))
                            ),
                            "msg_total": int(state["msg_total"]),
                            "msg_rate": int(state["msg_rate"]),
                            "gift_total": int(state["gift_total"]),
                            "online": int(state["online"]),
                        }
                    )
                messages = [m for m in self._recent if int(m["seq"]) > since]
                truncated = False
                if len(messages) > _MAX_MESSAGES_PER_RESPONSE:
                    messages = messages[-_MAX_MESSAGES_PER_RESPONSE:]
                    truncated = True
                return {
                    "rooms": rooms,
                    "messages": messages,
                    "last_seq": self._seq,
                    "truncated": truncated,
                }
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                    where="snapshot",
                    type_name=type(e).__name__,
                    e=e,
                )
            )
            return {"rooms": [], "messages": [], "last_seq": since, "truncated": False}

    @staticmethod
    def _default_state(platform: str) -> dict[str, Any]:
        """构造新房间的初始状态字典。"""
        return {
            "platform": platform,
            "connected": False,
            "started_at": time.time(),
            "msg_total": 0,
            "gift_total": 0,
            "online": 0,
            "msg_rate": 0,
            "last_msg_at": 0.0,
            "_buckets": deque(maxlen=6),
            "_sample_win_start": time.monotonic(),
            "_sample_win_count": 0,
            "_dropped": 0,
        }

    def _next_seq(self) -> int:
        """分配下一个单调递增游标（须持 self._lock 调用）。"""
        self._seq += 1
        return self._seq

    @staticmethod
    def _bump_rate(state: dict[str, Any], now: float) -> None:
        """往当前时间窗的速率桶中计数一条（须持 self._lock 调用）。"""
        buckets: deque[tuple[float, int]] = state["_buckets"]
        bucket_start = int(now // 10) * 10
        if buckets and buckets[-1][0] == bucket_start:
            buckets[-1] = (bucket_start, buckets[-1][1] + 1)
        else:
            buckets.append((bucket_start, 1))

    @staticmethod
    def _rate_of(state: dict[str, Any], now: float) -> int:
        """计算近 60 秒窗口内的弹幕速率（条/分）：剪枝过期桶后求和（须持 self._lock 调用）。"""
        buckets: deque[tuple[float, int]] = state["_buckets"]
        while buckets and buckets[0][0] < now - _RATE_WINDOW_SEC:
            buckets.popleft()
        return sum(count for _start, count in buckets)

    @staticmethod
    def _allow_stream_sample(state: dict[str, Any]) -> bool:
        """展示流采样判定：每房间每秒最多 _STREAM_SAMPLE_PER_SEC 条，超出折叠计数（须持 self._lock 调用）。"""
        now_mono = time.monotonic()
        if now_mono - float(state["_sample_win_start"]) >= 1.0:
            state["_sample_win_start"] = now_mono
            state["_sample_win_count"] = 0
        if int(state["_sample_win_count"]) < _STREAM_SAMPLE_PER_SEC:
            state["_sample_win_count"] = int(state["_sample_win_count"]) + 1
            return True
        state["_dropped"] = int(state["_dropped"]) + 1
        return False

    def _emit_message(self, room: str, msg_type: str, user: str, text: str) -> None:
        """将一条消息写入展示流（内存缓冲 + JSONL），折入被采样折叠的条数（须持 self._lock 调用）。"""
        payload: dict[str, Any] = {
            "ev": "msg",
            "seq": self._next_seq(),
            "room": room,
            "type": msg_type,
            "user": user,
            "text": text,
            "ts": time.time(),
        }
        dropped = int(self._rooms[room]["_dropped"]) if room in self._rooms else 0
        if dropped > 0:
            payload["dropped"] = dropped
            self._rooms[room]["_dropped"] = 0
        self._recent.append(dict(payload))
        self._write_line(payload)

    @staticmethod
    def _parse_online(text: str, current: int) -> int:
        """解析在线人数文本：优先整体转 int；否则按 K/M/万/亿 单位换算（如 "1.2万" → 12000）；。"""
        t = text.strip()
        if not t:
            return current
        try:
            return int(t)
        except ValueError:
            unit = 1
            if "亿" in t or "億" in t:
                unit = 100000000
            elif "万" in t or "萬" in t:
                unit = 10000
            elif t.upper().endswith("K"):
                unit = 1000
            elif t.upper().endswith("M"):
                unit = 1000000
            body = t.replace("亿", "").replace("億", "").replace("万", "").replace("萬", "")
            body = body.rstrip("KkMm")
            cleaned = "".join(ch for ch in body if ch.isdigit() or ch == ".")
            if cleaned:
                try:
                    return int(float(cleaned) * unit)
                except ValueError:
                    pass
            digits = "".join(ch for ch in t if ch.isdigit())
            return int(digits) if digits else current

    def _ensure_stats_thread(self) -> None:
        """确保周期 stats 线程在运行（房间全部移除后线程自行退出，须持 self._lock 调用）。"""
        t = self._stats_thread
        if t is not None and t.is_alive():
            return
        self._stats_stop.clear()
        self._stats_thread = threading.Thread(target=self._stats_loop, name="danmaku-monitor-stats", daemon=True)
        self._stats_thread.start()

    def _stats_loop(self) -> None:
        """stats 线程主体：每 _STATS_INTERVAL 秒为全部房间写一条 stats 事件；。"""
        thread = threading.current_thread()
        try:
            while not self._stats_stop.wait(_STATS_INTERVAL):
                try:
                    with self._lock:
                        if not self._rooms:
                            break
                        now = time.time()
                        for room, state in self._rooms.items():
                            state["msg_rate"] = self._rate_of(state, now)
                            self._write_line(
                                {
                                    "ev": "stats",
                                    "room": room,
                                    "platform": state["platform"],
                                    "connected": bool(state["connected"]),
                                    "msg_total": int(state["msg_total"]),
                                    "msg_rate": int(state["msg_rate"]),
                                    "gift_total": int(state["gift_total"]),
                                    "online": int(state["online"]),
                                    "ts": now,
                                }
                            )
                except Exception as e:
                    logger.debug(
                        i18n.tr(
                            "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                            where="stats_loop",
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )
        except Exception as e:
            logger.warning(
                i18n.tr(
                    "[弹幕监控]{where} 线程异常退出，将在下一条弹幕事件时重新拉起: {type_name}: {e}",
                    where="stats",
                    type_name=type(e).__name__,
                    e=e,
                )
            )
        finally:
            try:
                with self._lock:
                    if self._stats_thread is thread:
                        self._stats_thread = None
            except Exception as e:
                logger.debug(
                    i18n.tr(
                        "[弹幕监控]{where} 处理失败(忽略): {type_name}: {e}",
                        where="stats_cleanup",
                        type_name=type(e).__name__,
                        e=e,
                    )
                )

    @staticmethod
    def _prepare_log_path(log_path: str | None) -> str | None:
        """准备日志文件路径：创建父目录，失败则禁用文件输出并返回 None。"""
        if not log_path:
            return None
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            return log_path
        except Exception as e:
            logger.debug(i18n.tr("[弹幕监控]日志目录创建失败，禁用文件输出: {e}", e=e))
            return None

    def _write_line(self, payload: dict[str, Any]) -> None:
        """把一行 JSON 事件**入队**交给进程级写线程落盘（MID-25：调用方持 self._lock 时只做序列化 +。"""
        if not self._log_path:
            return
        try:
            _get_sidecar().submit("line", self._log_path, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug(i18n.tr("弹幕边车文件写入失败: {type_name}: {e}", type_name=type(e).__name__, e=e))

    def close_file(self) -> None:
        """flush 并关闭 JSONL 边车文件句柄（停止录制归档流程在改名前调用）。"""
        try:
            path = self._log_path
            if not path:
                return
            writer = _get_sidecar()
            if not writer.submit("close", path):
                raise TimeoutError("sidecar close command dropped (queue full)")
            if not writer.drain(_SIDECAR_DRAIN_SECONDS):
                raise TimeoutError(f"sidecar drain exceeded {_SIDECAR_DRAIN_SECONDS}s")
        except Exception as e:
            logger.debug(i18n.tr("[弹幕监控]关闭边车文件异常(忽略): {type_name}: {e}", type_name=type(e).__name__, e=e))

    def suspend_writes(self) -> None:
        """进入「停止录制 → 日志归档改名」窗口（MID-2243）：先置位抑制标记，再复用 close_file() 的。"""
        try:
            path = self._log_path
            if not path:
                return
            _get_sidecar().suspend(path)
            self.close_file()
        except Exception as e:
            logger.debug(i18n.tr("[弹幕监控]关闭边车文件异常(忽略): {type_name}: {e}", type_name=type(e).__name__, e=e))

    def resume_writes(self) -> None:
        """退出归档窗口：解除抑制，下一条事件经惰性重开继续落盘（幂等，可重复调用）。"""
        try:
            path = self._log_path
            if not path:
                return
            _get_sidecar().resume(path)
        except Exception as e:
            logger.debug(i18n.tr("[弹幕监控]关闭边车文件异常(忽略): {type_name}: {e}", type_name=type(e).__name__, e=e))

    def writes_suspended(self) -> bool:
        """当前是否处于归档抑制期（供观测/断言）。"""
        path = self._log_path
        if not path:
            return False
        return _get_sidecar().is_suspended(path)


_hub: DanmakuMonitorHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> DanmakuMonitorHub:
    """获取进程级弹幕监控枢纽单例：首次调用时以默认路径。"""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = DanmakuMonitorHub(log_path=os.path.join(script_path, "logs", "danmaku_monitor.jsonl"))
        return _hub


def close_monitor_file() -> None:
    """关闭弹幕监控边车文件句柄（停止录制归档流程调用，归档改名前必须先关句柄——。"""
    hub = _hub
    if hub is not None:
        hub.close_file()


def suspend_monitor_writes() -> None:
    """归档窗口的进入/退出（src/app/log_archive.py 成对调用，改名结束一律要退出）；枢纽未初始化时为 no-op、。"""
    hub = _hub
    if hub is not None:
        hub.suspend_writes()


def resume_monitor_writes() -> None:
    hub = _hub
    if hub is not None:
        hub.resume_writes()
