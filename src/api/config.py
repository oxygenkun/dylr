from __future__ import annotations

import base64
import configparser
import functools
import hashlib
import hmac
import os
import re
import secrets
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable, cast

from src.config.booleans import parse_config_bool
from src.config.reader import TEXT_ENCODING
from src.network import targets
from src.support.utils import atomic_write_text

BUILTIN_QUALITIES: tuple[str, ...] = (
    "原画",
    "蓝光",
    "蓝光30M",
    "蓝光20M",
    "蓝光8M",
    "蓝光4M",
    "超清",
    "高清",
    "标清",
    "流畅",
)

QUALITY_KEYWORDS = BUILTIN_QUALITIES

QUALITY_OPTIONS_SECTION = "录制设置"
QUALITY_OPTIONS_KEY = "自定义画质选项(逗号分隔)"

CLEAN_URL_HOST_LIST = (
    "live.douyin.com",
    "live.bilibili.com",
    "www.huajiao.com",
    "www.zhihu.com",
    "www.huya.com",
    "chzzk.naver.com",
    "www.liveme.com",
    "www.haixiutv.com",
    "v.6.cn",
    "m.6.cn",
    "www.lehaitv.com",
)

DEFAULT_QUALITY = "原画"

WEB_DEFAULTS: dict[str, str | int | bool] = {
    "web_host": "127.0.0.1",
    "web_port": 8000,
    "web_auth_enable": False,
    "web_password": "",
    "web_token_expiry": 86400,
    "web_show_console": True,
    "web_trusted_proxy": "",
    "web_allowed_hosts": "",
}


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        host = url.split("/", 3)[2]
    except IndexError:
        return url
    if host in CLEAN_URL_HOST_LIST:
        url = url.split("?")[0]
    return url


def parse_url_config(file_path: str | Path) -> list[dict[str, str | bool]]:
    rooms: list[dict[str, str | bool]] = []
    path = Path(file_path)
    if not path.exists():
        return rooms

    with path.open("r", encoding=TEXT_ENCODING, errors="ignore") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n").rstrip("\r")
            stripped = line.strip()
            if not stripped:
                continue

            enabled = True
            content = stripped
            if content.startswith("#"):
                enabled = False
                content = content.lstrip("#").strip()

            parts = re.split(r"[,，]", content)

            quality = DEFAULT_QUALITY
            url = ""
            name = ""

            if len(parts) == 1:
                url = parts[0].strip()
            elif len(parts) == 2:
                if _is_url(parts[0]):
                    url = parts[0].strip()
                    name = parts[1].strip()
                else:
                    quality = _normalize_quality(parts[0].strip())
                    url = parts[1].strip()
            elif _is_url(parts[0]):
                url = parts[0].strip()
                name = "，".join(p.strip() for p in parts[1:])
            else:
                quality = _normalize_quality(parts[0].strip())
                url = parts[1].strip()
                name = "，".join(p.strip() for p in parts[2:])

            if name.startswith("主播:"):
                name = name[len("主播:") :].strip()
            elif name.startswith("主播："):
                name = name[len("主播：") :].strip()

            if not url:
                continue

            url = normalize_url(url)
            rooms.append(
                {
                    "url": url,
                    "quality": quality,
                    "name": name,
                    "enabled": enabled,
                    "raw_line": raw_line,
                }
            )
    return rooms


def _is_url(s: str) -> bool:
    return "://" in s or "." in s


def _normalize_quality(q: str) -> str:
    q = q.strip()
    return q if q in QUALITY_KEYWORDS else DEFAULT_QUALITY


def _reject_newline(kind: str, value: str) -> None:
    if value and ("\n" in value or "\r" in value):
        raise ValueError(f"{kind} 含换行符，禁止写入配置文件")


def _reject_list_separator(kind: str, value: str) -> None:
    if value and ("," in value or "，" in value):
        raise ValueError(f"{kind} 含逗号（半角或全角），逗号是配置行的字段分隔符，请改用其他写法")


