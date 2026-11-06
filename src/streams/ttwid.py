import asyncio
import os
import sys
import threading
import time

from src.config.store import read_config_entry
from src.network.credentials import DEFAULT_TTL as _CACHE_TTL_SECONDS
from src.network.credentials import credential_cache, fetch_cookies, invalidate_cookies
from src.support import i18n
from src.support.logger import logger

OptionalStr = str | None

_DOUYIN_HOME_URL = "https://live.douyin.com/"


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(sys.executable))
    return os.path.split(os.path.realpath(sys.argv[0]))[0]


_cached_ttwid: str = ""
_cached_ttwid_at: float = 0.0
_cached_ttwid_by_proxy: dict[str, tuple[str, float]] = {}
_ttwid_scopes: set[tuple[str, OptionalStr]] = set()
_ttwid_lock = threading.RLock()

_CONFIG_TTWID_KEY = "ttwid"
_CONFIG_SECTION = "Cookie"

_TTWID_TTL_SECONDS: float = float(_CACHE_TTL_SECONDS)


def _proxy_slot(proxy_addr: OptionalStr) -> str:
    return proxy_addr or ""


def _cache_ttwid(value: str, proxy_addr: OptionalStr = None) -> str:
    global _cached_ttwid, _cached_ttwid_at
    now = time.monotonic()
    _cached_ttwid_by_proxy[_proxy_slot(proxy_addr)] = (value, now)
    _cached_ttwid = value
    _cached_ttwid_at = now
    return value


def _take_cached_ttwid(proxy_addr: OptionalStr) -> str:
    if not _cached_ttwid:
        return ""
    slot = _cached_ttwid_by_proxy.get(_proxy_slot(proxy_addr))
    if not slot:
        return ""
    value, fetched_at = slot
    if (time.monotonic() - fetched_at) >= _TTWID_TTL_SECONDS:
        _cached_ttwid_by_proxy.pop(_proxy_slot(proxy_addr), None)
        return ""
    return value


def _read_config_ttwid() -> str:
    try:
        raw = (read_config_entry(f"{_app_root()}/config/config.toml", _CONFIG_SECTION, _CONFIG_TTWID_KEY) or "").strip()
    except Exception:
        # 配置异常不得阻断录制启动：视同未配置，走动态获取。
        return ""
    if not raw:
        return ""
    return raw if raw.lower().startswith("ttwid=") else f"ttwid={raw}"


async def _fetch_ttwid(proxy_addr: OptionalStr = None) -> str:
    try:
        cookies_dict = await fetch_cookies(
            url=_DOUYIN_HOME_URL,
            proxy_addr=proxy_addr,
            headers={
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            },
            timeout=10,
            http2=False,  # 抖音对 HTTP/2 支持不稳定，常触发 ReadError('')，降级到 HTTP/1.1
        )
        if isinstance(cookies_dict, dict) and cookies_dict.get("ttwid"):
            logger.debug("自动获取抖音 ttwid 成功")
            return _cache_ttwid(f"ttwid={cookies_dict['ttwid']}", proxy_addr)
    except Exception as e:
        logger.warning(i18n.tr("自动获取抖音 ttwid 失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
    return ""


async def get_ttwid(proxy_addr: OptionalStr = None) -> str:
    cached = _take_cached_ttwid(proxy_addr)
    if cached:
        return cached

    cfg = _read_config_ttwid()
    if cfg:
        _cache_ttwid(cfg, proxy_addr)
        logger.debug("使用配置文件中的 ttwid")
        return cfg

    async def _fetch() -> str:
        return await _fetch_ttwid(proxy_addr)

    key = f"douyin_ttwid|{proxy_addr or ''}"
    _ttwid_scopes.add((key, proxy_addr))
    got = await credential_cache.get_or_fetch(key=key, factory=_fetch, wait_timeout=15)
    if isinstance(got, str) and got:
        _cache_ttwid(got, proxy_addr)
    return _take_cached_ttwid(proxy_addr)


def invalidate_ttwid(proxy_addr: OptionalStr = None) -> None:
    """显式作废进程内的 ttwid（与 spider.invalidate_bili_buvid_cache 同形）：抖音侧拒绝该凭据。"""
    global _cached_ttwid, _cached_ttwid_at
    _cached_ttwid = ""
    _cached_ttwid_at = 0.0
    _cached_ttwid_by_proxy.clear()
    scopes = set(_ttwid_scopes)
    if proxy_addr is not None:
        scopes.add((f"douyin_ttwid|{proxy_addr or ''}", proxy_addr or None))
    for key, scope_proxy in scopes:
        credential_cache.invalidate(key)
        invalidate_cookies(_DOUYIN_HOME_URL, scope_proxy)


def warmup_ttwid(proxy_addr: OptionalStr = None) -> None:
    try:
        asyncio.run(get_ttwid(proxy_addr))
    except Exception as e:
        logger.warning(i18n.tr("启动时预热 ttwid 失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
