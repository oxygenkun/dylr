"""带有界发送、心跳和退避重连的 WebSocket 客户端。"""

import asyncio
import inspect
import random
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.protocol import State

from src.support import i18n, utils
from src.support.logger import logger

_POISON_LOG_LIMIT = 5

_MAX_PENDING_SENDS = 64

_PING_INTERVAL = 20.0
_PING_TIMEOUT = 20.0
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_SEND_TIMEOUT = 5.0

_BACKOFF_MAX_MULTIPLIER = 16

_HEALTHY_CONNECTION_FACTOR = 2
_MIN_HEALTHY_CONNECTION_SECONDS = 1.0


def _backoff_delay(base: float, attempt: int) -> float:
    """重连退避时长：base * 2^(n-1) 封顶 16 倍，并叠加 0~30% 随机抖动。"""
    multiplier = min(2 ** max(attempt - 1, 0), _BACKOFF_MAX_MULTIPLIER)
    jitter = 1.0 + random.random() * 0.3
    return base * multiplier * jitter


def _default_ssl_context(url: str) -> ssl.SSLContext | None:
    """根据 url 生成默认 SSL 上下文：wss:// 返回放宽到 @SECLEVEL=1 的 SSLContext，ws:// 返回 None。"""
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass  # 某些构建不支持该字符串，退回默认
    return ctx