def format_url_line(url: str, quality: str | None = None, name: str | None = None) -> str:
    validate_room_target(url, quality, name)
    _reject_newline("URL", url)
    _reject_newline("画质", quality or "")
    _reject_newline("主播名", name or "")
    _reject_list_separator("主播名", name or "")
    _reject_list_separator("URL", url)
    url = normalize_url(url)
    parts: list[str] = []
    q = (quality or "").strip()
    if q and q != DEFAULT_QUALITY:
        parts.append(q)
    parts.append(url)
    n = (name or "").strip()
    if n:
        parts.append(f"主播: {n}")
    return ",".join(parts)


def validate_config_target(section: str, key: str, value: str) -> None:
    _reject_newline(f"配置项 [{section}] {key} 的值", value)
    _validate_outbound_target_value(key, value)


_ALLOWED_ROOM_SCHEMES = frozenset(("http", "https"))


def _check_url_target(
    raw_url: str,
    *,
    kind: str,
    allowed_schemes: frozenset[str],
    default_scheme: str | None,
    allow_local_targets: bool,
    hint: str = "",
) -> None:
    from urllib.parse import urlparse

    u = raw_url.strip()
    if not u:
        return
    if "://" in u:
        probe = u
    elif default_scheme:
        probe = f"{default_scheme}://{u}"
    else:
        probe = u
    parsed = urlparse(probe)
    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise ValueError(f"{kind}协议不允许: {scheme or '(空)'}（仅支持 {'/'.join(sorted(allowed_schemes))}）")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"{kind}缺少主机名")
    reason = targets.host_rejection_reason(host, allow_local_targets=allow_local_targets)
    if reason is not None:
        raise ValueError(f"{kind}{reason}，禁止写入{hint}")


def _validate_room_url_target(raw_url: str) -> None:
    _check_url_target(
        raw_url,
        kind="直播间地址",
        allowed_schemes=_ALLOWED_ROOM_SCHEMES,
        default_scheme="https",  # 允许省略协议（normalize_url 会补 https），按 host 段校验
        allow_local_targets=False,
    )


def validate_room_target(url: str, quality: str | None = None, name: str | None = None) -> None:
    _reject_newline("URL", url)
    _reject_newline("画质", quality or "")
    _reject_newline("主播名", name or "")
    _reject_list_separator("主播名", name or "")
    _reject_list_separator("URL", url)
    _validate_room_url_target(url)
    q = (quality or "").strip()
    if q and q not in BUILTIN_QUALITIES:
        raise ValueError(f"未知画质档位: {q}")


_PUSH_URL_KEYS = frozenset({"钉钉推送接口链接", "微信推送接口链接", "bark推送接口链接", "ntfy推送地址"})
_PROXY_ADDR_KEYS = frozenset({"代理地址"})
_SMTP_HOST_KEYS = frozenset({"smtp邮件服务器"})
_PROXY_SCHEMES = frozenset(("http", "https", "socks4", "socks4a", "socks5", "socks5h"))

_INTERNAL_TARGET_HINT = (
    "；确需指向本机/内网自建服务（如与录制器同机部署的 ntfy），"
    "请设置环境变量 DOUYIN_WEB_ALLOW_INTERNAL_TARGET=1 后重启本进程"
)


def _allow_internal_outbound_target() -> bool:
    return os.environ.get("DOUYIN_WEB_ALLOW_INTERNAL_TARGET", "").strip().lower() in ("1", "true", "yes")


def _key_matches(key: str, table: frozenset[str]) -> bool:
    folded = key.strip().casefold()
    return any(folded == candidate.casefold() for candidate in table)


