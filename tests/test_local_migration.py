# Verify the connection points introduced when porting the old local modifications.
# Bark pushes use the real notification dispatcher and a fake outbound Bark function.
# Both start and end messages share that dispatcher, so routing stays consistent.
# The room key must ignore query strings, fragments and trailing slashes.
# A room with a longer identifier must not match a shorter room's rule.
# Matching the path alone must not let a different host inherit the override.
# Invalid interruption levels fall back to the global setting.
# Follow tests run the fork's actual platform resolver and preserve its context object.
# Only platform network calls and stream extraction are replaced by asynchronous fakes.
# The monitor cache and freshness logic remain the production implementations.
# Cached room hints must bypass both room network requests and request-rate delays.
# Internal room IDs must survive into the optional danmaku arguments.
# A missing feed entry must use the existing web resolver exactly once.
# That fallback retains the original rate limiter rather than flooding room APIs.
# Configuration and monitor state are restored by monkeypatch after every test.
# All example stream URLs are inert and never requested by these tests.
# Exercise the migrated hooks through the fork's real dispatch functions without requests.
import threading
from typing import Any

import pytest

from src.monitors.douyin_follow import DouyinFollowMonitor
from src.notifications import notify
from src.recording import state
from src.streams import dispatch as platform_dispatch


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://live.douyin.com/123", "timeSensitive"),
        ("https://live.douyin.com/123/?q=1#fragment", "timeSensitive"),
        ("https://live.douyin.com/1234", "active"),
        ("https://other.example/123", "active"),
    ],
)
def test_bark_dispatch(monkeypatch: pytest.MonkeyPatch, url: str, expected: str) -> None:
    calls: list[str] = []

    def fake_bark(*args: Any, **kwargs: Any) -> dict[str, list[str]]:
        calls.append(kwargs["level"])
        return {"success": ["test"], "error": []}

    monkeypatch.setattr(state, "bark_level_overrides", {"live.douyin.com/123": "timeSensitive"})
    monkeypatch.setattr(state, "live_status_push", "BARK")
    monkeypatch.setattr(state, "bark_msg_level", "active")
    monkeypatch.setattr(notify, "bark", fake_bark)
    notify.push_message("test", url, "test")
    assert calls == [expected]
    assert notify.get_bark_level(url, "passive", {"live.douyin.com/123": "invalid"}) == "passive"


def test_follow_resolver_cache_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    room = {
        "anchor_name": "test",
        "id_str": "999",
        "status": 2,
        "stream_url": {
            "flv_pull_url": {"ORIGIN": "https://example.test/live.flv"},
            "hls_pull_url_map": {"ORIGIN": "https://example.test/live.m3u8"},
        },
    }
    monitor = DouyinFollowMonitor(threading.Event(), fetch=lambda *a: {}, log=lambda *a, **k: None)
    monitor.configure(True, "test-cookie")
    with monitor._lock:
        monitor._rooms = {"123": room}
        monitor._last_success = monitor._clock()
        monitor._revision = 1
    monkeypatch.setattr(platform_dispatch, "follow_monitor", monitor)
    monkeypatch.setattr(state, "semaphore", threading.Semaphore(1))
    requests: list[str] = []
    rate_calls: list[bool] = []

    async def fake_web(**kwargs: Any) -> dict[str, Any]:
        requests.append(kwargs["url"])
        return room

    async def fake_stream(data: dict[str, Any], *args: Any) -> dict[str, Any]:
        return {"is_live": data["status"] == 2, "anchor_name": data["anchor_name"]}

    monkeypatch.setattr(platform_dispatch.spider, "get_douyin_web_stream_data", fake_web)
    monkeypatch.setattr(platform_dispatch.stream, "get_douyin_stream_url", fake_stream)
    monkeypatch.setattr(platform_dispatch, "_douyin_rate_limit", lambda: rate_calls.append(True))
    ctx = platform_dispatch._PlatformResolveContext("https://live.douyin.com/123", None, "OD")
    platform_dispatch._resolve_douyin_com(ctx)
    assert ctx.port_info["is_live"] is True
    assert ctx.record_danmaku_args is not None
    assert ctx.record_danmaku_args["room_id"] == "999"
    assert not requests and not rate_calls
    ctx = platform_dispatch._PlatformResolveContext("https://live.douyin.com/456", None, "OD")
    platform_dispatch._resolve_douyin_com(ctx)
    assert requests == ["https://live.douyin.com/456"]
    assert rate_calls == [True]
