import io
import os
import sys
import threading
from datetime import datetime
from typing import TextIO

from src.danmaku.monitor import close_monitor_file, resume_monitor_writes, suspend_monitor_writes
from src.support import i18n
from src.support.logger import add_file_sinks, logger, rebind_console_sink, remove_file_sinks, script_path

ARCHIVE_LOG_NAMES: tuple[str, str, str, str] = (
    "streamget.log",
    "PlayURL.log",
    "danmaku_monitor.jsonl",
    "web_console.log",
)

_DISABLE_ENV = "DOUYIN_DISABLE_LOG_ARCHIVE"

_archive_lock = threading.Lock()

_web_console_rebind_pending = False


def _archive_target(src_path: str, ts: str) -> str:
    """生成归档目标路径：原名_YYYYMMDD_HHMMSS.扩展名；目标已存在时依次追加 _1/_2 序号避免覆盖。"""
    dir_name, file_name = os.path.split(src_path)
    stem, ext = os.path.splitext(file_name)
    candidate = os.path.join(dir_name, f"{stem}_{ts}{ext}")
    seq = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dir_name, f"{stem}_{ts}_{seq}{ext}")
        seq += 1
    return candidate


def _streams_bound_to(path: str) -> list[TextIO]:
    """收集 sys.stdout/sys.stderr 中指向指定文件的活动句柄（web.py 后台模式两者为同一对象，去重）。"""
    bound: list[TextIO] = []
    for stream in (sys.stdout, sys.stderr):
        if stream is None or any(stream is existing for existing in bound):
            continue
        name = getattr(stream, "name", None)
        if isinstance(name, str) and os.path.abspath(name) == os.path.abspath(path):
            bound.append(stream)
    return bound


_BRIDGE_BUFFER_LIMIT = 65536


class _ConsoleBridge(io.TextIOBase):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._size = 0
        self._dropped = 0
        self._buf_lock = threading.Lock()

    @property
    def name(self) -> str:
        """替身不对应任何文件路径，且该 name 必须与 logs/web_console.log 不同。"""
        return "<dlr-log-archive-bridge>"

    def writable(self) -> bool:
        """必须显式覆写：io.TextIOBase 的 writable() 默认返回 False，。"""
        return True

    def isatty(self) -> bool:
        """恒 False：替身不是终端。src/support/logger.py 的 _console_colorize() 走同样的 isatty 口径，。"""
        return False

    def write(self, text: str) -> int:
        with self._buf_lock:
            if self._size < _BRIDGE_BUFFER_LIMIT:
                self._chunks.append(text)
                self._size += len(text)
            else:
                self._dropped += 1
        return len(text)

    def flush(self) -> None:
        return None

    def take(self) -> str:
        """取出窗口期内缓存的全部输出（取出即清空）。只应在标准流已交回真实句柄后调用。"""
        with self._buf_lock:
            content = "".join(self._chunks)
            self._chunks.clear()
            self._size = 0
        return content


def _install_console_bridge() -> _ConsoleBridge:
    """第一步：用替身接管标准流，并把 loguru 控制台 sink 一并改绑到它。"""
    bridge = _ConsoleBridge()
    sys.stdout = bridge
    sys.stderr = bridge
    rebind_console_sink()
    return bridge


def _merge_bridge_output(bridge: _ConsoleBridge | None) -> None:
    """收尾：把替身期间缓存的输出并入新句柄。这些行本该落在被归档的那份文件里，与其丢弃不如追加到。"""
    if bridge is None:
        return
    stream = sys.stdout
    if stream is None or stream is bridge:
        return
    try:
        content = bridge.take()
        if content:
            stream.write(content)
    except Exception:
        pass


def _rebind_web_console(path: str) -> None:
    """重建 web_console.log 句柄并重新接管 sys.stdout/sys.stderr 与 loguru 控制台 sink。"""
    global _web_console_rebind_pending
    try:
        stream = open(path, "a", encoding="utf-8", buffering=1)
        _web_console_rebind_pending = False
    except OSError as e:
        logger.warning(i18n.tr("重建 web_console.log 句柄失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        _web_console_rebind_pending = True
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except OSError as e2:
            logger.error(i18n.tr("回退到 os.devnull 也失败: {type_name}: {e}", type_name=type(e2).__name__, e=e2))
            return
    sys.stdout = stream
    sys.stderr = stream
    rebind_console_sink()


def _archive_web_console(logs_dir: str, ts: str, archived: list[str]) -> None:
    """归档 web_console.log（仅 Web 后台模式下被重定向为 sys.stdout/sys.stderr、且为同一对象）。"""
    path = os.path.join(logs_dir, "web_console.log")
    if _web_console_rebind_pending and not os.path.isfile(path):
        _rebind_web_console(path)
        return
    if not os.path.isfile(path):
        return
    bound = _streams_bound_to(path)
    bridge: _ConsoleBridge | None = None
    if bound:
        bridge = _install_console_bridge()
    for stream in bound:
        try:
            stream.flush()
            stream.close()
        except Exception as e:
            logger.debug(i18n.tr("关闭 web_console 句柄异常(忽略): {type_name}: {e}", type_name=type(e).__name__, e=e))
    try:
        target = _archive_target(path, ts)
        os.rename(path, target)
        archived.append(target)
    except OSError as e:
        logger.warning(
            i18n.tr("日志归档失败(跳过): web_console.log - {type_name}: {e}", type_name=type(e).__name__, e=e)
        )
        if bound or _web_console_rebind_pending:
            _rebind_web_console(path)
            _merge_bridge_output(bridge)
        return
    if bound or _web_console_rebind_pending:
        _rebind_web_console(path)
        _merge_bridge_output(bridge)


def _rename_one(path: str, ts: str, archived: list[str]) -> None:
    """单文件归档：文件不存在则跳过；改名失败仅告警（Windows 下句柄被第三方进程占用时会发生），。"""
    name = os.path.basename(path)
    try:
        if not os.path.isfile(path):
            return
        target = _archive_target(path, ts)
        os.rename(path, target)
        archived.append(target)
        logger.debug(i18n.tr("日志已归档: {name} -> {target}", name=name, target=os.path.basename(target)))
    except OSError as e:
        logger.warning(
            i18n.tr("日志归档失败(跳过): {name} - {type_name}: {e}", name=name, type_name=type(e).__name__, e=e)
        )


def archive_runtime_logs(*, reopen_streams: bool = True) -> list[str]:
    """停止录制流程的日志归档入口：flush/关闭四个运行日志的句柄后，按停止操作发生时刻的。"""
    try:
        with _archive_lock:
            if os.environ.get(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
                return []
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            logs_dir = os.path.join(script_path, "logs")
            archived: list[str] = []

            _archive_web_console(logs_dir, ts, archived)

            remove_file_sinks()
            _rename_one(os.path.join(logs_dir, "streamget.log"), ts, archived)
            _rename_one(os.path.join(logs_dir, "PlayURL.log"), ts, archived)

            suspend_monitor_writes()
            try:
                close_monitor_file()
                _rename_one(os.path.join(logs_dir, "danmaku_monitor.jsonl"), ts, archived)
            finally:
                resume_monitor_writes()

            if reopen_streams:
                add_file_sinks()

            if archived:
                logger.info(
                    i18n.tr("运行日志已归档: {archived}", archived="、".join(os.path.basename(p) for p in archived))
                )
            return archived
    except Exception as e:
        try:
            logger.warning(i18n.tr("运行日志归档失败(忽略): {type_name}: {e}", type_name=type(e).__name__, e=e))
        except Exception:
            pass
        return []
