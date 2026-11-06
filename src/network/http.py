"""异步 HTTP 请求、按事件循环复用的连接池与有界响应读取。"""

import asyncio
import contextlib
import re
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import cast

import httpx

from src.network import policy
from src.network import urls as network_urls
from src.network.targets import UnsafeTarget, stream_target_reason, validate_http_target
from src.support import i18n, utils
from src.support.logger import logger

_CLIENT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)

type _CacheKey = tuple[str, bool, bool, asyncio.AbstractEventLoop]
type _CacheValue = tuple[httpx.AsyncClient, asyncio.AbstractEventLoop, threading.Thread]
_client_cache: dict[_CacheKey, _CacheValue] = {}
_client_cache_lock = threading.RLock()
_CACHE_SWEEP_THRESHOLD = 32


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 未压缩响应体读入上限
_MAX_DECOMPRESSED_BYTES = 32 * 1024 * 1024  # 解压后产出上限
_RESPONSE_CHUNK_SIZE = 64 * 1024


_MISSING_PACKAGE_PATTERN = re.compile(r"the '([\w.\-]+)' package")
_dep_warned: set[str] = set()


async def _bounded_response_read(response: httpx.Response) -> None:
    if hasattr(response, "_content"):
        return
    content_encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    compressed = any(token.strip() not in ("", "identity") for token in content_encoding.split(","))
    limit = _MAX_DECOMPRESSED_BYTES if compressed else _MAX_RESPONSE_BYTES
    kind = "解压后响应体" if compressed else "响应体"
    collected: list[bytes] = []
    total = 0
    async with contextlib.aclosing(
        cast(AsyncGenerator[bytes, None], response.aiter_bytes(_RESPONSE_CHUNK_SIZE))
    ) as byte_chunks:
        async for chunk in byte_chunks:
            total += len(chunk)
            if total > limit:
                raise ValueError(f"async 出站{kind}超过上限 {limit} 字节，已拒绝该响应")
            collected.append(chunk)
    body = b"".join(collected)
    response._content = body
    if response.content is not body:
        raise ValueError("httpx Response 装载形态与本模块回填约定不再一致（_content 漂移），已拒绝该响应")


def _request_guard(proxy_addr: str | None) -> Callable[[httpx.Request], Awaitable[None]]:
    async def guard(request: httpx.Request) -> None:
        validate_http_target(str(request.url), proxy_addr)

    return guard


def _sweep_closed_loop_entries(current_loop: asyncio.AbstractEventLoop) -> None:
    if len(_client_cache) <= _CACHE_SWEEP_THRESHOLD:
        return
    for key, (_client, loop, _owner) in list(_client_cache.items()):
        if loop is current_loop or not loop.is_closed():
            continue
        _client_cache.pop(key, None)


def _build_client(
    proxy_addr: str | None,
    timeout: int,
    verify: bool,
    http2: bool,
) -> httpx.AsyncClient:
    # 代理只认显式入参：trust_env 会解析 NO_PROXY，`[::1]` 这类括号写法会让 httpx 在
    # 构造客户端时抛 InvalidURL，且会绕过「是否使用代理ip」让请求偷偷走环境代理。
    return httpx.AsyncClient(
        proxy=proxy_addr,
        timeout=timeout,
        verify=verify,
        http2=http2,
        limits=_CLIENT_LIMITS,
        trust_env=False,
        event_hooks={"request": [_request_guard(proxy_addr)], "response": [_bounded_response_read]},
    )


async def _get_client(
    proxy_addr: str | None,
    timeout: int,
    verify: bool,
    http2: bool,
) -> httpx.AsyncClient:
    current_loop = asyncio.get_running_loop()
    current_thread = threading.current_thread()
    key: _CacheKey = (proxy_addr or "", verify, http2, current_loop)
    with _client_cache_lock:
        _sweep_closed_loop_entries(current_loop)
        cached = _client_cache.get(key)
    if cached is not None:
        client, client_loop, _owner = cached
        if not client.is_closed and client_loop is current_loop:
            return client
        stale = False
        with _client_cache_lock:
            if _client_cache.get(key) is cached:
                _client_cache.pop(key, None)
                stale = True
        if stale and not client.is_closed:
            try:
                await client.aclose()
            except Exception as e:
                logger.debug(i18n.tr("关闭失效 AsyncClient 失败: {e}", e=utils.mask_credentials(str(e))))

    new_client = _build_client(proxy_addr, timeout, verify, http2)
    reused: httpx.AsyncClient | None = None
    with _client_cache_lock:
        winner = _client_cache.get(key)
        if winner is not None and not winner[0].is_closed and winner[1] is current_loop:
            reused = winner[0]
        else:
            _client_cache[key] = (new_client, current_loop, current_thread)
    if reused is not None:
        try:
            await new_client.aclose()
        except Exception as e:
            logger.debug(i18n.tr("关闭并发重复创建的 AsyncClient 失败: {e}", e=utils.mask_credentials(str(e))))
        return reused
    return new_client


