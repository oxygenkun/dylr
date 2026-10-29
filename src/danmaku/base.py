from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from src.support import i18n


class DanmakuMessageType(Enum):
    CHAT = "chat"
    GIFT = "gift"
    ONLINE = "online"
    SUPER_CHAT = "superChat"


@dataclass
class DanmakuMessage:
    type: DanmakuMessageType
    user_name: str
    message: str
    data: Any = None
    color: str = "#FFFFFF"


def _log_task_exception(task: "asyncio.Task[None]") -> None:
    """后台弹幕协程的统一异常落盘：裸 asyncio.ensure_future 的异常只在任务被 GC 时以。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        from src.support.logger import logger

        logger.warning(i18n.tr("[弹幕]后台协程异常: {type_name}: {exc}", type_name=type(exc).__name__, exc=exc))


def spawn_danmaku_task(coro: Awaitable[None]) -> "asyncio.Task[None]":
    task = asyncio.ensure_future(coro)
    task.add_done_callback(_log_task_exception)
    return task


class DanmakuBase(ABC):
    heartbeat_interval: float = 45.0  # 秒，各平台子类覆盖

    def __init__(
        self,
        on_message: Optional[Callable[[DanmakuMessage], None]] = None,
        on_close: Optional[Callable[[str], None]] = None,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_message = on_message
        self._on_close = on_close
        self._on_ready = on_ready
        self._stopped = False

    def _on_reconnect(self, reason: str) -> None:
        """重连是**中间态**（max_reconnect 次数内会自动恢复），不能复用 on_close——后者的语义是。"""
        from src.support.logger import logger

        logger.debug(i18n.tr("[{cls}]弹幕连接断开，正在重连: {reason}", cls=type(self).__name__, reason=reason))

    @abstractmethod
    async def start(self, args: Any) -> None:
        """抽象方法：用平台启动参数 args（room_id / token 等）建连并持续接收，直到被 stop 或连接结束。"""

    @abstractmethod
    async def stop(self) -> None:
        """抽象方法：停止接收并关闭底层连接。"""

    @abstractmethod
    async def heartbeat(self) -> None:
        """抽象方法：发一次平台约定的心跳包，由 WebSocketClient 按 heartbeat_interval 定时调用。"""

    @abstractmethod
    def decode_message(self, data: bytes | str) -> None:
        """抽象方法：解析一帧原始数据（bytes 或 str），结果经 _emit 上抛。"""

    def _emit(self, msg: DanmakuMessage) -> None:
        """唯一上抛出口：判空只此一处，回调缺省时静默丢弃。"""
        if self._on_message:
            self._on_message(msg)
