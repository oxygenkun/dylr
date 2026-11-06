import random
import re
import threading
import time
from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

from src.network import policy, targets
from src.network import urls as network_urls
from src.recording import state
from src.support import i18n, utils

_URL_PATTERN = re.compile(r"(https?://)?(www\.)?[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+(:\d+)?(/.*)?")


def contains_url(string: str) -> bool:
    """用于区分配置行里的画质字段与网址。"""
    return _URL_PATTERN.search(string) is not None


_MAX_NAME_CHARS = 60

_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)]
)


def clean_name(input_text: str) -> str:
    cleaned_name = re.sub(state.rstr, "_", input_text.strip()).strip("_")
    cleaned_name = re.sub(r"[\x00-\x1f\x7f]", "_", cleaned_name).strip("_")
    cleaned_name = cleaned_name.replace("（", "(").replace("）", ")")
    if state.clean_emoji:
        cleaned_name = utils.remove_emojis(cleaned_name, "_").strip("_")
    cleaned_name = cleaned_name.replace("&", "_")
    if cleaned_name and cleaned_name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        cleaned_name = cleaned_name + "_"
    if len(cleaned_name) > _MAX_NAME_CHARS:
        cleaned_name = cleaned_name[:_MAX_NAME_CHARS].strip("_")
    return cleaned_name or "空白昵称"


def get_quality_code(qn: str) -> str:
    """蓝光细粒度档位（蓝光4M/8M/20M/30M）：虎牙/斗鱼按各自专属档位表处理（见 src/streams/stream.py），。"""
    quality_zh_to_en = {
        "原画": "OD",
        "蓝光": "BD",
        "蓝光30M": "BD30",
        "蓝光20M": "BD20",
        "蓝光8M": "BD8",
        "蓝光4M": "BD4",
        "超清": "UHD",
        "高清": "HD",
        "标清": "SD",
        "流畅": "LD",
    }
    return quality_zh_to_en.get(qn, "OD")


DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 ("
    "KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"
)

_RECORD_HEADER_RULES: dict[str, str] = {
    "PandaTV": "origin:https://www.pandalive.co.kr",
    "WinkTV": "origin:https://www.winktv.co.kr",
    "PopkonTV": "origin:https://www.popkontv.com",
    "TTingLive(原Flextv)": "origin:https://www.flextv.co.kr",
    "千度热播": "referer:https://qiandurebo.com",
    "17Live": "referer:https://17.live/en/live/6302408",
    "浪Live": "referer:https://www.lang.live",
    "shopee": "origin",  # 由 live_url 域名解析
    "blued": "referer:https://app.blued.cn",
    "B站直播": "referer:https://live.bilibili.com/",
}


def get_record_headers(platform: str, live_url: str, cookies: str | None = None) -> dict[str, str] | None:
    """校验探针与 ffmpeg 录制共用请求头，避免探针通过而录制被拒绝。"""
    header_dict: dict[str, str] = {}
    rule = _RECORD_HEADER_RULES.get(platform)
    if rule == "origin" and live_url:
        live_domain = "/".join(live_url.split("/")[0:3])
        header_dict["origin"] = live_domain
    elif rule:
        key, value = rule.split(":", 1)
        header_dict[key] = value
    if cookies:
        header_dict["cookie"] = cookies
    return header_dict or None


_DESKTOP_UA_PLATFORMS = ("虎牙直播", "B站直播")


def get_record_user_agent(platform: str) -> str | None:
    if platform in _DESKTOP_UA_PLATFORMS:
        return DESKTOP_UA
    return None


_GET_RECHECK_INTERVAL = 0.8
_GET_RECHECK_JITTER = 0.7

_PROBE_MIN_HOST_INTERVAL = 0.35
_PROBE_THROTTLE_JITTER = 0.4
_probe_last_seen: dict[str, float] = {}
_probe_throttle_lock = threading.Lock()


def _recheck_delay() -> float:
    return _GET_RECHECK_INTERVAL + random.uniform(0, _GET_RECHECK_JITTER)


def _throttle_probe(url: str) -> None:
    """同 host 探针节流：距上次同 host 探针不足最小间隔时补足等待（锁内计算、锁外睡眠，。"""
    host = urlsplit(url).netloc
    if not host:
        return
    wait = 0.0
    with _probe_throttle_lock:
        now = time.time()
        min_gap = _PROBE_MIN_HOST_INTERVAL + random.uniform(0, _PROBE_THROTTLE_JITTER)
        retention = _PROBE_MIN_HOST_INTERVAL * 10
        for stale_host in [h for h, ts in _probe_last_seen.items() if now - ts > retention]:
            _probe_last_seen.pop(stale_host, None)
        last = _probe_last_seen.get(host, 0.0)
        if now - last < min_gap:
            wait = min_gap - (now - last)
        _probe_last_seen[host] = now + wait  # 登记预计发出时刻，排队中的后续探针据此排队
    if wait > 0:
        time.sleep(wait)