@asynccontextmanager
async def _client(
    proxy_addr: str | None,
    timeout: int,
    verify: bool,
    http2: bool,
    isolated: bool,
) -> AsyncIterator[httpx.AsyncClient]:
    client = (
        _build_client(proxy_addr, timeout, verify, http2)
        if isolated
        else await _get_client(proxy_addr, timeout, verify, http2)
    )
    try:
        yield client
    finally:
        if isolated:
            await _release_client(client)


async def _release_client(client: httpx.AsyncClient) -> None:
    try:
        if not client.is_closed:
            await client.aclose()
    except Exception as e:
        logger.debug(i18n.tr("关闭失效 AsyncClient 失败: {e}", e=utils.mask_credentials(f"{type(e).__name__}: {e}")))


async def _close_owned_clients() -> None:
    current_loop = asyncio.get_running_loop()
    current_thread = threading.current_thread()
    with _client_cache_lock:
        entries = list(_client_cache.values())
        _client_cache.clear()
    closed = 0
    for client, client_loop, owner in entries:
        if client_loop is not current_loop or owner is not current_thread:
            continue
        try:
            if not client.is_closed:
                await client.aclose()
                closed += 1
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "进程退出释放 AsyncClient 失败: {type_name}: {e}",
                    type_name=type(e).__name__,
                    e=utils.mask_credentials(str(e)),
                )
            )
    skipped = len(entries) - closed
    if skipped:
        logger.debug(i18n.tr("进程退出跳过 {count} 个非本线程/本循环的 AsyncClient（只丢引用）", count=skipped))


def close_clients() -> None:
    with _client_cache_lock:
        if not _client_cache:
            return
    try:
        loop: asyncio.AbstractEventLoop | None
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            raise RuntimeError("loop closed")
        if loop.is_running():
            with _client_cache_lock:
                _client_cache.clear()
            return
        loop.run_until_complete(_close_owned_clients())
    except Exception as e:
        logger.debug(i18n.tr("close_clients 回退到引用清理: {e}", e=utils.mask_credentials(str(e))))
        with _client_cache_lock:
            _client_cache.clear()


def _failure_result(
    redirect_url: bool, return_cookies: bool, include_cookies: bool
) -> str | dict[str, str] | tuple[str, dict[str, str]]:
    if redirect_url:
        return ""
    if return_cookies:
        return ("", {}) if include_cookies else {}
    return ""


def _missing_dependency_name(err: BaseException) -> str:
    name = getattr(err, "name", None)
    if isinstance(name, str) and name:
        return name
    match = _MISSING_PACKAGE_PATTERN.search(str(err))
    return match.group(1) if match else ""


def _log_dependency_missing(err: BaseException, url: str) -> None:
    module = _missing_dependency_name(err)
    scope = module or type(err).__name__
    masked_url = utils.mask_credentials(url)
    if scope in _dep_warned:
        logger.debug(
            i18n.tr(
                "request 可选依赖仍缺失（已告警过）: {masked_url} - {type_name}: {e} - 缺失模块 {module}",
                masked_url=masked_url,
                type_name=type(err).__name__,
                e=utils.mask_credentials(str(err)),
                module=module,
            )
        )
        return
    _dep_warned.add(scope)
    logger.warning(
        i18n.tr(
            "request 缺少可选依赖，请求根本没有发出（本轮按空响应返回，非风控）: {masked_url} - {type_name}: {e}"
            " - 缺失模块 {module}，请执行 uv sync --locked",
            masked_url=masked_url,
            type_name=type(err).__name__,
            e=utils.mask_credentials(str(err)),
            module=module,
        )
    )


