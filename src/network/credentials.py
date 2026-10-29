"""跨房间线程复用的凭据缓存与并发请求去重。"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from src.network import http
from src.network.urls import normalize_proxy_url
from src.support import i18n, utils
from src.support.logger import logger

DEFAULT_TTL = 30 * 60
_WAIT_MARGIN = 5.0

type _Waiter[T] = tuple[asyncio.AbstractEventLoop, asyncio.Future[T | None]]


@dataclass(slots=True)
class _Inflight[T]:
    owner_loop: asyncio.AbstractEventLoop
    waiters: list[_Waiter[T]] = field(default_factory=list)
    cacheable: bool = True


def _notify[T](waiters: list[_Waiter[T]], value: T | None) -> None:
    def deliver(future: asyncio.Future[T | None], result: T | None) -> None:
        if not future.done():
            future.set_result(result)

    for loop, future in waiters:
        try:
            # Future 只能在等待者所属的事件循环里修改。
            loop.call_soon_threadsafe(deliver, future, value)
        except RuntimeError:
            pass


class CredentialCache[T]:
    """缓存成功结果，同一个键的并发拉取共用一个 factory。"""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[T | None, float]] = {}
        self._inflight: dict[str, _Inflight[T]] = {}
        self._lock = threading.Lock()

    def _reap_orphans_locked(self, key: str | None) -> list[_Waiter[T]]:
        waiters: list[_Waiter[T]] = []
        for candidate in list(self._inflight) if key is None else [key]:
            record = self._inflight.get(candidate)
            if record is not None and record.owner_loop.is_closed():
                self._inflight.pop(candidate, None)
                waiters.extend(record.waiters)
        return waiters

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            waiters = self._reap_orphans_locked(key)
            if key is None:
                self._entries.clear()
                for record in self._inflight.values():
                    record.cacheable = False
            else:
                self._entries.pop(key, None)
                record = self._inflight.get(key)
                if record is not None:
                    record.cacheable = False
        _notify(waiters, None)

    async def get_or_fetch(
        self,
        key: str,
        factory: Callable[[], Awaitable[T | None]],
        *,
        ttl: float = DEFAULT_TTL,
        wait_timeout: float = 15.0,
        cache_falsy: bool = False,
    ) -> T | None:
        """wait_timeout 只约束等待者；factory 负责自身的 I/O 超时。"""
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[T | None] | None = None
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and time.monotonic() < entry[1]:
                return entry[0]
            self._entries.pop(key, None)
            stranded = self._reap_orphans_locked(key)
            record = self._inflight.get(key)
            if record is None:
                record = _Inflight[T](owner_loop=loop)
                self._inflight[key] = record
            else:
                waiter = loop.create_future()
                record.waiters.append((loop, waiter))
        _notify(stranded, None)

        if waiter is not None:
            try:
                return await asyncio.wait_for(waiter, wait_timeout)
            except TimeoutError:
                logger.warning(
                    i18n.tr(
                        "等待其它线程的凭据拉取超时，返回空结果: {masked_key}", masked_key=utils.mask_credentials(key)
                    )
                )
                return None
            finally:
                with self._lock:
                    record.waiters[:] = [(owner, pending) for owner, pending in record.waiters if pending is not waiter]

        value: T | None = None
        try:
            try:
                value = await factory()
            except Exception as error:
                logger.warning(
                    i18n.tr(
                        "凭据拉取失败: {masked_key} - {type_name}: {e}",
                        masked_key=utils.mask_credentials(key),
                        type_name=type(error).__name__,
                        e=utils.mask_credentials(str(error)),
                    )
                )
            else:
                if value or cache_falsy:
                    with self._lock:
                        if record.cacheable and self._inflight.get(key) is record:
                            self._entries[key] = (value, time.monotonic() + ttl)
        finally:
            with self._lock:
                if self._inflight.get(key) is record:
                    self._inflight.pop(key, None)
                waiters = list(record.waiters)
            _notify(waiters, value)
        return value


cookie_cache = CredentialCache[dict[str, str]]()
credential_cache = CredentialCache[object]()


def _cookie_key(url: str, proxy_addr: str | None = None) -> str:
    normalized = url.split("?", 1)[0].rstrip("/")
    return f"{normalized}|{normalize_proxy_url(proxy_addr) or ''}"


def invalidate_cookies(url: str, proxy_addr: str | None = None) -> None:
    cookie_cache.invalidate(_cookie_key(url, proxy_addr))


async def fetch_cookies(
    url: str,
    proxy_addr: str | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = 10,
    http2: bool = False,
    ttl: int = DEFAULT_TTL,
) -> dict[str, str]:
    async def fetch() -> dict[str, str] | None:
        result = await http.request(
            url=url, proxy_addr=proxy_addr, headers=headers, return_cookies=True, timeout=timeout, http2=http2
        )
        cookies = {key: str(value) for key, value in result.items()} if isinstance(result, dict) else {}
        return cookies or None

    return (
        await cookie_cache.get_or_fetch(
            _cookie_key(url, proxy_addr), fetch, ttl=ttl, wait_timeout=timeout + _WAIT_MARGIN
        )
        or {}
    )
