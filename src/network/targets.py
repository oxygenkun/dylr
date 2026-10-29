"""出站目标的地址判定；配置验证与请求校验共用，DNS 结果不缓存。"""

import ipaddress
import socket
from typing import cast
from urllib.parse import urlparse

from src.network.urls import is_http_url
from src.support import i18n
from src.support.utils import mask_credentials

_EXTRA_BLOCKED_V4_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT/共享地址空间（阿里云元数据 100.100.100.200 落于其中）
    ipaddress.ip_network("0.0.0.0/8"),  # 「本机」语义，含 0.x 缩写形态
    ipaddress.ip_network("198.18.0.0/15"),  # 基准测试段
    ipaddress.ip_network("192.0.0.0/24"),  # IETF 协议分配段
)

_V6_TRANSITION_NETWORKS = (
    ipaddress.ip_network("2001::/32"),  # Teredo
    ipaddress.ip_network("2002::/16"),  # 6to4
)

_CLOUD_METADATA_IPS = frozenset(
    (
        "169.254.169.254",  # AWS / OpenStack / GCP 通用
        "169.254.170.2",  # AWS ECS 容器凭据
        "100.100.100.200",  # 阿里云（落在 100.64.0.0/10 内，但 3.14 起该段不算私网）
        "169.254.0.4",  # 腾讯云（元数据 IP 形态）
    )
)

_INTERNAL_NAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",  # RFC 6762 私有解析 + AWS 的 *.ec2.internal，覆盖 metadata.google.internal
    ".home.arpa",  # RFC 8375 家庭网络域
    ".localdomain",
    ".invalid",
    ".test",
    ".onion",
    ".i2p",
)
_INTERNAL_NAME_EXACT = frozenset(
    (
        "localhost",
        "metadata",
        "metadata.tencentyun.com",  # 腾讯云元数据的域名形态（公网侧解析亦指向链路本地）
        "ip6-localhost",
        "ip6-loopback",
    )
)

_LOCAL_NAME_EXACT = frozenset(("localhost", "ip6-localhost", "ip6-loopback"))
_LOCAL_NAME_SUFFIX = ".localhost"


def normalize_host(value: str) -> str:
    h = value.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        if end > 0:
            return h[1:end]
    if h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    return h.strip().rstrip(".")


def _parse_aton_part(part: str) -> int | None:
    if not part:
        return None
    low = part.lower()
    if low.startswith("0x"):
        digits, base, allow = part[2:], 16, "0123456789abcdef"
    elif part[0] == "0" and len(part) > 1:
        digits, base, allow = part[1:], 8, "01234567"
    else:
        digits, base, allow = part, 10, "0123456789"
    if not digits or any(ch not in allow for ch in digits.lower()):
        return None
    try:
        return int(digits, base)
    except ValueError:
        return None


def _ipv4_from_aton(host: str) -> ipaddress.IPv4Address | None:
    parts = host.split(".")
    n = len(parts)
    if not 1 <= n <= 4:
        return None
    last_index = n - 1
    total = 0
    for i, part in enumerate(parts):
        value = _parse_aton_part(part)
        if value is None:
            return None
        if i < last_index:
            if value > 255:
                return None
            total |= value << (8 * (3 - i))
        elif value >= 1 << (8 * (5 - n)):
            return None
        else:
            total |= value
    try:
        return ipaddress.IPv4Address(total)
    except ValueError:
        return None


def parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    name = normalize_host(host)
    if not name:
        return None
    name = name.split("%", 1)[0]
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return _ipv4_from_aton(name)


def _in_always_blocked_v4_network(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return isinstance(addr, ipaddress.IPv4Address) and any(addr in net for net in _EXTRA_BLOCKED_V4_NETWORKS)


def ip_rejection_reason(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_local_targets: bool = False
) -> str | None:
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return ip_rejection_reason(mapped, allow_local_targets=allow_local_targets)
    text = str(addr)
    if text in _CLOUD_METADATA_IPS:
        return f"云厂商元数据端点: {text}"
    if addr.is_loopback:
        if allow_local_targets:
            return None
        return f"回环地址: {text}"
    if addr.is_unspecified:
        return f"未指定地址(0.0.0.0/::): {text}"
    if addr.is_link_local:
        return f"链路本地地址(含 169.254.0.0/16): {text}"
    if addr.version == 6 and any(addr in net for net in _V6_TRANSITION_NETWORKS):
        return None
    if addr.is_private:
        if allow_local_targets and not _in_always_blocked_v4_network(addr):
            return None
        return f"私网地址: {text}"
    if addr.is_multicast:
        return f"组播地址: {text}"
    if addr.is_reserved:
        return f"保留地址: {text}"
    if isinstance(addr, ipaddress.IPv4Address) and any(addr in net for net in _EXTRA_BLOCKED_V4_NETWORKS):
        return f"共享/保留网段(CGNAT 等): {text}"
    return None


def resolve_host_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return [normalize_host(cast(str, info[4][0])) for info in infos]


def offline_host_rejection_reason(host: str) -> str | None:
    literal = parse_ip_literal(host)
    if literal is not None:
        return ip_rejection_reason(literal)
    name = normalize_host(host)
    if not name:
        return "缺少主机名"
    if name in _INTERNAL_NAME_EXACT or any(name.endswith(suffix) for suffix in _INTERNAL_NAME_SUFFIXES):
        return f"本机/内部用途域名: {name}"
    return None


def host_rejection_reason(host: str, *, allow_local_targets: bool = False) -> str | None:
    literal = parse_ip_literal(host)
    if literal is not None:
        return ip_rejection_reason(literal, allow_local_targets=allow_local_targets)
    name = normalize_host(host)
    if not name:
        return "直播间地址缺少主机名"
    if allow_local_targets and (name in _LOCAL_NAME_EXACT or name.endswith(_LOCAL_NAME_SUFFIX)):
        return None
    if name in _INTERNAL_NAME_EXACT or any(name.endswith(suffix) for suffix in _INTERNAL_NAME_SUFFIXES):
        return f"本机/内部用途域名: {name}"
    try:
        addresses = resolve_host_ips(name)
    except OSError:
        return f"主机名无法解析: {name}"
    if not addresses:
        return f"主机名无解析结果: {name}"
    for ip_text in addresses:
        parsed = parse_ip_literal(ip_text)
        if parsed is None:
            continue
        reason = ip_rejection_reason(parsed, allow_local_targets=allow_local_targets)
        if reason is not None:
            return f"主机名 {name} 解析到{reason}"
    return None


class UnsafeTarget(ValueError):
    """目标地址违反出站规则。"""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(i18n.tr("出站目标被拒绝: {url} - {reason}", url=mask_credentials(url), reason=reason))


def stream_target_reason(url: str, proxy_addr: str | None = None) -> str | None:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "流地址格式无效"
    if not host:
        return "流地址缺少主机名"
    if proxy_addr and parse_ip_literal(host) is None:
        return offline_host_rejection_reason(host)
    return host_rejection_reason(host)


def validate_http_target(url: str, proxy_addr: str | None = None) -> None:
    reason = stream_target_reason(url, proxy_addr) if is_http_url(url) else "非 HTTP/HTTPS 地址"
    if reason is not None:
        raise UnsafeTarget(url, reason)