_PROBE_BACKOFF_PLATFORMS = ("虎牙直播",)
_FLV_FIRST_PLATFORMS = ("虎牙直播",)
_PROBE_BACKOFF_SECONDS = 60.0
_PROBE_BACKOFF_INTERVAL_MARGIN = 70.0
_probe_backoff: dict[str, float] = {}
_probe_backoff_lock = threading.Lock()


def _probe_backoff_window() -> float:
    """退避窗口必须至少跨过一个主循环周期，否则闭环恒不成立：固定 60s 而 delay_default 默认。"""
    interval = float(state.delay_default or 0)
    return max(_PROBE_BACKOFF_SECONDS, interval + _PROBE_BACKOFF_INTERVAL_MARGIN)


def _probe_backoff_key(url: str) -> str:
    """提取 url 的退避键（scheme://host/路径，去掉 query）：虎牙每轮解析返回新 token（query 变化）。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _mark_probe_reject(url: str, platform: str | None) -> None:
    """记录一次探针拒绝（401/403）：platform 在退避名单内才记录；顺带清理过期键防字典无界增长。"""
    if platform not in _PROBE_BACKOFF_PLATFORMS:
        return
    key = _probe_backoff_key(url)
    now = time.time()
    window = _probe_backoff_window()
    with _probe_backoff_lock:
        for stale in [k for k, ts in _probe_backoff.items() if now - ts > window]:
            _probe_backoff.pop(stale, None)
        _probe_backoff[key] = now


def _clear_probe_reject(url: str, platform: str | None) -> None:
    """撤销一次退避记录：该地址已被实际拉流验证可用（录制成功），先前的拒绝（探针假绿或。"""
    if platform not in _PROBE_BACKOFF_PLATFORMS:
        return
    key = _probe_backoff_key(url)
    with _probe_backoff_lock:
        _probe_backoff.pop(key, None)


def _probe_in_backoff(url: str, platform: str | None) -> bool:
    """查询 url 是否处于探针退避窗口内（仅对退避名单内平台生效）。"""
    if platform not in _PROBE_BACKOFF_PLATFORMS:
        return False
    with _probe_backoff_lock:
        ts = _probe_backoff.get(_probe_backoff_key(url))
        return ts is not None and time.time() - ts <= _probe_backoff_window()


mark_ffmpeg_reject = _mark_probe_reject
clear_ffmpeg_reject = _clear_probe_reject


def _is_h265(url: str) -> bool:
    """判定流地址是否为 h265 编码：候选过滤、record_url 兜底与 stream.py 的 h264 备选收集。"""
    codec = network_urls.query_values(url, "codec")
    return bool(codec) and codec[0] == "h265"


def _h265_copy_format_supported() -> bool:
    """判定当前「视频保存格式」的容器能否直拷（-c copy）HEVC 流：TS/MKV/MP4 三个 muxer 均。"""
    raw = getattr(state, "video_save_type", "")
    fmt = raw.strip().upper() if isinstance(raw, str) else ""
    return fmt in ("TS", "MKV", "MP4")


_ALLOWED_STREAM_SCHEMES = frozenset({"http", "https", "rtmp", "rtmps"})


def _is_recordable_url(url: str) -> bool:
    """判定 url 是否具备「可以进 ffmpeg -i」的最小形态：协议在白名单内 + 主机名非空 + 不以 '-' 开头。"""
    if not url or url.startswith("-"):
        return False
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    return parts.scheme.lower() in _ALLOWED_STREAM_SCHEMES and bool(parts.netloc)


_PROBE_TIMEOUT_SECONDS = 5


_PROBE_BODY_LIMIT = 65_536


def _read_redirect_body(response: httpx.Response) -> None:
    if not response.has_redirect_location:
        return
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(_PROBE_BODY_LIMIT + 1):
        total += len(chunk)
        if total > _PROBE_BODY_LIMIT:
            raise targets.UnsafeTarget(
                str(response.url),
                i18n.tr("重定向响应体超过探针上限 {limit} 字节", limit=_PROBE_BODY_LIMIT),
            )
        chunks.append(chunk)
    response._content = b"".join(chunks)


def _probe_client(timeout: int, proxy_addr: str | None, verify: bool) -> httpx.Client:
    """为一次选源构造客户端，校验目标并限制中间跳响应体。"""
    return httpx.Client(
        timeout=timeout,
        proxy=proxy_addr,
        verify=verify,
        trust_env=False,
        event_hooks={
            "request": [lambda request: targets.validate_http_target(str(request.url), proxy_addr)],
            "response": [_read_redirect_body],
        },
    )


class _ProbeResponse:
    def __init__(self, status_code: int, headers: Mapping[str, str], text: str) -> None:
        """探针读取结果：只暴露判定与日志要用的三个字段。刻意不直接返回 httpx.Response——。"""
        self.status_code = status_code
        self.headers = headers
        self.text = text


def _bounded_probe_get(
    client: httpx.Client,
    url: str,
    headers: Mapping[str, str],
    *,
    read_body: bool = True,
    body_limit: int = _PROBE_BODY_LIMIT,
) -> _ProbeResponse:
    with client.stream("GET", url, headers=dict(headers), follow_redirects=True) as resp:
        if not read_body:
            return _ProbeResponse(resp.status_code, resp.headers, "")
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= body_limit:
                break
        raw = b"".join(chunks)[:body_limit]
        if not raw:
            return _ProbeResponse(resp.status_code, resp.headers, "")
        encoding = resp.encoding or "utf-8"
        return _ProbeResponse(resp.status_code, resp.headers, raw.decode(encoding, errors="replace"))


_PRIVATE_HOST_PATTERN = re.compile(
    r"^(?:localhost|127\.|0\.|10\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|::1|fe80|f[cd][0-9a-f]{2}:)",
    re.IGNORECASE,
)
_IPV4_HOST_PATTERN = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def _is_derived_hop_allowed(url: str) -> bool:
    """派生地址是否允许发出：形态合规（_is_recordable_url）且不是内网/本机目标。"""
    if not _is_recordable_url(url):
        return False
    host = urlsplit(url).hostname or ""
    return not _PRIVATE_HOST_PATTERN.match(host)


class _DerivedHopRejected(targets.UnsafeTarget):
    """播放列表正文派生出不安全目标。"""


def _registrable_domain(host: str) -> str:
    """取主机名的近似注册域（末两段：a.b.example.com → example.com）。"""
    if _IPV4_HOST_PATTERN.fullmatch(host):
        return ""
    labels = host.split(".")
    if len(labels) < 3:
        return ""
    return ".".join(labels[-2:])


def _is_same_probe_scope(base_url: str, derived_url: str) -> bool:
    """派生地址与播放列表是否同 host 或同注册域。"""
    base_host = (urlsplit(base_url).hostname or "").lower()
    derived_host = (urlsplit(derived_url).hostname or "").lower()
    if not base_host or not derived_host:
        return False
    if base_host == derived_host:
        return True
    base_domain = _registrable_domain(base_host)
    return bool(base_domain) and base_domain == _registrable_domain(derived_host)


def _headers_without_cookie(headers: Mapping[str, str]) -> dict[str, str]:
    """剥掉 cookie 头：是否携带的唯一判据是「与播放列表是否同域」，不由跳转形态决定。"""
    return {name: value for name, value in headers.items() if name.lower() != "cookie"}


def _headers_for_derived_hop(base_url: str, derived_url: str, headers: Mapping[str, str]) -> dict[str, str]:
    """派生地址这一跳实际使用的请求头：同域沿用原头，跨域只剥 Cookie、继续探测（凭据外泄面。"""
    if _is_same_probe_scope(base_url, derived_url):
        return dict(headers)
    logger.debug(
        i18n.tr(
            "HLS 播放列表派生的地址与列表不同域，已剥离 Cookie 后继续探测: {url}",
            url=utils.mask_credentials(derived_url),
        )
    )
    return _headers_without_cookie(headers)


_BANDWIDTH_PATTERN = re.compile(r"BANDWIDTH=(\d+)")


def _pick_master_variant(text: str, base_url: str) -> str | None:
    """从 master playlist 正文里挑出「应当探测」的变体 URL：按 BANDWIDTH 取最高码率档。"""
    variants: list[tuple[int, str]] = []
    pending_bw: int | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("#EXT-X-STREAM-INF"):
                bw_match = _BANDWIDTH_PATTERN.search(line)
                pending_bw = int(bw_match.group(1)) if bw_match else None
            continue
        variants.append((pending_bw if pending_bw is not None else -1, urljoin(base_url, line)))
        pending_bw = None
    if not variants:
        return None
    best_bw = max(bw for bw, _ in variants)
    return next(u for bw, u in variants if bw == best_bw)


def _probe_hls_segment(
    client: httpx.Client,
    playlist_url: str,
    headers: Mapping[str, str],
    platform: str | None = None,
) -> bool:
    """HLS 播放列表可达 ≠ 可录制：媒体分片必须单独探测（斗鱼 hw CDN 实测事故）。"""
    media_url = playlist_url
    media_lines: list[str] = []

    try:
        resp = _bounded_probe_get(client, playlist_url, headers)
        if resp.status_code != 200:
            return True  # 列表 GET 失败（HEAD/Range-GET 已通过）——不据此推翻，交由 ffmpeg
        text = resp.text or ""
        for _ in range(2):
            if "#EXT-X-STREAM-INF" not in text:
                break
            variant_url = _pick_master_variant(text, media_url)
            if not variant_url:
                return True  # 只有标签行、取不到变体 URL——解析不出分片，保守维持列表可达
            if not _is_derived_hop_allowed(variant_url):
                logger.warning(
                    i18n.tr(
                        "流地址形态不合规，已丢弃（不交给 ffmpeg -i）: {url}",
                        url=utils.mask_credentials(variant_url),
                    )
                )
                raise _DerivedHopRejected(variant_url, "变体列表地址形态不合规/指向内网")
            sub = _bounded_probe_get(
                client,
                variant_url,
                _headers_for_derived_hop(playlist_url, variant_url, headers),
            )
            if sub.status_code != 200:
                return True  # 子列表取不到——保守，不下分片结论
            media_url = variant_url
            text = sub.text or ""
        media_lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not media_lines:
            return True  # 空列表（直播刚结束/未推流）——无分片可探，维持列表可达结论
        seg_line = media_lines[-1]
        is_media = seg_line.endswith((".ts", ".mp4", ".m4s")) or any(
            ext + "?" in seg_line for ext in (".ts", ".mp4", ".m4s")
        )
        if not is_media:
            return True  # 非标准分片行（加密/自定义格式）——解析不出，保守放行
        seg_url = urljoin(media_url, seg_line)
        if not _is_derived_hop_allowed(seg_url):
            logger.warning(
                i18n.tr(
                    "流地址形态不合规，已丢弃（不交给 ffmpeg -i）: {url}",
                    url=utils.mask_credentials(seg_url),
                )
            )
            raise _DerivedHopRejected(seg_url, "媒体分片地址形态不合规/指向内网")
        seg_headers = _headers_for_derived_hop(playlist_url, seg_url, headers)
    except _DerivedHopRejected:
        raise
    except targets.UnsafeTarget:
        raise
    except Exception as e:
        logger.debug(
            i18n.tr(
                "HLS 分片探测解析异常(按列表可达处理): {url} - {type_name}: {e}",
                url=utils.mask_credentials(playlist_url),
                type_name=type(e).__name__,
                e=e,
            )
        )
        return True
    probe_status: int | None = None
    for attempt in range(2):
        _throttle_probe(seg_url)
        try:
            seg_resp = _bounded_probe_get(client, seg_url, {**seg_headers, "Range": "bytes=0-0"}, read_body=False)
        except targets.UnsafeTarget:
            raise
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "HLS 分片探测异常(按列表可达处理): {url} - {type_name}: {e}",
                    url=utils.mask_credentials(seg_url),
                    type_name=type(e).__name__,
                    e=e,
                )
            )
            return True
        if seg_resp.status_code in (200, 206):
            if attempt:
                logger.debug(
                    i18n.tr(
                        "HLS 分片探测重试通过({status_code})，先前拒绝为偶发: {url}",
                        url=utils.mask_credentials(seg_url),
                        status_code=seg_resp.status_code,
                    )
                )
            return True
        probe_status = seg_resp.status_code
        if seg_resp.status_code in (401, 403):
            _mark_probe_reject(playlist_url, platform)
            if attempt == 0:
                time.sleep(_recheck_delay())
                continue
        break  # 404 等确定性拒绝不重试
    logger.warning(
        i18n.tr(
            "HLS 播放列表可达但媒体分片不可达({status_code})，列表层为假绿: {url}",
            url=utils.mask_credentials(seg_url),
            status_code=probe_status,
        )
    )
    return False


def _confirm_get_ok(
    client: httpx.Client,
    url: str,
    head_status: int,
    headers: Mapping[str, str],
    last_resort: bool = False,
    platform: str | None = None,
) -> bool:
    """FLV/record_url 源 HEAD 通过后的 GET 复核（流式请求、不读 body）：虎牙等 CDN 出现过。"""
    reject_status: int | None = None

    for attempt in range(2):
        try:
            with client.stream("GET", url, headers=headers, follow_redirects=True) as probe:
                if probe.status_code not in (401, 403):
                    if attempt:
                        logger.debug(
                            i18n.tr(
                                "流地址校验: {url} - GET 复核重试通过({status_code})，先前拒绝为偶发",
                                url=utils.mask_credentials(url),
                                status_code=probe.status_code,
                            )
                        )
                    return True
                reject_status = probe.status_code
                _mark_probe_reject(url, platform)
        except targets.UnsafeTarget:
            raise
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "流地址校验: {url} - GET 复核异常: {type_name}: {e}（attempt {attempt}）",
                    url=utils.mask_credentials(url),
                    type_name=type(e).__name__,
                    e=e,
                    attempt=attempt,
                )
            )
            if attempt == 0:
                time.sleep(_recheck_delay())
                continue
            return True
        if attempt == 0:
            time.sleep(_recheck_delay())
    if last_resort:
        logger.warning(
            i18n.tr(
                "流地址校验: {url} - HEAD={head_status} 通过但 GET 复核两次 {reject_status}；已无备选源，仍交由 ffmpeg 尝试（探针与 ffmpeg 客户端指纹不同，探针拒绝不代表 ffmpeg 不可拉流）",
                url=utils.mask_credentials(url),
                head_status=head_status,
                reject_status=reject_status,
            )
        )
        return True
    logger.warning(
        i18n.tr(
            "流地址校验失败: {url} - HEAD={head_status} 通过但 GET 复核两次 {reject_status}（CDN 稳定拒绝 GET），判定不可达",
            url=utils.mask_credentials(url),
            head_status=head_status,
            reject_status=reject_status,
        )
    )
    return False


def _validate_stream_url(
    url: str,
    proxy_addr: str | None = None,
    timeout: int = _PROBE_TIMEOUT_SECONDS,
    verify: bool | None = None,
    platform: str | None = None,
    cookies: str | None = None,
    last_resort: bool = False,
    client: httpx.Client | None = None,
    security_rejected: set[str] | None = None,
) -> bool:
    """探测流地址是否可用于录制：proxy_addr 为可选代理、timeout 为单次探针超时、verify 为 SSL。"""

    internal_reason = targets.stream_target_reason(url, proxy_addr)
    if internal_reason is not None:
        logger.warning(
            i18n.tr(
                "探针拒绝内网/保留目标（疑似被篡改的流地址）: {masked_url} - {reason}",
                masked_url=utils.mask_credentials(url),
                reason=internal_reason,
            )
        )
        if security_rejected is not None:
            security_rejected.add(url)
        return False
    if urlsplit(url).scheme.lower() in ("rtmp", "rtmps"):
        return _is_recordable_url(url)
    if verify is None:
        verify = policy.should_verify_stream_tls(platform)

    headers: dict[str, str] = {}
    if platform:
        base = get_record_headers(platform, url, cookies=cookies)
        if base:
            headers.update(base)
        headers["User-Agent"] = get_record_user_agent(platform) or MOBILE_UA
    if _probe_in_backoff(url, platform):
        if last_resort:
            logger.warning(
                i18n.tr(
                    "流地址校验: {url} - CDN 探针退避中，跳过探针直接交由 ffmpeg 拉流", url=utils.mask_credentials(url)
                )
            )
            return True
        logger.warning(
            i18n.tr("流地址校验: {url} - CDN 探针退避中，跳过本轮探针、回退下一候选", url=utils.mask_credentials(url))
        )
        return False
    _throttle_probe(url)
    owns_client = client is None
    probe_client: httpx.Client | None = client
    try:
        if probe_client is None:
            proxy_addr = network_urls.normalize_proxy_url(proxy_addr)
            probe_client = _probe_client(timeout, proxy_addr, verify)
        response = probe_client.head(url, headers=headers, follow_redirects=True)
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code in (401, 403):
            _mark_probe_reject(url, platform)
        if ".m3u8" in url.lower():
            if response.status_code == 200 or any(
                k in content_type for k in ("video", "octet-stream", "flash", "mpegurl")
            ):
                if _probe_hls_segment(probe_client, url, headers, platform=platform):
                    return True
                if last_resort:
                    logger.warning(
                        i18n.tr(
                            "流地址校验: {url} - HLS 媒体分片不可达；已无备选源，仍交由 ffmpeg 尝试",
                            url=utils.mask_credentials(url),
                        )
                    )
                    return True
                return False
            probe: _ProbeResponse | None = None  # 失败路径（last_resort/告警）需 Range-GET 结果；先置 None 避免未绑定
            for attempt in range(2):
                probe = _bounded_probe_get(probe_client, url, {**headers, "Range": "bytes=0-0"}, read_body=False)
                if probe.status_code in (200, 206):
                    if attempt:
                        logger.debug(
                            i18n.tr(
                                "流地址校验: {url} - Range-GET 重试通过({status_code})，先前拒绝为偶发",
                                url=utils.mask_credentials(url),
                                status_code=probe.status_code,
                            )
                        )
                    if _probe_hls_segment(probe_client, url, headers, platform=platform):
                        return True
                    if last_resort:
                        logger.warning(
                            i18n.tr(
                                "流地址校验: {url} - HLS 媒体分片不可达；已无备选源，仍交由 ffmpeg 尝试",
                                url=utils.mask_credentials(url),
                            )
                        )
                        return True
                    return False
                if probe.status_code not in (401, 403):
                    break  # 非探针误杀类拒绝（如 404），不重试
                _mark_probe_reject(url, platform)
                if attempt == 0:
                    time.sleep(_recheck_delay())
            if probe is None:
                logger.warning(
                    i18n.tr("流地址校验: {url} - Range-GET 未取得响应，按校验失败处理", url=utils.mask_credentials(url))
                )
                return False
            if last_resort:
                logger.warning(
                    i18n.tr(
                        "流地址校验: {url} - HEAD={status_code}, Range-GET={status_code_2}；已无备选源，仍交由 ffmpeg 尝试（探针与 ffmpeg 客户端指纹不同）",
                        url=utils.mask_credentials(url),
                        status_code=response.status_code,
                        status_code_2=probe.status_code,
                    )
                )
                return True
            logger.warning(
                i18n.tr(
                    "流地址校验失败: {url} - HEAD={status_code}, Range-GET={status_code_2}, content-type={content_type}",
                    url=utils.mask_credentials(url),
                    status_code=response.status_code,
                    status_code_2=probe.status_code,
                    content_type=probe.headers.get("content-type", ""),
                )
            )
            return False
        if any(k in content_type for k in ("video", "octet-stream", "flash", "mpegurl")):
            return _confirm_get_ok(
                probe_client, url, response.status_code, headers, last_resort=last_resort, platform=platform
            )
        if "text/html" in content_type or "application/json" in content_type:
            if last_resort:
                logger.warning(
                    i18n.tr(
                        "流地址校验: {url} - status_code={status_code}, content-type={content_type}；已无备选源，仍交由 ffmpeg 尝试（探针与 ffmpeg 客户端指纹不同）",
                        url=utils.mask_credentials(url),
                        status_code=response.status_code,
                        content_type=content_type,
                    )
                )
                return True
            logger.warning(
                i18n.tr(
                    "流地址校验失败（返回非流媒体内容）: {url} - status_code={status_code}, content-type={content_type}",
                    url=utils.mask_credentials(url),
                    status_code=response.status_code,
                    content_type=content_type,
                )
            )
            return False
        if response.status_code == 200:
            return _confirm_get_ok(
                probe_client, url, response.status_code, headers, last_resort=last_resort, platform=platform
            )
        if last_resort:
            logger.warning(
                i18n.tr(
                    "流地址校验: {url} - status_code={status_code}, content-type={content_type}；已无备选源，仍交由 ffmpeg 尝试（探针与 ffmpeg 客户端指纹不同）",
                    url=utils.mask_credentials(url),
                    status_code=response.status_code,
                    content_type=content_type,
                )
            )
            return True
        logger.warning(
            i18n.tr(
                "流地址校验失败: {url} - status_code={status_code}, content-type={content_type}",
                url=utils.mask_credentials(url),
                status_code=response.status_code,
                content_type=content_type,
            )
        )
        return False
    except _DerivedHopRejected as e:
        logger.warning(
            i18n.tr(
                "探针拒绝内网/保留目标（疑似被篡改的流地址）: {masked_url} - {reason}",
                masked_url=utils.mask_credentials(e.url),
                reason=f"播放列表正文派生跳命中: {e.reason}（初始地址 {utils.mask_credentials(url)}）",
            )
        )
        if security_rejected is not None:
            security_rejected.add(url)
        return False
    except targets.UnsafeTarget as e:
        logger.warning(
            i18n.tr(
                "探针拒绝内网/保留目标（疑似被篡改的流地址）: {masked_url} - {reason}",
                masked_url=utils.mask_credentials(e.url),
                reason=f"重定向逐跳复检命中: {e.reason}（初始地址 {utils.mask_credentials(url)}）",
            )
        )
        if security_rejected is not None:
            security_rejected.add(url)
        return False
    except Exception as e:
        logger.warning(
            i18n.tr(
                "流地址校验异常: {url} - {type_name}: {e}",
                url=utils.mask_credentials(url),
                type_name=type(e).__name__,
                e=e,
            )
        )
        if last_resort:
            logger.warning(
                i18n.tr(
                    "已无备选源，仍交由 ffmpeg 尝试: {url}",
                    url=utils.mask_credentials(url),
                )
            )
            return True
        logger.warning(
            i18n.tr(
                "流地址校验异常（判定为不可达）: {url}",
                url=utils.mask_credentials(url),
            )
        )
        return False
    finally:
        if owns_client and probe_client is not None:
            try:
                probe_client.close()
            except Exception as e:
                logger.debug(i18n.tr("关闭探针客户端失败: {type_name}: {e}", type_name=type(e).__name__, e=e))


def _hls_selection_config() -> tuple[bool, tuple[str, ...]]:
    """读取 main 的 HLS 采集配置（是否启用 + 排除平台名）：两个全局在启动早期或测试替身下。"""
    enabled = bool(getattr(state, "hls_collection_enabled", True))
    raw: object = getattr(state, "hls_collection_exclude_platforms", ())
    if isinstance(raw, str):
        names = raw.replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        names = [str(p) for p in raw]
    else:
        names = []
    return enabled, tuple(name.strip() for name in names if name.strip())


def _same_origin_flv(hls_url: str, flv_candidates: list[str]) -> str | None:
    """从 FLV 候选里找出与给定 HLS 地址「同源」的那条：部分平台（如斗鱼）把 FLV 直链按扩展名。"""
    stem = hls_url.lower().split("?", 1)[0]
    if not stem.endswith(".m3u8"):
        return None
    target = stem[: -len(".m3u8")] + ".flv"
    for candidate in flv_candidates:
        if candidate.lower().split("?", 1)[0] == target:
            return candidate
    return None


def _log_source_choice(platform: str | None, kind: str, url: str) -> None:
    """选源结论单行日志：记下「最终采用哪条源、属于哪一类候选」，便于在「只出弹幕 SRT、。"""
    logger.debug(
        i18n.tr(
            "选源结论: platform={platform} 采用 {kind} 源: {url}",
            platform=platform or "",
            kind=kind,
            url=utils.mask_credentials(url),
        )
    )


def select_source_url(
    stream_info: Mapping[str, object],
    proxy_addr: str | None = None,
    platform: str | None = None,
    cookies: str | None = None,
) -> str | None:
    """从 stream_info 挑选本轮实际录制地址：默认 HLS → FLV → record_url；FLV-first 平台。"""

    def _as_str_list(value: object) -> list[str]:
        """proxy_addr 必须透传给校验器：境外平台若不走与解析阶段相同的代理路径，直连校验会。"""
        if isinstance(value, str) and value:
            return [value]
        if isinstance(value, list):
            return [u for u in value if isinstance(u, str) and u]
        return []

    _hls_enabled, _hls_exclude_platforms = _hls_selection_config()
    hls_excluded = platform is not None and platform in _hls_exclude_platforms
    hls_effective_enabled = _hls_enabled and not hls_excluded

    hls_candidates = _as_str_list(stream_info.get("m3u8_url"))
    for u in _as_str_list(stream_info.get("m3u8_url_list")):
        if u not in hls_candidates:
            hls_candidates.append(u)
    flv_candidates = _as_str_list(stream_info.get("flv_url"))
    for u in _as_str_list(stream_info.get("flv_url_list")):
        if u not in flv_candidates:
            flv_candidates.append(u)
    record_url_raw = stream_info.get("record_url")
    record_url_str = record_url_raw if isinstance(record_url_raw, str) else ""
    record_url_is_hls = ".m3u8" in record_url_str.lower()
    record_url_enabled = bool(record_url_str) and (hls_effective_enabled or not record_url_is_hls)
    hls_available = bool(hls_candidates)
    flv_available = bool(flv_candidates)
    has_fallback = flv_available or record_url_enabled

    if not (hls_available or has_fallback):
        logger.warning(
            i18n.tr(
                "解析结果无任何流地址（m3u8/flv/record_url 均为空），本轮放弃: {anchor_name}",
                anchor_name=stream_info.get("anchor_name") or "",
            )
        )
        return None
    if hls_available and not hls_effective_enabled and not has_fallback:
        if hls_excluded:
            logger.warning(
                i18n.tr(
                    "平台 {platform} 在 HLS 采集排除列表中，且无 FLV/record_url 可回退，本轮放弃（可将该平台移出排除列表恢复 HLS 采集）: {anchor_name}",
                    platform=platform,
                    anchor_name=stream_info.get("anchor_name") or "",
                )
            )
        else:
            logger.warning(
                i18n.tr(
                    "存在 HLS 源但 HLS 采集未启用，且无 FLV/record_url 可回退，本轮放弃（可开启 HLS 采集恢复录制）: {anchor_name}",
                    anchor_name=stream_info.get("anchor_name") or "",
                )
            )
        return None

    proxy_addr = network_urls.normalize_proxy_url(proxy_addr)
    probe_client: httpx.Client | None
    try:
        probe_client = _probe_client(_PROBE_TIMEOUT_SECONDS, proxy_addr, policy.should_verify_stream_tls(platform))
    except ValueError, TypeError, httpx.InvalidURL:
        probe_client = None
    try:
        if record_url_enabled and not _is_recordable_url(record_url_str):
            logger.warning(
                i18n.tr(
                    "流地址形态不合规，已丢弃（不交给 ffmpeg -i）: {url}",
                    url=utils.mask_credentials(record_url_str),
                )
            )
            record_url_enabled = False

        def _accept_source(url: str, kind: str) -> str | None:
            if not _is_recordable_url(url):
                logger.warning(
                    i18n.tr(
                        "流地址形态不合规，已丢弃（不交给 ffmpeg -i）: {url}",
                        url=utils.mask_credentials(url),
                    )
                )
                return None
            landing_reason = targets.stream_target_reason(url, proxy_addr)
            if landing_reason is not None:
                logger.warning(
                    i18n.tr(
                        "探针拒绝内网/保留目标（疑似被篡改的流地址）: {masked_url} - {reason}",
                        masked_url=utils.mask_credentials(url),
                        reason=f"交给 ffmpeg -i 前的落地复核命中（{kind} 通道）: {landing_reason}",
                    )
                )
                return None
            _log_source_choice(platform, kind, url)
            return url

        flv_first = platform in _FLV_FIRST_PLATFORMS
        hls_seq: list[tuple[str, bool]] = (
            [(u, True) for u in hls_candidates] if (hls_available and hls_effective_enabled) else []
        )
        flv_seq: list[tuple[str, bool]] = [(u, False) for u in flv_candidates]
        seq = flv_seq + hls_seq if flv_first else hls_seq + flv_seq

        h265_copy_ok = _h265_copy_format_supported()
        usable: list[tuple[str, bool]] = []
        for url, is_hls in seq:
            if _is_h265(url) and not h265_copy_ok:
                logger.warning(i18n.tr("h265 编码候选无法 copy 录制，跳过: {url}", url=utils.mask_credentials(url)))
                continue
            usable.append((url, is_hls))

        probed: dict[str, bool] = {}
        security_rejected: set[str] = set()
        prev_kind: bool | None = None
        for idx, (cand, is_hls) in enumerate(usable):
            if prev_kind is not None and is_hls != prev_kind:
                if prev_kind:
                    logger.warning(i18n.tr("HLS 候选校验失败，回退 FLV"))
                else:
                    logger.warning(i18n.tr("FLV 候选校验失败，回退 HLS"))
            prev_kind = is_hls
            is_last = idx == len(usable) - 1
            reachable = _validate_stream_url(
                cand,
                proxy_addr=proxy_addr,
                platform=platform,
                cookies=cookies,
                last_resort=is_last and not record_url_enabled,
                client=probe_client,
                security_rejected=security_rejected,
            )
            probed[cand] = reachable
            if reachable:
                accepted = _accept_source(cand, "HLS" if is_hls else "FLV")
                if accepted:
                    return accepted
                probed[cand] = False
            if is_hls and not probed[cand]:
                same_origin = _same_origin_flv(cand, flv_candidates)
                if same_origin:
                    logger.warning(
                        i18n.tr(
                            "HLS 源校验失败，将回退同 token 的 FLV 源: {url}",
                            url=utils.mask_credentials(same_origin),
                        )
                    )
        if usable and record_url_enabled:
            logger.warning(i18n.tr("HLS/FLV 全部候选校验失败，尝试 record_url 兜底"))

        if record_url_enabled:
            if _is_h265(record_url_str):
                logger.warning(i18n.tr("record_url 含 h265 编码，且无 HLS/FLV 兜底可用"))
            if record_url_str in probed and record_url_str not in security_rejected:
                if not probed[record_url_str]:
                    logger.warning(
                        i18n.tr(
                            "流地址校验: {url} - 与本轮已探候选同址，复用结论并交由 ffmpeg 拉流",
                            url=utils.mask_credentials(record_url_str),
                        )
                    )
                accepted = _accept_source(record_url_str, "record_url")
                if accepted:
                    return accepted
            elif record_url_str not in security_rejected and _validate_stream_url(
                record_url_str,
                proxy_addr=proxy_addr,
                platform=platform,
                cookies=cookies,
                last_resort=True,
                client=probe_client,
                security_rejected=security_rejected,
            ):
                accepted = _accept_source(record_url_str, "record_url")
                if accepted:
                    return accepted
        if hls_available and not hls_effective_enabled:
            logger.warning(
                i18n.tr(
                    "存在 {count} 条 m3u8 候选但 HLS 采集对本平台关闭（全局关闭或命中排除列表），"
                    "已整组静默丢弃（可开启「是否启用HLS采集(是/否)」或将平台移出「HLS采集排除平台(逗号分隔)」恢复）: {anchor_name}",
                    count=len(hls_candidates),
                    anchor_name=stream_info.get("anchor_name") or "",
                )
            )
        logger.warning(i18n.tr("选源结论: platform={platform} 本轮无可用源", platform=platform or ""))
        return None
    finally:
        if probe_client is not None:
            try:
                probe_client.close()
            except Exception as e:
                logger.debug(i18n.tr("关闭探针客户端失败: {type_name}: {e}", type_name=type(e).__name__, e=e))


def _douyin_rate_limit() -> None:
    """抖音接口调用限流：必要时 sleep，保证两次抖音请求间隔不小于 douyin_min_interval 秒。"""
    with state.douyin_rate_lock:
        now = time.time()
        elapsed = now - state.douyin_last_request_time
        if elapsed < state.douyin_min_interval:
            time.sleep(state.douyin_min_interval - elapsed)
        state.douyin_last_request_time = time.time()