class WebSocketClient:
    def __init__(
        self,
        url: str,
        on_message: Callable[[bytes | str], None],
        *,
        on_ready: Callable[[], None] | None = None,
        on_heartbeat: Callable[[], Awaitable[None] | None] | None = None,
        on_close: Callable[[str], None] | None = None,
        on_reconnect: Callable[[str], None] | None = None,
        heartbeat_interval: float = 45.0,
        headers: Mapping[str, str] | None = None,
        backup_url: str | None = None,
        max_reconnect: int = 5,
        reconnect_interval: float = 5.0,
        connect_timeout: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
        proxy: str | None = None,
    ) -> None:
        self._url = url
        self._backup_url = backup_url
        self._on_message = on_message
        self._on_ready = on_ready
        self._on_heartbeat = on_heartbeat
        self._on_close = on_close
        self._on_reconnect = on_reconnect
        self.heartbeat_interval = heartbeat_interval
        self._headers = headers
        self.max_reconnect = max_reconnect
        self.reconnect_interval = reconnect_interval
        self.connect_timeout = connect_timeout
        self._proxy = proxy
        self._ssl_context = ssl_context if ssl_context is not None else _default_ssl_context(url)

        self._ws: ClientConnection | None = None
        self._stopped = False
        self._reconnect_count = 0
        self._healthy_lifetime_seconds = max(
            _MIN_HEALTHY_CONNECTION_SECONDS, reconnect_interval * _HEALTHY_CONNECTION_FACTOR
        )
        self._close_reported = False
        self._send_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()

    async def connect(self) -> None:
        """接收消息并管理心跳、重连和最终关闭通知。"""
        while not self._stopped:
            url = self._url
            if self._reconnect_count > 0 and self._backup_url:
                url = self._backup_url
            poison_count = 0
            connected_at: float | None = None
            got_frame = False
            try:
                async with websockets.connect(
                    url,
                    additional_headers=self._headers,
                    open_timeout=self.connect_timeout,
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=_PING_TIMEOUT,
                    max_size=_MAX_FRAME_BYTES,
                    ssl=self._ssl_context,
                    proxy=self._proxy,  # None=直连(B站/虎牙等);Twitch 等海外平台可传显式代理
                ) as ws:
                    self._ws = ws
                    connected_at = time.monotonic()
                    if self._on_ready:
                        self._on_ready()
                    hb_task = asyncio.create_task(self._heartbeat_loop())
                    try:
                        async for data in ws:
                            if self._stopped:
                                break
                            got_frame = True
                            try:
                                self._on_message(data)
                            except Exception as e:
                                poison_count += 1
                                if poison_count <= _POISON_LOG_LIMIT:
                                    logger.warning(
                                        i18n.tr(
                                            "弹幕消息处理异常（已丢弃该帧，连接保持）: {type_name}: {e}",
                                            type_name=type(e).__name__,
                                            e=utils.mask_credentials(str(e)),
                                        )
                                    )
                                elif poison_count == _POISON_LOG_LIMIT + 1:
                                    logger.warning(
                                        i18n.tr(
                                            "弹幕消息处理异常持续出现，后续同类异常不再逐条记录（累计 {count} 条）",
                                            count=poison_count,
                                        )
                                    )
                                continue
                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except asyncio.CancelledError:
                            if not hb_task.cancelled():
                                raise
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as error:
                reason = utils.mask_credentials(str(error))
            else:
                reason = ""
            finally:
                self._ws = None

            if self._stopped:
                break
            self._reset_count_if_healthy(connected_at, got_frame)
            self._reconnect_count += 1
            if self._reconnect_count > self.max_reconnect:
                message = (
                    i18n.tr("重连超过最大次数，与服务器断开连接: {reason}", reason=reason)
                    if reason
                    else i18n.tr("重连超过最大次数，与服务器断开连接")
                )
                self._report_close(message)
                break
            if self._on_reconnect:
                self._on_reconnect(reason or i18n.tr("连接已关闭，正在尝试重连"))
            await asyncio.sleep(_backoff_delay(self.reconnect_interval, self._reconnect_count))

    def _reset_count_if_healthy(self, connected_at: float | None, got_frame: bool) -> None:
        """收到消息或连接存活足够久后，重新计算重连预算。"""
        if got_frame:
            self._reconnect_count = 0
            return
        if connected_at is not None and (time.monotonic() - connected_at) > self._healthy_lifetime_seconds:
            self._reconnect_count = 0

    def _report_close(self, reason: str) -> None:
        """失败上报只触发一次，避免重复关闭房间。"""
        if self._close_reported:
            return
        self._close_reported = True
        if self._on_close:
            self._on_close(utils.mask_credentials(reason))

    async def send(self, data: bytes | str) -> None:
        """串行发送数据；超时关闭底层连接以触发重连。"""
        ws = self._ws
        if ws is None:
            return
        if ws.state is not State.OPEN:
            return
        async with self._send_lock:
            if ws is not self._ws or ws.state is not State.OPEN:
                return
            try:
                await asyncio.wait_for(ws.send(data), timeout=_SEND_TIMEOUT)
            except TimeoutError:
                logger.warning(i18n.tr("弹幕发送超时（{seconds}s），判定连接已失效并触发重连", seconds=_SEND_TIMEOUT))
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            except Exception as e:
                logger.debug(
                    i18n.tr(
                        "弹幕数据发送失败（已忽略）: {type_name}: {e}",
                        type_name=type(e).__name__,
                        e=utils.mask_credentials(str(e)),
                    )
                )

    def send_nowait(self, data: bytes | str) -> None:
        """同步版发送：把 send(data) 作为任务丢进事件循环，立即返回不等待结果（供 on_ready 等同步回调使用）。"""
        ws = self._ws
        if ws is None or ws.state is not State.OPEN:
            return
        if len(self._pending) >= _MAX_PENDING_SENDS:
            logger.warning(i18n.tr("弹幕发送后台任务堆积超过 {count} 个，本次发送已丢弃", count=_MAX_PENDING_SENDS))
            return

        async def send_if_current() -> None:
            if self._ws is ws:
                await self.send(data)

        task = asyncio.create_task(send_if_current())
        self._pending.add(task)
        task.add_done_callback(self._finish_send)

    def _finish_send(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning(
                i18n.tr(
                    "WebSocket 后台发送异常: {type_name}: {error}",
                    type_name=type(error).__name__,
                    error=utils.mask_credentials(str(error)),
                )
            )

    async def _heartbeat_loop(self) -> None:
        """定期调用心跳回调，异步回调超时后关闭连接。"""
        heartbeat_timeout = max(1.0, self.heartbeat_interval + 1.0)
        while not self._stopped:
            await asyncio.sleep(self.heartbeat_interval)
            if self._stopped or self._ws is None:
                break
            if self._on_heartbeat:
                try:
                    result = self._on_heartbeat()
                    if inspect.isawaitable(result):
                        try:
                            await asyncio.wait_for(cast(Awaitable[None], result), timeout=heartbeat_timeout)
                        except TimeoutError:
                            logger.warning(
                                i18n.tr(
                                    "[WebSocket] 心跳回调超时 ({seconds}s),关闭连接以触发重连",
                                    seconds=f"{heartbeat_timeout:.0f}",
                                )
                            )
                            try:
                                if self._ws is not None:
                                    await self._ws.close()
                            except Exception:
                                pass
                            break
                except Exception:
                    pass

    async def close(self) -> None:
        """主动关闭：置停止标记（阻止后续重连与心跳）并关闭底层连接，异常忽略，无返回值。"""
        self._stopped = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def fail(self, reason: str = "") -> None:
        """关闭连接，并通过统一出口上报失败原因。"""
        await self.close()
        self._report_close(reason)
