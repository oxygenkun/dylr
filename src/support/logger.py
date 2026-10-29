import configparser
import os
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING

from loguru import logger

from src.config.booleans import parse_config_bool

if TYPE_CHECKING:
    from loguru import Record


__all__ = [
    "logger",
    "rebind_console_sink",
    "remove_file_sinks",
    "add_file_sinks",
    "ROOM_FIELD",
    "set_room_context",
    "get_room_context",
]


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(sys.executable))
    entry_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
    source_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return os.path.dirname(source_dir) if entry_dir == source_dir else entry_dir


logger.remove()

ROOM_FIELD = "room"
_room_var: ContextVar[str] = ContextVar("dlr_room_context", default="")


def set_room_context(room: str) -> None:
    _room_var.set(room or "")


def get_room_context() -> str:
    return _room_var.get()


def _room_patcher(record: Record) -> None:
    room = _room_var.get()
    if room and not record["extra"].get(ROOM_FIELD):
        record["extra"][ROOM_FIELD] = room


logger.configure(extra={ROOM_FIELD: ""}, patcher=_room_patcher)

custom_format = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> - <level>{message}</level>"

_console_sink_id: int | None = None


def _console_colorize() -> bool:
    """控制台该不该着色：判定口径单点收敛到这里，导入期注册与 rebind_console_sink() 共用一份实现。"""
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def rebind_console_sink() -> None:
    """按当前 sys.stderr 建立/重建控制台 sink。loguru 的 sink 在 add() 时即绑定具体对象，**不会**因。"""
    global _console_sink_id
    if _console_sink_id is not None:
        try:
            logger.remove(_console_sink_id)
        except ValueError:
            pass
        _console_sink_id = None
    if sys.stderr is None:
        return
    _console_sink_id = logger.add(
        sink=sys.stderr, format=custom_format, level="DEBUG", colorize=_console_colorize(), enqueue=True
    )


if sys.stderr is not None:
    _console_sink_id = logger.add(
        sink=sys.stderr, format=custom_format, level="DEBUG", colorize=_console_colorize(), enqueue=True
    )

script_path = _app_root()

_streamget_sink_id: int | None = None
_playurl_sink_id: int | None = None


_STREAMGET_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[room]} | {name}:{function}:{line} - {message}"
)
_PLAYURL_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {extra[room]} | {message}"


def _add_streamget_sink() -> int:
    """两个录制文件 sink 的分档口径（改任一处都要同步，排查链路靠它定位该看哪个文件）：INFO 级 →。"""
    return logger.add(
        f"{script_path}/logs/streamget.log",
        level="DEBUG",
        format=_STREAMGET_FORMAT,
        filter=lambda i: i["level"].name != "INFO",
        serialize=False,
        enqueue=True,
        retention=3,
        rotation="300 KB",
        encoding="utf-8",
    )


def _add_playurl_sink() -> int:
    """注册 INFO 级文件 sink（直播流 URL 等），返回 handler id。"""
    return logger.add(
        f"{script_path}/logs/PlayURL.log",
        level="INFO",
        format=_PLAYURL_FORMAT,
        filter=lambda i: i["level"].name == "INFO",
        serialize=False,
        enqueue=True,
        retention=3,
        rotation="300 KB",
        encoding="utf-8",
    )


def remove_file_sinks() -> None:
    """移除并关闭录制日志文件 sink：loguru remove() 会先 flush enqueue 队列再关闭文件句柄，。"""
    global _streamget_sink_id, _playurl_sink_id
    if _streamget_sink_id is not None:
        try:
            logger.remove(_streamget_sink_id)
        except ValueError:
            pass
        _streamget_sink_id = None
    if _playurl_sink_id is not None:
        try:
            logger.remove(_playurl_sink_id)
        except ValueError:
            pass
        _playurl_sink_id = None


def _register_recording_sinks() -> None:
    """两个录制 sink **逐个独立**注册：原先两处调用点都把 _add_streamget_sink / _add_playurl_sink。"""
    global _streamget_sink_id, _playurl_sink_id
    if _streamget_sink_id is None:
        try:
            _streamget_sink_id = _add_streamget_sink()
        except OSError:
            _streamget_sink_id = None
    if _playurl_sink_id is None:
        try:
            _playurl_sink_id = _add_playurl_sink()
        except OSError:
            _playurl_sink_id = None


def add_file_sinks() -> None:
    """重新注册录制日志文件 sink（归档改名后恢复日志写入，loguru add() 即创建全新同名文件）。"""
    global _streamget_sink_id, _playurl_sink_id
    if not _log_to_file:
        return
    try:
        _register_recording_sinks()
    except OSError:
        _streamget_sink_id = None
        _playurl_sink_id = None


_log_to_file: bool = True
try:
    _cfg_parser = configparser.RawConfigParser()
    _files_read = _cfg_parser.read(f"{script_path}/config/config.ini", encoding="utf-8-sig")
    _log_to_file = parse_config_bool(_cfg_parser.get("录制设置", "是否启用日志文件(是/否)"), True)
except configparser.NoSectionError, configparser.NoOptionError:
    pass
except Exception:
    pass

if _log_to_file:
    _register_recording_sinks()
