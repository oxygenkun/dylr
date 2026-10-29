"""凭据缓存的 TTL、跨循环去重、失效与 Cookie 请求契约。"""

import asyncio
import threading
import time
import types
from collections.abc import Iterator

import pytest

from src.network import credentials
from src.network.credentials import CredentialCache, _cookie_key, _Inflight


@pytest.fixture(autouse=True)
def clean_caches() -> Iterator[None]:
    credentials.cookie_cache.invalidate()
    credentials.credential_cache.invalidate()
    yield
    credentials.cookie_cache.invalidate()
    credentials.credential_cache.invalidate()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    current = [1000.0]
    shim = types.SimpleNamespace(**vars(time))
    shim.monotonic = lambda: current[0]
    monkeypatch.setattr(credentials, "time", shim)
    return current


def test_cookie_key_separates_network_exits() -> None:
    assert _cookie_key("https://example.com/?room=1") == _cookie_key("https://example.com/?room=2")
    assert _cookie_key("https://example.com") != _cookie_key("https://example.com", "http://127.0.0.1:7890")
    assert _cookie_key("https://example.com", "127.0.0.1:7890") == _cookie_key(
        "https://example.com", "http://127.0.0.1:7890"
    )


async def test_cache_uses_written_expiry(clock: list[float]) -> None:
    cache = CredentialCache[str]()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    assert await cache.get_or_fetch("key", factory, ttl=60) == "value-1"
    clock[0] += 59
    assert await cache.get_or_fetch("key", factory, ttl=1) == "value-1"
    clock[0] += 2
    assert await cache.get_or_fetch("key", factory) == "value-2"


@pytest.mark.parametrize("value", [None, "", 0, False])
@pytest.mark.parametrize("cache_falsy", [True, False])
async def test_falsy_success_policy(value: object, cache_falsy: bool) -> None:
    cache = CredentialCache[object]()
    calls = 0

    async def factory() -> object:
        nonlocal calls
        calls += 1
        return value

    assert await cache.get_or_fetch("key", factory, cache_falsy=cache_falsy) == value
    assert await cache.get_or_fetch("key", factory, cache_falsy=cache_falsy) == value
    assert calls == (1 if cache_falsy else 2)


async def test_failed_fetch_is_not_cached_even_with_cache_falsy() -> None:
    cache = CredentialCache[str]()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary failure")
        return "fresh"

    assert await cache.get_or_fetch("key", factory, cache_falsy=True) is None
    assert await cache.get_or_fetch("key", factory, cache_falsy=True) == "fresh"
    assert calls == 2


async def test_same_loop_concurrent_requests_share_one_fetch() -> None:
    cache = CredentialCache[str]()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "shared"

    assert await asyncio.gather(*(cache.get_or_fetch("key", factory) for _ in range(5))) == ["shared"] * 5
    assert calls == 1


def test_room_threads_share_one_fetch() -> None:
    cache = CredentialCache[str]()
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    results: list[str | None] = []
    errors: list[BaseException] = []

    async def factory() -> str:
        calls.append(threading.get_ident())
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return "shared"

    def worker() -> None:
        try:
            results.append(asyncio.run(cache.get_or_fetch("key", factory)))
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=worker)
    waiter = threading.Thread(target=worker)
    owner.start()
    try:
        assert entered.wait(timeout=5)
        waiter.start()
        deadline = time.monotonic() + 5
        registered = False
        while time.monotonic() < deadline:
            with cache._lock:
                registered = bool(cache._inflight["key"].waiters)
            if registered:
                break
            threading.Event().wait(0.001)
        assert registered
        release.set()
    finally:
        release.set()
        owner.join(timeout=5)
        if waiter.ident is not None:
            waiter.join(timeout=5)
        assert not owner.is_alive()
        assert not waiter.is_alive()
    assert not errors
    assert calls and len(calls) == 1
    assert results == ["shared", "shared"]


async def test_invalidation_keeps_waiters_but_prevents_stale_writeback() -> None:
    cache = CredentialCache[str]()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> str:
        entered.set()
        await release.wait()
        return "stale"

    owner = asyncio.create_task(cache.get_or_fetch("key", factory))
    await asyncio.wait_for(entered.wait(), timeout=5)
    waiter = asyncio.create_task(cache.get_or_fetch("key", factory))
    await asyncio.sleep(0)
    cache.invalidate("key")
    release.set()
    assert await asyncio.wait_for(owner, timeout=5) == "stale"
    assert await asyncio.wait_for(waiter, timeout=5) == "stale"
    assert "key" not in cache._entries


