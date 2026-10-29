"""共享目标判定与请求发送前的重定向校验。"""

import httpx
import pytest

from src.network import http, targets
from src.streams import selection


@pytest.mark.parametrize("text", ["127.1", "2130706433", "0x7f000001", "0177.0.0.1", "[::1]"])
def test_alternate_loopback_literals_are_rejected(text: str) -> None:
    address = targets.parse_ip_literal(text)
    assert address is not None and address.is_loopback
    assert targets.host_rejection_reason(text) is not None


def test_proxied_domains_skip_local_dns_but_private_literals_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str) -> list[str]:
        raise AssertionError("proxied domains must use the proxy resolver")

    monkeypatch.setattr(targets, "resolve_host_ips", resolve)
    targets.validate_http_target("https://cdn.example.com/live", "http://127.0.0.1:7890")
    with pytest.raises(targets.UnsafeTarget):
        targets.validate_http_target("http://169.254.169.254/", "http://127.0.0.1:7890")


@pytest.mark.parametrize("client_kind", ["async", "sync"])
@pytest.mark.parametrize("destination", ["http://127.0.0.1:6379/", "http://user:SecretPassword@169.254.169.254/"])
async def test_internal_redirect_is_rejected_before_transport(
    monkeypatch: pytest.MonkeyPatch, client_kind: str, destination: str
) -> None:
    monkeypatch.setattr(targets, "resolve_host_ips", lambda host: ["93.184.216.34"])
    seen: list[str] = []
    start = "https://cdn.example.com/live"

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": destination})

    transport = httpx.MockTransport(handle)
    if client_kind == "async":
        async with http._build_client(None, 5, True, False) as client:
            client._mounts = {}
            client._transport = transport
            with pytest.raises(targets.UnsafeTarget) as error:
                await client.get(start, follow_redirects=True)
    else:
        with selection._probe_client(5, None, True) as client:
            client._mounts = {}
            client._transport = transport
            with pytest.raises(targets.UnsafeTarget) as error:
                client.get(start, follow_redirects=True)
    assert seen == [start]
    assert "SecretPassword" not in str(error.value)


@pytest.mark.parametrize("client_kind", ["async", "sync"])
async def test_public_redirect_still_reaches_destination(monkeypatch: pytest.MonkeyPatch, client_kind: str) -> None:
    monkeypatch.setattr(targets, "resolve_host_ips", lambda host: ["93.184.216.34"])
    seen: list[str] = []
    start, destination = "https://start.example.com/live", "https://final.example.com/live"

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == start:
            return httpx.Response(302, headers={"location": destination})
        return httpx.Response(200, text="OK")

    if client_kind == "async":
        async with http._build_client(None, 5, True, False) as client:
            client._mounts = {}
            client._transport = httpx.MockTransport(handle)
            response = await client.get(start, follow_redirects=True)
    else:
        with selection._probe_client(5, None, True) as client:
            client._mounts = {}
            client._transport = httpx.MockTransport(handle)
            response = client.get(start, follow_redirects=True)
    assert response.text == "OK"
    assert seen == [start, destination]


@pytest.mark.parametrize(
    "destination, body",
    [
        ("https://final.example.com/live", b"x" * 65_537),
        ("http://127.0.0.1:6379/live", b""),
    ],
    ids=["oversized-body", "internal-target"],
)
@pytest.mark.parametrize("record_fallback", [False, True])
def test_rejected_redirect_cannot_use_last_resort(
    monkeypatch: pytest.MonkeyPatch, destination: str, body: bytes, record_fallback: bool
) -> None:
    monkeypatch.setattr(targets, "resolve_host_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(selection.state, "hls_collection_enabled", True)
    monkeypatch.setattr(selection, "_throttle_probe", lambda url: None)
    start = "https://probeUser:probePassword@start.example.com/live.m3u8?token=probeSecret"
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(302, headers={"location": destination}, content=body)

    build_client = selection._probe_client

    def build(timeout: int, proxy_addr: str | None, verify: bool) -> httpx.Client:
        client = build_client(timeout, proxy_addr, verify)
        client._mounts = {}
        client._transport = httpx.MockTransport(handle)
        return client

    monkeypatch.setattr(selection, "_probe_client", build)
    rejected: set[str] = set()
    messages: list[str] = []
    handler_id = selection.logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    try:
        if record_fallback:
            assert selection.select_source_url({"m3u8_url": start, "record_url": start}) is None
        else:
            assert selection._validate_stream_url(start, last_resort=True, security_rejected=rejected) is False
            assert start in rejected
    finally:
        selection.logger.remove(handler_id)
    assert seen == [start, start]
    logs = "\n".join(messages)
    assert "***" in logs
    assert all(secret not in logs for secret in ("probeUser", "probePassword", "probeSecret"))