async def request(
    url: str,
    *,
    proxy_addr: str | None = None,
    headers: Mapping[str, str] | None = None,
    data: Mapping[str, object] | str | bytes | bytearray | memoryview | None = None,
    json_data: Mapping[str, object] | Sequence[object] | None = None,
    timeout: int = 20,
    redirect_url: bool = False,
    return_cookies: bool = False,
    include_cookies: bool = False,
    content_encoding: str = "utf-8",
    verify: bool | None = None,
    http2: bool = True,
) -> str | dict[str, str] | tuple[str, dict[str, str]]:
    if headers is None:
        headers = {}
    if not network_urls.is_http_url(url):
        logger.warning(
            i18n.tr(
                "request 拒绝非白名单协议的请求: {masked_url}",
                masked_url=utils.mask_credentials(url),
            )
        )
        return _failure_result(redirect_url, return_cookies, include_cookies)
    if data is not None and json_data is not None:
        raise ValueError(
            i18n.tr(
                "request 不接受同时传 data 与 json_data: {masked_url}",
                masked_url=utils.mask_credentials(url),
            )
        )
    if verify is None:
        verify = policy.verify_tls
    try:
        proxy_addr = network_urls.normalize_proxy_url(proxy_addr)
        async with _client(proxy_addr, timeout, verify, http2, return_cookies) as client:
            if data is not None or json_data is not None:
                if isinstance(data, (str, bytes, bytearray, memoryview)):
                    content_data = bytes(data) if isinstance(data, (bytearray, memoryview)) else data
                    response = await client.post(
                        url,
                        content=content_data,
                        json=json_data,
                        headers=headers,
                        timeout=timeout,
                        follow_redirects=True,
                    )
                else:
                    response = await client.post(
                        url,
                        data=data,
                        json=json_data,
                        headers=headers,
                        timeout=timeout,
                        follow_redirects=True,
                    )
            else:
                response = await client.get(url, headers=headers, follow_redirects=True, timeout=timeout)

            charset = getattr(response, "charset_encoding", None)
            if content_encoding and not charset:
                response.encoding = content_encoding
            if redirect_url:
                return str(response.url)
            if return_cookies:
                cookies_dict = dict(response.cookies.items())
                return (response.text, cookies_dict) if include_cookies else cookies_dict
            resp_str = response.text
    except (ImportError, RuntimeError) as e:
        if _missing_dependency_name(e):
            _log_dependency_missing(e, url)
            return _failure_result(redirect_url, return_cookies, include_cookies)
        logger.debug(
            i18n.tr(
                "request 请求失败: {masked_url} - {type_name}: {e}",
                masked_url=utils.mask_credentials(url),
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
        return _failure_result(redirect_url, return_cookies, include_cookies)
    except UnsafeTarget as e:
        logger.warning(
            i18n.tr(
                "request 请求失败: {masked_url} - {type_name}: {e}",
                masked_url=utils.mask_credentials(url),
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
        return _failure_result(redirect_url, return_cookies, include_cookies)
    except Exception as e:
        logger.debug(
            i18n.tr(
                "request 请求失败: {masked_url} - {type_name}: {e}",
                masked_url=utils.mask_credentials(url),
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
        return _failure_result(redirect_url, return_cookies, include_cookies)

    return resp_str


async def is_reachable(
    url: str,
    *,
    proxy_addr: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: int = 10,
    verify: bool | None = None,
    http2: bool = False,
    platform: str = "",
) -> bool:
    if not network_urls.is_http_url(url):
        logger.warning(i18n.tr("is_reachable 拒绝非白名单协议的请求: {url}", url=utils.mask_credentials(url)))
        return False
    proxy_addr = network_urls.normalize_proxy_url(proxy_addr)
    internal_reason = stream_target_reason(url, proxy_addr)
    if internal_reason is not None:
        logger.warning(
            i18n.tr(
                "is_reachable 拒绝内网/保留目标（疑似被篡改的流地址）: {masked_url} - {reason}",
                masked_url=utils.mask_credentials(url),
                reason=internal_reason,
            )
        )
        return False
    if verify is None:
        verify = policy.should_verify_stream_tls(platform)
    try:
        client: httpx.AsyncClient = await _get_client(proxy_addr, timeout, verify, http2)
        response = await client.head(url, headers=headers, follow_redirects=True, timeout=timeout)
        if response.status_code == 200:
            return True
        if ".m3u8" in url.lower():
            probe = await client.get(
                url, headers={**(headers or {}), "Range": "bytes=0-0"}, follow_redirects=True, timeout=timeout
            )
            if probe.status_code in (200, 206):
                return True
            logger.debug(
                i18n.tr(
                    "is_reachable 校验未通过: {url} - HEAD={status_code}, Range-GET={status_code_2}, content-type={content_type}",
                    url=utils.mask_credentials(url),
                    status_code=response.status_code,
                    status_code_2=probe.status_code,
                    content_type=probe.headers.get("content-type", ""),
                )
            )
            return False
        logger.debug(
            i18n.tr(
                "is_reachable 校验未通过: {url} - status_code={status_code}, content-type={content_type}",
                url=utils.mask_credentials(url),
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
            )
        )
        return False
    except UnsafeTarget as e:
        logger.warning(
            i18n.tr(
                "is_reachable 拒绝内网/保留目标（疑似被篡改的流地址）: {masked_url} - {reason}",
                masked_url=utils.mask_credentials(e.url),
                reason=f"重定向逐跳复检命中: {e.reason}（初始地址 {utils.mask_credentials(url)}）",
            )
        )
        return False
    except Exception as e:
        logger.debug(
            i18n.tr(
                "is_reachable 校验失败（判定为不可达）: {url} - {type_name}: {e}",
                url=utils.mask_credentials(url),
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
    return False