def _validate_outbound_target_value(key: str, value: str) -> None:
    stripped = value.strip()
    if not stripped:
        return
    kind = f"配置项「{key.strip()}」"
    if _key_matches(key, _PUSH_URL_KEYS):
        for segment in _split_multi_value(stripped):
            _check_url_target(
                segment,
                kind=kind,
                allowed_schemes=_ALLOWED_ROOM_SCHEMES,
                default_scheme=None,  # 推送地址缺协议时 urllib 本就报错，要求显式 http(s)://
                allow_local_targets=_allow_internal_outbound_target(),
                hint=_INTERNAL_TARGET_HINT,
            )
        return
    if _key_matches(key, _PROXY_ADDR_KEYS):
        _check_url_target(
            stripped,
            kind=kind,
            allowed_schemes=_PROXY_SCHEMES,
            default_scheme="http",  # 裸 ip:port 写法（normalize_proxy_url 同款归一）
            allow_local_targets=True,
        )
        return
    if _key_matches(key, _SMTP_HOST_KEYS):
        if "://" in stripped or any(ch.isspace() for ch in stripped):
            raise ValueError(f"{kind}应填主机名或 IP（可带 :端口），不得含协议前缀或空白")
        host = targets.normalize_host(stripped)
        reason = targets.host_rejection_reason(host, allow_local_targets=True) if host else "缺少主机名"
        if reason is not None:
            raise ValueError(f"{kind}{reason}，禁止写入")


def _split_multi_value(raw: str) -> list[str]:
    """拆分逗号分隔的多值配置（支持全角逗号），逐项 strip 并丢弃空项。"""
    return [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]


def normalize_quality_options(options: Iterable[str]) -> list[str]:
    """规范化画质选项：仅保留内置档位并去重（保持传入顺序），结果为空时回退内置全集。"""
    result: list[str] = []
    for item in options:
        name = (item or "").strip()
        if name in BUILTIN_QUALITIES and name not in result:
            result.append(name)
    return result or list(BUILTIN_QUALITIES)


def read_quality_options(config_file: str | Path) -> list[str]:
    """读取用户勾选的画质选项；键缺失/为空/全部非法时回退内置全集（首次使用行为不变）。"""
    parser = configparser.ConfigParser(interpolation=None)
    _ = parser.read(config_file, encoding=TEXT_ENCODING)
    raw = ""
    if parser.has_section(QUALITY_OPTIONS_SECTION):
        raw = parser.get(QUALITY_OPTIONS_SECTION, QUALITY_OPTIONS_KEY, fallback="")
    return normalize_quality_options(_split_multi_value(raw))


def write_quality_options(config_file: str | Path, options: Iterable[str]) -> list[str] | None:
    """写回画质选项（行级更新，保留注释与节顺序）；返回实际落盘的规范化列表，写失败返回 None。"""
    raw_list = list(options)
    for item in raw_list:
        _reject_newline("画质选项", str(item))
    normalized = normalize_quality_options(raw_list)
    value = ",".join(normalized)
    _reject_newline("画质选项", value)
    ok = update_or_append_config_line(config_file, QUALITY_OPTIONS_SECTION, QUALITY_OPTIONS_KEY, value)
    return normalized if ok else None


def _looks_like_url(segment: str) -> bool:
    """段是否像网址（仅需区分画质槽位与 URL 段，故只判 scheme，不用完整 URL 正则）。"""
    return "://" in segment


def _segment_matches_url(segment: str, url: str) -> bool:
    """段级 URL 匹配：配置行里的 URL 段可能缺 scheme 或带 query，统一规范化后比较，。"""
    seg = segment.strip()
    if not seg or seg.startswith(("主播:", "主播：")):
        return False
    return seg == url or normalize_url(seg) == url


def _rewrite_quality_field(raw_line: str, url: str, quality: str) -> str | None:
    """重写单行配置的画质段；行不匹配 URL 或已是目标画质时返回 None（无需变更）。"""
    stripped = raw_line.rstrip("\r\n")
    eol = raw_line[len(stripped) :]
    comment_prefix = ""
    body = stripped
    m = re.match(r"^(\s*#\s*)", body)
    if m:
        comment_prefix = m.group(1)
        body = body[m.end() :]
    if not body.strip():
        return None

    segments = [seg.strip() for seg in re.split(r"[,，]", body)]
    url_idx = -1
    for i, seg in enumerate(segments):
        if _segment_matches_url(seg, url):
            url_idx = i
            break
    if url_idx < 0:
        return None

    target_q = quality if quality and quality != DEFAULT_QUALITY else ""
    slot_idx = url_idx - 1 if url_idx > 0 and not _looks_like_url(segments[0]) else -1
    if (segments[slot_idx] if slot_idx >= 0 else "") == target_q:
        return None  # 已是目标画质，幂等跳过

    keep = [seg for i, seg in enumerate(segments) if i not in (url_idx, slot_idx) and seg]
    rebuilt_parts: list[str] = []
    if target_q:
        rebuilt_parts.append(target_q)
    rebuilt_parts.append(segments[url_idx])
    rebuilt_parts.extend(keep)
    return f"{comment_prefix}{','.join(rebuilt_parts)}{eol}"


