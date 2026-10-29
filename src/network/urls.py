"""URL、代理、Cookie 与 JSONP 格式处理。"""

import json
import re
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse


def is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and (port is None or port > 0)
    except ValueError:
        return False


def format_cookies(cookies_dict: Mapping[str, object]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies_dict.items())


def normalize_proxy_url(proxy_addr: str | None) -> str | None:
    value = proxy_addr.strip() if proxy_addr else ""
    if not value:
        return None
    return value if "://" in value else "http://" + value


def parse_jsonp(jsonp_str: str) -> dict[str, object]:
    match = re.fullmatch(r"[\w.]+\((.*)\);?", jsonp_str.strip(), re.DOTALL)
    if match is None:
        raise ValueError("No JSON data found in JSONP response.")
    value = json.loads(match.group(1))
    if not isinstance(value, dict):
        raise ValueError("JSONP response must contain a JSON object.")
    return value


def query_values(url: str, param_name: str) -> list[str]:
    return parse_qs(urlparse(url).query).get(param_name, [])
