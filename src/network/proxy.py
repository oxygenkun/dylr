"""系统代理发现与代理 URL 解析。"""

import ipaddress
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from src.support import i18n, utils
from src.support.logger import logger

_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h", "socks4", "socks4a"})
_PROXY_ENV_NAMES = ("http_proxy", "https_proxy", "all_proxy")
_WINDOWS_PROXY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")


@dataclass(frozen=True, slots=True)
class ProxyInfo:
    host: str
    port: int
    scheme: str = "http"
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        auth = ""
        if self.username or self.password:
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return f"{self.scheme}://{auth}{host}:{self.port}"


def parse_proxy_url(value: str) -> ProxyInfo | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw if "://" in raw else "http://" + raw)
        host = parsed.hostname
        port = parsed.port
        if parsed.scheme not in _PROXY_SCHEMES or not host or not port:
            return None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host = host.rstrip(".").encode("idna").decode("ascii")
            if len(host) > 253 or host.replace(".", "").isdigit():
                return None
            if not all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
                return None
        return ProxyInfo(
            host,
            port,
            parsed.scheme,
            unquote(parsed.username or "", errors="strict"),
            unquote(parsed.password or "", errors="strict"),
        )
    except ValueError, UnicodeError:
        return None


def _windows_proxy_values() -> list[str]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_PROXY_KEY) as key:
            if winreg.QueryValueEx(key, "ProxyEnable")[0] != 1:
                return []
            value = str(winreg.QueryValueEx(key, "ProxyServer")[0])
    except OSError as error:
        logger.warning(
            i18n.tr(
                "读取代理注册表项失败: {type_name}: {e}",
                type_name=type(error).__name__,
                e=utils.mask_credentials(str(error)),
            )
        )
        return []
    return [part.partition("=")[2] if "=" in part else part for part in value.split(";")]


def detect_system_proxy() -> ProxyInfo | None:
    if sys.platform.startswith("win"):
        values = _windows_proxy_values()
    else:
        values = [os.getenv(name) or os.getenv(name.upper()) or "" for name in _PROXY_ENV_NAMES]
    for value in values:
        proxy = parse_proxy_url(value)
        if proxy is not None:
            return proxy
    return None