def update_room_quality(url_config_file: str | Path, url: str, quality: str | None) -> bool:
    """将 URL_config.ini 中指定 URL 所在行的画质段更新为 quality；返回是否发生变更。"""
    if not url:
        return False
    target = normalize_url(url)
    new_quality = (quality or "").strip()
    _reject_newline("画质", new_quality)

    path = Path(url_config_file)
    if not path.exists():
        return False
    with _config_write_lock:
        with path.open("r", encoding=TEXT_ENCODING, newline="") as f:
            lines = f.readlines()

        changed = False
        out_lines: list[str] = []
        for raw_line in lines:
            rewritten = _rewrite_quality_field(raw_line, target, new_quality)
            if rewritten is not None:
                changed = True
                out_lines.append(rewritten)
            else:
                out_lines.append(raw_line)
        if not changed:
            return False

        joined = "".join(out_lines)
        return _atomic_write_text(path, joined)


def _atomic_write_text(path: Path, text: str) -> bool:
    """委托 src.support.utils.atomic_write_text（唯一实现）。"""
    return atomic_write_text(str(path), text, encoding=TEXT_ENCODING)


def read_web_config(config_file: str | Path) -> dict[str, str | int | bool]:
    parser = configparser.ConfigParser(interpolation=None)
    _ = parser.read(config_file, encoding=TEXT_ENCODING)
    result: dict[str, str | int | bool] = {}
    for key, default in WEB_DEFAULTS.items():
        if not parser.has_section("Web"):
            result[key] = default
            continue
        raw = parser.get("Web", key, fallback=str(default))
        if isinstance(default, bool):
            result[key] = parse_config_bool(raw, default)
        elif isinstance(default, int):
            try:
                result[key] = int(raw)
            except ValueError, TypeError:
                result[key] = default
        else:
            result[key] = raw
    return result


SENSITIVE_SECTIONS = {"Cookie", "账号密码", "Authorization"}
_SENSITIVE_SECTIONS_FOLDED = frozenset(name.casefold() for name in SENSITIVE_SECTIONS)
SENSITIVE_MASK = "***"

_WILDCARD_BIND_HOSTS = frozenset(("0.0.0.0", "::", "*", ""))


def is_loopback_bind_host(host: str) -> bool:
    name = targets.normalize_host(host)
    if not name:
        return False
    if name in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    addr = targets.parse_ip_literal(name)
    return bool(addr is not None and addr.is_loopback)


def get_allowed_hosts(cfg: Mapping[str, str | int | bool]) -> set[str]:
    allowed = {"localhost"}
    for raw in (str(cfg.get("web_host", "") or ""), str(cfg.get("web_allowed_hosts", "") or "")):
        for item in raw.replace("，", ",").split(","):
            name = targets.normalize_host(item)
            if name and name not in _WILDCARD_BIND_HOSTS:
                allowed.add(name)
    return allowed


def is_host_allowed(value: str, cfg: Mapping[str, str | int | bool]) -> bool:
    name = targets.normalize_host(value)
    if not name:
        return False
    if name in get_allowed_hosts(cfg):
        return True
    if targets.parse_ip_literal(name) is not None:
        return True
    if "." not in name:
        return True
    for allowed in get_allowed_hosts(cfg):
        if allowed.startswith("*.") and (name == allowed[2:] or name.endswith(allowed[1:])):
            return True
    return False


_ORIGIN_TEST_SENTINEL_HOSTS = frozenset(("testserver",))
_ORIGIN_SCHEME_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _as_port(value: object) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


