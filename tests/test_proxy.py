"""代理 URL 解析、系统发现和 Windows 注册表句柄释放。"""

import sys
import types
from urllib.parse import unquote, urlsplit

import pytest

from src.network import proxy
from src.network.proxy import ProxyInfo, detect_system_proxy, parse_proxy_url

_ENV_KEYS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    process = types.SimpleNamespace(**vars(sys))
    process.platform = "linux"
    monkeypatch.setattr(proxy, "sys", process)
    for name in _ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.mark.parametrize(
    ("value", "host", "port", "scheme"),
    [
        ("127.0.0.1:7890", "127.0.0.1", 7890, "http"),
        ("http://localhost:1", "localhost", 1, "http"),
        ("https://proxy:65535/", "proxy", 65535, "https"),
        ("HTTP://a.example.com:3128", "a.example.com", 3128, "http"),
        ("socks5h://[::1]:1080", "::1", 1080, "socks5h"),
        ("socks4a://a.io:1080", "a.io", 1080, "socks4a"),
        ("http://[fe80::1%eth0]:8080", "fe80::1%eth0", 8080, "http"),
    ],
)
def test_parse_valid_proxy(value: str, host: str, port: int, scheme: str) -> None:
    info = parse_proxy_url(value)
    assert info is not None
    assert (info.host, info.port, info.scheme) == (host, port, scheme)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://proxy",
        "::1",
        "http://[invalid]:80",
        "http://gg:gg:80",
        "http://999.999.999.999:80",
        "http://bad!host:80",
        "http://proxy:0",
        "http://proxy:65536",
        "http://proxy:abc",
        "http://proxy:²",
        "file://proxy:80",
        "foo://proxy:80",
        "http://proxy:80/path",
        "http://proxy:80?token=x",
    ],
)
def test_invalid_proxy_returns_none(value: str) -> None:
    assert parse_proxy_url(value) is None


@pytest.mark.parametrize(("username", "password"), [("u@name", "p:/?#%word"), ("", "password"), ("user", "")])
def test_credentials_round_trip_without_double_encoding(username: str, password: str) -> None:
    info = ProxyInfo("::1", 8080, "http", username, password)
    parsed = parse_proxy_url(info.url)
    assert parsed == info
    parts = urlsplit(parsed.url)
    assert unquote(parts.username or "") == username
    assert unquote(parts.password or "") == password
    if password:
        assert password not in repr(info)


def test_proxy_info_is_immutable() -> None:
    info = ProxyInfo("127.0.0.1", 8080)
    with pytest.raises(AttributeError):
        setattr(info, "host", "other")


def test_lowercase_proxy_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.2:8080")
    assert detect_system_proxy() == ProxyInfo("127.0.0.1", 7890)


def test_proxy_falls_back_to_next_valid_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy:invalid")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://user:password@a.io:1080")
    assert detect_system_proxy() == ProxyInfo("a.io", 1080, "socks5", "user", "password")


def test_ftp_proxy_does_not_enable_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ftp_proxy", "http://127.0.0.1:2121")
    assert detect_system_proxy() is None


def test_all_proxy_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1080")
    assert detect_system_proxy() == ProxyInfo("127.0.0.1", 1080, "socks5h")


@pytest.mark.parametrize("enabled", [0, 1])
@pytest.mark.parametrize("query_fails", [False, True])
def test_windows_registry_is_closed_on_all_paths(
    monkeypatch: pytest.MonkeyPatch, enabled: int, query_fails: bool
) -> None:
    process = types.SimpleNamespace(**vars(sys))
    process.platform = "win32"
    monkeypatch.setattr(proxy, "sys", process)
    events: list[str] = []

    class RegistryKey:
        def __enter__(self) -> "RegistryKey":
            events.append("open")
            return self

        def __exit__(self, *args: object) -> None:
            events.append("close")

    def query(key: RegistryKey, name: str) -> tuple[object, int]:
        if query_fails:
            raise OSError("registry unavailable")
        return (enabled if name == "ProxyEnable" else "http=bad:port;https=[::1]:8080"), 0

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        types.SimpleNamespace(HKEY_CURRENT_USER=object(), OpenKey=lambda *args: RegistryKey(), QueryValueEx=query),
    )
    expected = ProxyInfo("::1", 8080) if enabled and not query_fails else None
    assert detect_system_proxy() == expected
    assert events == ["open", "close"]
