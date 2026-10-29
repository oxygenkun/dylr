"""URL 与 JSONP 解析函数的输入、输出契约。"""

import pytest

from src.network.urls import is_http_url, normalize_proxy_url, parse_jsonp, query_values


@pytest.mark.parametrize("url", ["http://example.com", "https://example.com:443/path", "http://[::1]:8000/"])
def test_http_url_accepts_http_schemes(url: str) -> None:
    assert is_http_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "//example.com/path",
        "http:///path",
        "https://[invalid",
        "https://example.com:bad",
        "https://example.com:0",
        "https://example.com:65536",
        "file:///etc/passwd",
        "ws://example.com/ws",
        "wss://example.com/ws",
    ],
)
def test_invalid_http_url_returns_false(url: str) -> None:
    assert is_http_url(url) is False


@pytest.mark.parametrize("body", ["[]", "null", '"text"', "42"])
def test_jsonp_requires_a_json_object(body: str) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_jsonp(f"callback({body});")


@pytest.mark.parametrize("payload", ["junk callback({});", "callback({}); trailing", "callback("])
def test_jsonp_rejects_extra_content(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_jsonp(payload)


def test_jsonp_accepts_multiline_object_and_surrounding_whitespace() -> None:
    assert parse_jsonp(' \n a.b.callback({\n"key": 1\n}); \n') == {"key": 1}


@pytest.mark.parametrize(
    ("key", "expected"), [("codec", ["h264", "h265"]), ("single", ["x"]), ("missing", []), ("empty", [])]
)
def test_query_parameter_values(key: str, expected: list[str]) -> None:
    assert query_values("https://example.com/live?codec=h264&codec=h265&single=x&empty=", key) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (" 	 ", None),
        (" 127.0.0.1:7890 ", "http://127.0.0.1:7890"),
        (" socks5://proxy:1080 ", "socks5://proxy:1080"),
    ],
)
def test_proxy_normalization_strips_outer_whitespace(value: str | None, expected: str | None) -> None:
    assert normalize_proxy_url(value) == expected