def _origin_authority(origin: str) -> tuple[str, int, bool] | None:
    scheme, sep, rest = origin.strip().partition("://")
    if not sep:
        return None
    default_port = _ORIGIN_SCHEME_PORTS.get(scheme.lower())
    if default_port is None:
        return None
    authority, slash, tail = rest.partition("/")
    if slash or tail or not authority:
        return None
    if authority.startswith("["):
        end = authority.find("]")
        if end <= 0:
            return None
        host = authority[1:end].split("%", 1)[0]
        tail = authority[end + 1 :]
        if tail and not tail.startswith(":"):
            return None
        port_text = tail[1:] if tail else ""
    else:
        if authority.count(":") > 1:
            return None  # 裸 IPv6（未加方括号）不是浏览器会发出的 Origin 形态
        host, _, port_text = authority.partition(":")
    if not host or any(ch in authority for ch in "@?#\\"):
        return None
    host = host.strip().lower().rstrip(".")
    if not host:
        return None
    if not port_text:
        return host, default_port, False
    if not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return host, port, True


def get_origin_allowed_hosts(cfg: Mapping[str, str | int | bool]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for item in _split_multi_value(str(cfg.get("web_allowed_hosts", "") or "")):
        parsed = _origin_authority(item if "://" in item else f"http://{item}")
        if parsed is None:
            continue
        host, port, explicit = parsed
        result[host] = port if explicit else None
    return result


def is_origin_allowed(
    origin: str,
    cfg: Mapping[str, str | int | bool],
    *,
    bind_host: str = "",
    bind_port: int = 0,
) -> bool:
    parsed = _origin_authority(origin)
    if parsed is None:
        return False
    host, port, explicit_port = parsed
    if not explicit_port and host in _ORIGIN_TEST_SENTINEL_HOSTS:
        return True
    expected_port = bind_port or _as_port(cfg.get("web_port"))
    registered = get_origin_allowed_hosts(cfg)
    if host in registered:
        want = registered[host]
        if want is not None:
            return want == port
        return expected_port > 0 and port == expected_port
    if expected_port <= 0 or port != expected_port:
        return False
    if bind_host:
        bound = targets.normalize_host(bind_host)
        if bound and bound not in _WILDCARD_BIND_HOSTS and bound == host:
            return True
    return is_loopback_bind_host(host)


_SENSITIVE_KEY_PATTERN = re.compile(
    r"令牌|密码|授权码|token|secret|passwd|password|api[_-]?key|推送接口链接|推送地址|代理地址",
    re.IGNORECASE,
)

_SECRET_VALUE_QUERY_RE = re.compile(r"(access[_-]?token|token|key|secret|pwd|passwd|password|auth)=", re.IGNORECASE)
_URL_VALUE_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _looks_like_secret_value(value: str) -> bool:
    v = value.strip()
    if not _URL_VALUE_RE.match(v):
        return False
    parts = v.split("/", 3)
    if len(parts) > 2 and "@" in parts[2]:
        return True  # userinfo（http://user:pass@host）
    return bool(_SECRET_VALUE_QUERY_RE.search(v))


_KEY_NOT_SECRET_PATTERN = re.compile(r"expiry|timeout|有效期|过期", re.IGNORECASE)


def is_sensitive_key(key: str) -> bool:
    if _KEY_NOT_SECRET_PATTERN.search(key):
        return False
    return bool(_SENSITIVE_KEY_PATTERN.search(key))


def is_sensitive_item(section: str, key: str) -> bool:
    return section.casefold() in _SENSITIVE_SECTIONS_FOLDED or is_sensitive_key(key)


def read_config_safe(config_file: str | Path) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    _ = parser.read(config_file, encoding=TEXT_ENCODING)
    result: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        items: dict[str, str] = {}
        for key, value in parser.items(section):
            if value.strip() and (is_sensitive_item(section, key) or _looks_like_secret_value(value)):
                items[key] = SENSITIVE_MASK
            else:
                items[key] = value
        result[section] = items
    return result


@functools.lru_cache(maxsize=128)
def _key_line_pattern(key: str) -> re.Pattern[str]:
    """按 key 缓存编译后的行匹配模式：模式串依赖 key，无法整体预编译，但同 key 反复更新时应复用。"""
    return re.compile(r"^(\s*" + re.escape(key) + r"\s*[=:：]\s*)(.*)$", re.IGNORECASE)


_config_write_lock = threading.RLock()


def update_config_line(config_file: str | Path, section: str, key: str, value: str) -> bool:
    path = Path(config_file)
    if not path.exists():
        return False
    with _config_write_lock:
        with path.open("r", encoding=TEXT_ENCODING, newline="") as f:
            lines = f.readlines()
        cur_section: str | None = None
        in_target = False
        replaced = False
        key_pattern = _key_line_pattern(key)
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                cur_section = stripped[1:-1].strip()
                in_target = cur_section == section
                new_lines.append(line)
                continue
            if in_target and not replaced:
                m = key_pattern.match(line.rstrip("\n").rstrip("\r"))
                if m:
                    prefix = m.group(1)  # "key = " 部分
                    old_tail = m.group(2)  # 原值（可能含行内注释）
                    inline_comment = ""
                    _stripped_tail = old_tail.lstrip()
                    if _stripped_tail[:1] in ('"', "'"):
                        _quote = _stripped_tail[0]
                        _q_start = old_tail.find(_quote) + 1
                        _q_end = old_tail.find(_quote, _q_start)
                        if _q_end > 0:
                            inline_comment = old_tail[_q_end + 1 :]
                    eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                    new_lines.append(f"{prefix}{value}{inline_comment}{eol}")
                    replaced = True
                    continue
            new_lines.append(line)
        if not replaced:
            return False
        return _atomic_write_text(path, "".join(new_lines))
    return True


def append_config_line(config_file: str | Path, section: str, key: str, value: str) -> bool:
    path = Path(config_file)
    if not path.exists():
        return False
    with _config_write_lock:
        with path.open("r", encoding=TEXT_ENCODING, newline="") as f:
            lines = f.readlines()
        insert_at: int | None = None  # 目标节内的插入点（下一节头之前）；None＝文件尾
        in_section = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not (stripped.startswith("[") and stripped.endswith("]")):
                continue
            name = stripped[1:-1].strip()
            if in_section:
                insert_at = idx  # 走到下一节头部即目标节结束
                break
            if name == section:
                in_section = True
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        entry = f"{key} = {value}\n"
        if in_section:
            lines.insert(insert_at if insert_at is not None else len(lines), entry)
        else:
            lines.append(f"[{section}]\n{entry}")
        _atomic_write_text(path, "".join(lines))
    return True


def update_or_append_config_line(config_file: str | Path, section: str, key: str, value: str) -> bool:
    """行级「替换优先、缺键补建」的组合写入（2026-09-12 审查 H-6）：两步全程持同一把。"""
    with _config_write_lock:
        if update_config_line(config_file, section, key, value):
            return True
        return append_config_line(config_file, section, key, value)


_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 200_000
_PBKDF2_MAX_ITERATIONS = 1_000_000


def clamp_pbkdf2_iterations(raw: object) -> int:
    try:
        value = int(cast("str", raw))
    except TypeError, ValueError:
        return _PBKDF2_ITERATIONS
    return max(_PBKDF2_ITERATIONS, min(value, _PBKDF2_MAX_ITERATIONS))


def hash_web_password(plaintext: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def is_hashed_web_password(value: str) -> bool:
    return bool(value) and value.startswith(f"{_PBKDF2_ALGO}$")


def verify_web_password(plaintext: str, stored: str) -> bool:
    if not stored:
        return False
    if is_hashed_web_password(stored):
        try:
            _, iters_s, salt_b64, hash_b64 = stored.split("$")
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
            dk = hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"), salt, clamp_pbkdf2_iterations(iters_s))
        except ValueError:
            return False
        return hmac.compare_digest(dk, expected)
    return hmac.compare_digest(plaintext.encode("utf-8"), stored.encode("utf-8"))