@pytest.mark.parametrize("invalidate", [True, False])
async def test_orphan_reclaim_wakes_waiters(invalidate: bool) -> None:
    cache = CredentialCache[str]()
    dead = asyncio.new_event_loop()
    dead.close()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    cache._inflight["key"] = _Inflight(owner_loop=dead, waiters=[(loop, future)])

    async def factory() -> str:
        return "fresh"

    if invalidate:
        cache.invalidate("key")
    else:
        assert await cache.get_or_fetch("key", factory) == "fresh"
    assert await asyncio.wait_for(future, timeout=5) is None
    assert "key" not in cache._inflight


async def test_closed_waiter_loop_does_not_break_cleanup() -> None:
    cache = CredentialCache[str]()
    dead = asyncio.new_event_loop()
    future = dead.create_future()
    dead.close()
    cache._inflight["key"] = _Inflight(owner_loop=dead, waiters=[(dead, future)])
    cache.invalidate()
    assert not cache._inflight


async def test_cancellation_releases_owner_and_waiters() -> None:
    cache = CredentialCache[str]()
    entered = asyncio.Event()

    async def factory() -> str:
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    owner = asyncio.create_task(cache.get_or_fetch("key", factory))
    await asyncio.wait_for(entered.wait(), timeout=5)
    waiter = asyncio.create_task(cache.get_or_fetch("key", factory))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await asyncio.wait_for(waiter, timeout=5) is None
    assert not cache._inflight


async def test_waiter_timeout_detaches_without_cancelling_owner() -> None:
    cache = CredentialCache[str]()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> str:
        entered.set()
        await release.wait()
        return "fresh"

    owner = asyncio.create_task(cache.get_or_fetch("key", factory))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        assert await cache.get_or_fetch("key", factory, wait_timeout=0) is None
        assert not cache._inflight["key"].waiters
    finally:
        release.set()
        assert await asyncio.wait_for(owner, timeout=5) == "fresh"


async def test_late_owner_does_not_remove_takeover() -> None:
    cache = CredentialCache[str]()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> str:
        entered.set()
        await release.wait()
        return "old"

    owner = asyncio.create_task(cache.get_or_fetch("key", factory))
    await asyncio.wait_for(entered.wait(), timeout=5)
    takeover = _Inflight[str](owner_loop=asyncio.get_running_loop())
    cache._inflight["key"] = takeover
    release.set()
    await asyncio.wait_for(owner, timeout=5)
    assert cache._inflight["key"] is takeover
    cache._inflight.clear()


@pytest.mark.parametrize(("result", "expected"), [({"did": 123}, {"did": "123"}), ({}, {}), ("invalid", {})])
async def test_cookie_fetch_contract(monkeypatch: pytest.MonkeyPatch, result: object, expected: dict[str, str]) -> None:
    calls: list[dict[str, object]] = []

    async def request(**kwargs: object) -> object:
        calls.append(kwargs)
        return result

    monkeypatch.setattr(credentials.http, "request", request)
    assert await credentials.fetch_cookies("https://example.com/", headers={"User-Agent": "test"}) == expected
    assert calls == [
        {
            "url": "https://example.com/",
            "proxy_addr": None,
            "headers": {"User-Agent": "test"},
            "return_cookies": True,
            "timeout": 10,
            "http2": False,
        }
    ]


async def test_cookie_invalidation_is_scoped_to_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def request(**kwargs: object) -> dict[str, str]:
        return {"did": str(kwargs["proxy_addr"])}

    monkeypatch.setattr(credentials.http, "request", request)
    await credentials.fetch_cookies("https://example.com/", "http://127.0.0.1:7890")
    await credentials.fetch_cookies("https://example.com/")
    credentials.invalidate_cookies("https://example.com/")
    assert _cookie_key("https://example.com/") not in credentials.cookie_cache._entries
    assert _cookie_key("https://example.com/", "http://127.0.0.1:7890") in credentials.cookie_cache._entries


async def test_failure_logs_mask_url_and_proxy_credentials() -> None:
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)))
    try:

        async def factory() -> str:
            raise OSError("https://example.com/?token=SecretToken via http://user:SecretPassword@proxy.example:8080")

        assert await CredentialCache[str]().get_or_fetch("https://example.com/?token=KeyToken", factory) is None
    finally:
        logger.remove(sink)
    text = "".join(lines)
    assert "SecretToken" not in text and "SecretPassword" not in text and "KeyToken" not in text
    assert "OSError" in text and "***" in text


async def test_invalidating_other_key_does_not_discard_fresh_result() -> None:
    cache = CredentialCache[str]()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> str:
        entered.set()
        await release.wait()
        return "fresh"

    owner = asyncio.create_task(cache.get_or_fetch("kept", factory))
    await asyncio.wait_for(entered.wait(), timeout=5)
    cache.invalidate("other")
    release.set()
    assert await asyncio.wait_for(owner, timeout=5) == "fresh"
    assert cache._entries["kept"][0] == "fresh"
