import asyncio
import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.parse
import uuid
from operator import itemgetter
from typing import Optional, cast

import httpx

from src import JS_SCRIPT_PATH
from src.config import files as config_files
from src.config.booleans import parse_config_bool
from src.network import http, policy, targets
from src.network import urls as network_urls
from src.network.credentials import DEFAULT_TTL as _CREDENTIAL_TTL
from src.network.credentials import credential_cache, fetch_cookies
from src.runtime import javascript
from src.runtime.javascript import ProgramError
from src.streams.errors import trace_error_decorator, trace_error_decorator_or_none
from src.streams.room import UnsupportedUrlError, get_sec_user_id, get_unique_id, is_user_homepage_url
from src.streams.ttwid import get_ttwid
from src.support import i18n, utils
from src.support.logger import logger, script_path
from src.support.utils import generate_random_string

OptionalStr = str | None
OptionalDict = dict[str, str] | None
OptionalStreamDict = dict[str, str | bool] | None

_BANDWIDTH_PATTERN = re.compile(r"BANDWIDTH=(\d+)")
_DOUYIN_HEVC_FLV_PATTERN = re.compile(r'(https?://[^\s"\']*stream-\d{10,}(?!_[a-z0-9]+)\.flv(?:[^"\']|\\u0026)+)')

_POPKONTV_APP_CREDENTIAL = "FpAhe6mh8Qtz116OENBmRddbYVirNKasktdXQiuHfm88zRaFydTsFy63tzkdZY0u"


def _popkontv_credential() -> str:
    return os.environ.get("POPKONTV_APP_CREDENTIAL", "").strip() or _POPKONTV_APP_CREDENTIAL


def _shopee_host_suffix(url: str) -> str:
    """Shopee 站点后缀解析的唯一实现：取「去掉 live. 首段后的完整 TLD」。"""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return "com"
    if not host:
        return "com"
    host = re.sub(r"^live\.", "", host)
    parts = host.split(".", maxsplit=1)
    return parts[-1] if len(parts) > 1 and parts[-1] else "com"


_SHOPEE_ALLOWED_HOST_SUFFIXES = (
    "shopee.sg",
    "shopee.com",
    "shopee.co.id",
    "shopee.com.my",
    "shopee.com.ph",
    "shopee.co.th",
    "shopee.vn",
    "shopee.com.br",
    "shopee.mx",
    "shopee.cl",
    "shopee.tw",
    "shopee.fr",
    "shopee.es",
    "shopee.pl",
    "shp.ee",
)


def _shopee_is_allowed_host(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if "@" in parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _SHOPEE_ALLOWED_HOST_SUFFIXES)


def _safe_extract_id(url: str, default: str = "") -> str:
    path = url.split("?")[0].rstrip("/")
    parts = path.rsplit("/", maxsplit=1)
    return parts[1] if len(parts) > 1 else default


_cached_kuaishou_did: str = ""
_cached_kuaishou_did_ts: float = 0.0
_cached_kuaishou_did_proxy: str = ""
_cached_twitch_client_id: str = ""
_cached_twitch_client_id_ts: float = 0.0
_cached_twitch_client_id_proxy: str = ""
_kuaishou_did_lock = threading.Lock()
_twitch_client_id_lock = threading.Lock()

_TWITCH_WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0"
_TWITCH_BROWSER_VERSION = _TWITCH_WEB_UA.split("rv:", 1)[-1].split(")", 1)[0]  # -> "148.0"


def invalidate_kuaishou_did_cache(proxy_addr: OptionalStr = None) -> None:
    global _cached_kuaishou_did, _cached_kuaishou_did_ts, _cached_kuaishou_did_proxy
    with _kuaishou_did_lock:
        _cached_kuaishou_did = ""
        _cached_kuaishou_did_ts = 0.0
        recorded = _cached_kuaishou_did_proxy
        _cached_kuaishou_did_proxy = ""
    keys = {recorded}
    if proxy_addr is not None:
        keys.add(proxy_addr or "")
    for k in keys:
        credential_cache.invalidate(f"kuaishou_did|{k}")


def invalidate_twitch_client_id_cache(proxy_addr: OptionalStr = None) -> None:
    global _cached_twitch_client_id, _cached_twitch_client_id_ts, _cached_twitch_client_id_proxy
    with _twitch_client_id_lock:
        _cached_twitch_client_id = ""
        _cached_twitch_client_id_ts = 0.0
        recorded = _cached_twitch_client_id_proxy
        _cached_twitch_client_id_proxy = ""
    keys = {recorded}
    if proxy_addr is not None:
        keys.add(proxy_addr or "")
    for k in keys:
        credential_cache.invalidate(f"twitch_client_id|{k}")


def _take_cached_kuaishou_did(proxy_addr: OptionalStr) -> str:
    if not _cached_kuaishou_did:
        return ""
    if _cached_kuaishou_did_proxy != (proxy_addr or ""):
        return ""
    if _cached_kuaishou_did_ts and (time.monotonic() - _cached_kuaishou_did_ts) >= _CREDENTIAL_TTL:
        return ""
    return _cached_kuaishou_did


async def _ensure_kuaishou_did(proxy_addr: OptionalStr = None) -> str:
    global _cached_kuaishou_did, _cached_kuaishou_did_ts, _cached_kuaishou_did_proxy
    cached_did = _take_cached_kuaishou_did(proxy_addr)
    if cached_did:
        return cached_did

    async def _fetch() -> str:
        cookies_dict = await fetch_cookies(
            url="https://live.kuaishou.com/",
            proxy_addr=proxy_addr,
            headers={
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            },
            timeout=10,
        )
        if not isinstance(cookies_dict, dict):
            return ""
        did = cookies_dict.get("did", "")
        didv = cookies_dict.get("didv", "")
        if not did:
            return ""
        logger.debug("自动获取快手 did 成功")
        return f"did={did}; didv={didv}" if didv else f"did={did}"

    got = await credential_cache.get_or_fetch(key=f"kuaishou_did|{proxy_addr or ''}", factory=_fetch, wait_timeout=15)
    if isinstance(got, str) and got:
        with _kuaishou_did_lock:
            _cached_kuaishou_did = got
            _cached_kuaishou_did_ts = time.monotonic()
            _cached_kuaishou_did_proxy = proxy_addr or ""
        return got
    with _kuaishou_did_lock:
        if _cached_kuaishou_did and _cached_kuaishou_did_proxy != (proxy_addr or ""):
            return ""
        if _cached_kuaishou_did and (time.monotonic() - _cached_kuaishou_did_ts) >= _CREDENTIAL_TTL:
            _cached_kuaishou_did = ""
            _cached_kuaishou_did_ts = 0.0
            _cached_kuaishou_did_proxy = ""
        return _cached_kuaishou_did


async def _ensure_twitch_client_id(proxy_addr: OptionalStr = None) -> str:
    global _cached_twitch_client_id, _cached_twitch_client_id_ts, _cached_twitch_client_id_proxy
    if (
        _cached_twitch_client_id
        and _cached_twitch_client_id_proxy == (proxy_addr or "")
        and (not _cached_twitch_client_id_ts or (time.monotonic() - _cached_twitch_client_id_ts) < _CREDENTIAL_TTL)
    ):
        return _cached_twitch_client_id

    async def _fetch() -> str:
        html = await http.request(
            url="https://www.twitch.tv/",
            proxy_addr=proxy_addr,
            headers={"User-Agent": _TWITCH_WEB_UA, "Accept-Language": "en-US"},
            timeout=10,
        )
        html = _get_str_response(html)
        match = re.search(r'"Client-ID"\s*[:=]\s*"([a-z0-9]{20,})"', html)
        if not match:
            return ""
        logger.debug("自动获取 Twitch Client-Id 成功")
        return match.group(1)

    got = await credential_cache.get_or_fetch(
        key=f"twitch_client_id|{proxy_addr or ''}", factory=_fetch, wait_timeout=15
    )
    if isinstance(got, str) and got:
        with _twitch_client_id_lock:
            _cached_twitch_client_id = got
            _cached_twitch_client_id_ts = time.monotonic()
            _cached_twitch_client_id_proxy = proxy_addr or ""
    elif _cached_twitch_client_id and (time.monotonic() - _cached_twitch_client_id_ts) >= _CREDENTIAL_TTL:
        with _twitch_client_id_lock:
            _cached_twitch_client_id = ""
            _cached_twitch_client_id_ts = 0.0
    return _cached_twitch_client_id


def _generate_twitch_play_session_id() -> str:
    return generate_random_string(32).lower()


def _get_str_response(resp: object) -> str:
    if isinstance(resp, str):
        return resp
    if isinstance(resp, tuple) and len(resp) > 0 and isinstance(resp[0], str):
        return resp[0]
    return ""


def _loads_dict(text: object) -> dict[str, object]:
    s = _get_str_response(text)
    if not s:
        return {}
    return _safe_loads(s) or {}


def _safe_loads(text: str) -> Optional[dict[str, object]]:
    try:
        parsed = cast(object, json.loads(text))
    except json.JSONDecodeError as e:
        logger.warning(i18n.tr("JSON 解析失败(已忽略): {type_name}: {e}", type_name=type(e).__name__, e=e))
        return None
    return parsed if isinstance(parsed, dict) else None


def _loads_dict_prefix(text: str) -> dict[str, object]:
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}


def _dig(data: object, *keys: str | int) -> object:
    current: object = data
    for key in keys:
        if isinstance(key, int):
            if not isinstance(current, list) or not -len(current) <= key < len(current):
                return None
            current = current[key]
        elif isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _dig_str(data: object, *keys: str | int) -> str:
    value = _dig(data, *keys)
    return value if isinstance(value, str) else ""


def _dig_list(data: object, *keys: str | int) -> list[object]:
    value = _dig(data, *keys)
    return cast(list[object], value) if isinstance(value, list) else []


def _qn_sort_key(item: object) -> int:
    """B站清晰度档位的排序键：current_qn 实际响应里是 int，但风控页/降级响应可能给字符串。"""
    value = _dig(item, "current_qn")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _warn_api_abnormal(json_str: str, api_name: str, field: str, data: dict[str, object]) -> None:
    if not json_str:
        logger.warning(i18n.tr("{api} 返回空响应（风控或网络失败）", api=api_name))
        return
    code: object = None
    for key in ("code", "status", "errorCode", "resultCode"):
        code = _dig(data, key)
        if code is not None:
            break
    msg: object = None
    for key in ("message", "msg", "errorMsg"):
        msg = _dig(data, key)
        if msg:
            break
    logger.warning(
        i18n.tr(
            "{api} 响应缺少 {field} 字段（房间不存在/未开通 或 接口改版/风控页）: code={code} msg={msg}",
            api=api_name,
            field=field,
            code=code if code is not None else "",
            msg=msg if msg is not None else "",
        )
    )


def get_params(url: str, params: str) -> OptionalStr:
    parsed_url = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed_url.query)

    if params in query_params:
        return query_params[params][0]
    return None


def extract_douyin_hevc_flv_url(html: str) -> OptionalStr:
    for match in _DOUYIN_HEVC_FLV_PATTERN.findall(html):
        clean_url = match.replace("\\u0026", "&").rstrip("\\").strip()
        parsed = urllib.parse.urlparse(clean_url)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("only_audio", ["0"])[0] == "1":
            continue
        if query.get("codec"):
            return cast(str, clean_url)
        separator = "&" if parsed.query else "?"
        return cast(str, f"{clean_url}{separator}codec=h265")
    return None


async def get_play_url_list(m3u8: str, proxy: OptionalStr = None, header: OptionalDict = None) -> list[str]:
    resp = await http.request(url=m3u8, proxy_addr=proxy, headers=header)
    if not isinstance(resp, str):
        return []
    play_url_list: list[str] = []
    _m3u8_scheme = urllib.parse.urlparse(m3u8).scheme or "https"
    for i in resp.split("\n"):
        line = i.strip()
        if line.startswith(("https://", "http://")):
            play_url_list.append(line)
        elif line.startswith("//"):
            play_url_list.append(f"{_m3u8_scheme}:{line}")
    if not play_url_list:
        for i in resp.split("\n"):
            if i.strip().endswith("m3u8"):
                play_url_list.append(i.strip())
    bandwidth_pattern = _BANDWIDTH_PATTERN
    bandwidth_list = cast(list[str], bandwidth_pattern.findall(resp))
    if bandwidth_list and len(bandwidth_list) == len(play_url_list):
        url_to_bandwidth = {url: int(bandwidth) for bandwidth, url in zip(bandwidth_list, play_url_list)}
        play_url_list = sorted(play_url_list, key=lambda url: url_to_bandwidth[url], reverse=True)
    return play_url_list


def _inject_origin_urls(stream_section: dict[str, object], origin_url_list: dict[str, object]) -> None:
    """把抖音 ORIGIN（原画）线路注入 stream 段的候选表。"""
    sdk_raw = origin_url_list.get("sdk_params", "")
    if isinstance(sdk_raw, dict):
        sdk_params = cast(dict[str, object], sdk_raw)
    else:
        sdk_params = _loads_dict(sdk_raw if isinstance(sdk_raw, str) else "")
    vcodec = sdk_params.get("VCodec")
    origin_hls_codec = vcodec if isinstance(vcodec, str) else ""

    hls_v = origin_url_list.get("hls", "")
    flv_v = origin_url_list.get("flv", "")
    hls_s = hls_v if isinstance(hls_v, str) else ""
    flv_s = flv_v if isinstance(flv_v, str) else ""
    if hls_s:
        hls_pull_url_map = cast(dict[str, object], stream_section.get("hls_pull_url_map") or {})
        stream_section["hls_pull_url_map"] = {"ORIGIN": hls_s + "&codec=" + origin_hls_codec, **hls_pull_url_map}
    if flv_s:
        flv_pull_url = cast(dict[str, object], stream_section.get("flv_pull_url") or {})
        stream_section["flv_pull_url"] = {"ORIGIN": flv_s + "&codec=" + origin_hls_codec, **flv_pull_url}


def _extract_room_data_from_html(html_str: str) -> dict[str, object]:
    if not html_str:
        return {}
    try:
        match_json_str = re.search(r'(\{\\"state\\":.*?)]\\n"]\)', html_str)
        if not match_json_str:
            match_json_str = re.search(r'(\{\\"common\\":.*?)]\\n"]\)</script><div hidden', html_str)
        if not match_json_str:
            return {}
        json_str = match_json_str.group(1)
        cleaned_string = json_str.replace("\\", "").replace(r"u0026", r"&")
        room_store_match = re.search('"roomStore":(.*?),"linkmicStore"', cleaned_string, re.DOTALL)
        if not room_store_match:
            return {}
        room_store = room_store_match.group(1)
        anchor_name_match = re.search('"nickname":"(.*?)","avatar_thumb', room_store, re.DOTALL)
        anchor_name = anchor_name_match.group(1) if anchor_name_match else ""
        room_store = room_store.split(',"has_commerce_goods"')[0]
        room_store_dict = _loads_dict_prefix(room_store) or _loads_dict(room_store + "}}}")
        room_info = cast(dict[str, object], room_store_dict.get("roomInfo") or {})
        json_data = cast(dict[str, object], room_info.get("room") or {})
        json_data["anchor_name"] = anchor_name
        if json_data.get("status") == 4:
            return json_data
        stream_url_field = cast(dict[str, object], json_data.get("stream_url") or {})
        stream_orientation = stream_url_field.get("stream_orientation")
        origin_url_list: dict[str, object] | None = None
        match_json_str2 = cast(list[str], re.findall(r'"(\{\\"common\\":.*?)"]\)</script><script nonce=', html_str))
        if match_json_str2:
            if stream_orientation == 1:
                json_str2 = match_json_str2[0]
            else:
                json_str2 = match_json_str2[1] if len(match_json_str2) > 1 else match_json_str2[0]
            json_data2 = _loads_dict(
                json_str2.replace("\\", "").replace('"{', "{").replace('}"', "}").replace("u0026", "&")
            )
            data2 = cast(dict[str, object], json_data2.get("data") or {})
            if "origin" in data2:
                origin_url_list = cast(dict[str, object], cast(dict[str, object], data2["origin"]).get("main") or {})
        else:
            html_str_clean = html_str.replace("\\", "").replace("u0026", "&")
            match_json_str3 = re.search('"origin":\\{"main":(.*?),"dash"', html_str_clean, re.DOTALL)
            if match_json_str3:
                origin_url_list = _loads_dict(match_json_str3.group(1) + "}")
        if origin_url_list:
            _inject_origin_urls(stream_url_field, origin_url_list)
            hevc_flv_url = extract_douyin_hevc_flv_url(html_str)
            if hevc_flv_url:
                stream_url_field["hevc_flv_url"] = hevc_flv_url
        return json_data
    except Exception as e:
        logger.debug(
            i18n.tr(
                "抖音房间页 __INITIAL_STATE__ 解析异常（按未开播处理）: {type_name}: {e}",
                type_name=type(e).__name__,
                e=e,
            )
        )
        return {}


async def get_douyin_web_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    chrome_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )
    headers = {
        "cookie": "",
        "referer": url,
        "user-agent": chrome_ua,
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "sec-ch-ua": '"Google Chrome";v="141", "Chromium";v="141", "Not_A Brand";v="8"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest",
    }
    if cookies:
        headers["cookie"] = cookies
    else:
        headers["cookie"] = await get_ttwid(proxy_addr)

    try:
        web_rid = url.split("?")[0].rstrip("/").split("live.douyin.com/")[-1]
        params = {
            "aid": "6383",
            "app_name": "douyin_web",
            "live_id": "1",
            "device_platform": "web",
            "language": "zh-CN",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "141.0.0.0",
            "web_rid": web_rid,
            "msToken": "",
        }

        api = f"https://live.douyin.com/webcast/room/web/enter/?{urllib.parse.urlencode(params)}"

        async def _try_web_api() -> dict[str, object]:
            json_str = _get_str_response(await http.request(url=api, proxy_addr=proxy_addr, headers=headers))
            if not json_str:
                raise Exception("empty response from API (possible risk control)")
            parsed = _loads_dict(json_str)
            status_code = parsed.get("status_code")
            if status_code is not None and int(cast(str, status_code)) != 0:
                status_msg = parsed.get("status_msg", "unknown error")
                raise Exception(f"API returned status_code={status_code}, msg={status_msg}")
            json_data = cast(dict[str, object], parsed.get("data") or {})
            inner_list = json_data.get("data")
            if not inner_list:
                room_status = json_data.get("room_status")
                if isinstance(room_status, int) and not isinstance(room_status, bool) and room_status != 0:
                    logger.debug(
                        "Douyin room is not live (web API returned empty room list, room_status={room_status})",
                        room_status=room_status,
                    )
                    return {"anchor_name": _dig_str(json_data, "user", "nickname")}
                raise Exception(f"{utils.mask_credentials(url)} VR live is not supported or room not found")
            room_data = cast(dict[str, object], cast(list[object], inner_list)[0])
            user_info = cast(dict[str, object], json_data.get("user") or {})
            room_data["anchor_name"] = _dig_str(user_info, "nickname")
            return room_data

        room_data: dict[str, object] | None = None
        api_error: Exception | None = None
        html_str = ""
        for attempt in range(2):
            try:
                room_data = await _try_web_api()
                break
            except Exception as e:
                api_error = e
                if attempt == 0:
                    await asyncio.sleep(0.5)  # 给瞬时风控一个缓冲窗口
                    continue
                logger.warning(
                    i18n.tr("Douyin web API failed: {api_error}, falling back to HTML scraping", api_error=api_error)
                )
                try:
                    html_str = _get_str_response(await http.request(url=url, proxy_addr=proxy_addr, headers=headers))
                    room_data = _extract_room_data_from_html(html_str)
                    if not room_data:
                        raise Exception(f"HTML scraping also failed after API error: {api_error}")
                    logger.debug("HTML scraping fallback succeeded")
                except Exception as e2:
                    raise Exception(f"Douyin web data fetch error (API: {api_error}) (HTML fallback: {e2}).")
        if room_data is None:
            raise Exception(f"Douyin web data fetch error: {api_error}")

        if room_data.get("status") == 2:
            if "stream_url" not in room_data:
                raise RuntimeError(
                    "The live streaming type or gameplay is not supported on the computer side yet, please use the "
                    "app to share the link for recording."
                )
            stream_url = cast(dict[str, object], room_data["stream_url"])
            if not html_str:
                html_str = _get_str_response(await http.request(url=url, proxy_addr=proxy_addr, headers=headers))
            hevc_flv_url = extract_douyin_hevc_flv_url(html_str)
            live_core_sdk_data = cast(dict[str, object], stream_url.get("live_core_sdk_data") or {})
            pull_datas = cast(dict[str, object], stream_url.get("pull_datas") or {})
            if live_core_sdk_data:
                json_str = ""
                if pull_datas:
                    hevc_candidate = ""
                    first_candidate = ""
                    for value in pull_datas.values():
                        value_dict = cast(dict[str, object], value) if isinstance(value, dict) else {}
                        candidate_raw = value_dict.get("stream_data") or ""
                        candidate = candidate_raw if isinstance(candidate_raw, str) else ""
                        if not candidate:
                            continue
                        try:
                            cand_data = cast(dict[str, object], _loads_dict(candidate).get("data") or {})
                        except json.JSONDecodeError, TypeError:
                            continue
                        if "origin" not in cand_data:
                            continue
                        if not first_candidate:
                            first_candidate = candidate
                        try:
                            cand_main = cast(dict[str, object], cast(dict[str, object], cand_data["origin"])["main"])
                            cand_sdk_raw = cand_main.get("sdk_params", "{}")
                            codec_val = _loads_dict(cand_sdk_raw if isinstance(cand_sdk_raw, str) else "{}").get(
                                "VCodec", ""
                            )
                            codec = codec_val if isinstance(codec_val, str) else ""
                        except json.JSONDecodeError, KeyError, TypeError:
                            codec = ""
                        if "h265" in codec.lower() or "hevc" in codec.lower():
                            hevc_candidate = candidate
                            break
                    json_str = hevc_candidate or first_candidate
                elif "pull_data" in live_core_sdk_data:
                    pull_data = live_core_sdk_data["pull_data"]
                    if isinstance(pull_data, dict):
                        sd = cast(dict[str, object], pull_data).get("stream_data", "")
                        json_str = sd if isinstance(sd, str) else ""
                    else:
                        json_str = ""
                if json_str:
                    parsed_data = cast(dict[str, object], _loads_dict(json_str).get("data") or {})
                    if "origin" in parsed_data:
                        origin_url_list = cast(
                            dict[str, object], cast(dict[str, object], parsed_data["origin"]).get("main") or {}
                        )
                        if origin_url_list:
                            _inject_origin_urls(stream_url, origin_url_list)
                    if hevc_flv_url:
                        stream_url["hevc_flv_url"] = hevc_flv_url
    except Exception as e:
        tb_lineno = e.__traceback__.tb_lineno if e.__traceback__ else 0
        logger.error(i18n.tr("Error message: {e} Error line: {tb_lineno}", e=e, tb_lineno=tb_lineno))
        room_data = cast(dict[str, object], {"anchor_name": ""})
    return room_data


@trace_error_decorator
async def get_douyin_app_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        + "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Referer": url,
        "Cookie": "",
    }
    if cookies:
        headers["Cookie"] = cookies
    else:
        headers["Cookie"] = await get_ttwid(proxy_addr)

    async def get_app_data(room_id: str, sec_uid: str) -> dict[str, object]:
        app_params = {
            "verifyFp": "",
            "type_id": "0",
            "live_id": "1",
            "room_id": room_id,
            "sec_user_id": sec_uid,
            "version_code": "141.0.0.0",
            "app_id": "1128",
        }
        api2 = f"https://webcast.amemv.com/webcast/room/reflow/info/?{urllib.parse.urlencode(app_params)}"
        try:
            json_str2 = _get_str_response(await http.request(url=api2, proxy_addr=proxy_addr, headers=headers))
            if not json_str2:
                raise Exception("empty response from API (possible risk control)")
            parsed2 = _loads_dict(json_str2)
            status_code2 = parsed2.get("status_code")
            if status_code2 is not None and int(cast(str, status_code2)) != 0:
                status_msg2 = parsed2.get("status_msg", "unknown error")
                raise Exception(f"API returned status_code={status_code2}, msg={status_msg2}")
            json_data2 = cast(dict[str, object], parsed2.get("data") or {})
            room_field = json_data2.get("room")
            if not room_field:
                raise Exception(f"{utils.mask_credentials(url)} VR live is not supported or room not found")
            room_data2 = cast(dict[str, object], room_field)
            owner = cast(dict[str, object], room_data2.get("owner") or {})
            room_data2["anchor_name"] = _dig_str(owner, "nickname")
            return room_data2
        except Exception as e:
            raise Exception(f"Douyin app data fetch error, because {e}.")

    async def resolve_from_homepage() -> dict[str, object]:
        unique_id = await get_unique_id(url, proxy_addr=proxy_addr)
        return await get_douyin_web_stream_data(f"https://live.douyin.com/{unique_id}", proxy_addr, cookies)

    try:
        web_rid = url.split("?")[0].split("live.douyin.com/")
        if len(web_rid) > 1:
            return await get_douyin_web_stream_data(url, proxy_addr, cookies)
        if is_user_homepage_url(url):
            return await resolve_from_homepage()
        try:
            data = await get_sec_user_id(url, proxy_addr=proxy_addr)
            if data is None:
                raise RuntimeError("Failed to get sec_user_id")
            _room_id, _sec_uid = data
            room_data = await get_app_data(_room_id, _sec_uid)
        except UnsupportedUrlError:
            return await resolve_from_homepage()

        if room_data.get("status") == 2:
            if "stream_url" not in room_data:
                raise RuntimeError(
                    "The live streaming type or gameplay is not supported on the computer side yet, please use the "
                    + "app to share the link for recording."
                )
            stream_url = cast(dict[str, object], room_data["stream_url"])
            live_core_sdk_data = cast(dict[str, object], stream_url.get("live_core_sdk_data") or {})
            pull_datas = cast(dict[str, object], stream_url.get("pull_datas") or {})
            if live_core_sdk_data:
                if pull_datas:
                    key = list(pull_datas.keys())[0]
                    first_pull = cast(dict[str, object], pull_datas[key])
                    sd0 = first_pull.get("stream_data", "")
                    json_str = sd0 if isinstance(sd0, str) else ""
                else:
                    pull_data = live_core_sdk_data.get("pull_data", {})
                    if isinstance(pull_data, dict):
                        sd0 = cast(dict[str, object], pull_data).get("stream_data", "")
                        json_str = sd0 if isinstance(sd0, str) else ""
                    else:
                        json_str = ""
                if json_str:
                    parsed_data = cast(dict[str, object], _loads_dict(json_str).get("data") or {})
                    if "origin" in parsed_data:
                        origin_url_list = cast(
                            dict[str, object], cast(dict[str, object], parsed_data["origin"]).get("main") or {}
                        )
                        _inject_origin_urls(stream_url, origin_url_list)
    except Exception as e:
        tb_lineno = e.__traceback__.tb_lineno if e.__traceback__ else 0
        logger.error(i18n.tr("Error message: {e} Error line: {tb_lineno}", e=e, tb_lineno=tb_lineno))
        room_data = cast(dict[str, object], {"anchor_name": ""})
    return room_data


_TIKTOK_BUILTIN_GUEST_COOKIE = (
    "1%7Cz7FKki38aKyy7i-BC9rEDwcrVvjcLcFEL6QIeqldoy4%7C1761302831%7C6c1461e9f1f980cbe0404c5190"
    "5177d5d53bbd822e1bf66128887d942c9c3e2f"
)


def _read_tiktok_guest_cookie() -> str:
    override = os.environ.get("TIKTOK_GUEST_COOKIE", "").strip()
    if override:
        return override
    try:
        cfg_value = config_files.read_ini_value(f"{script_path}/config/config.ini", "Cookie", "tiktok_guest_cookie")
    except Exception:
        cfg_value = None
    if cfg_value and cfg_value.strip():
        return cfg_value.strip()
    return _TIKTOK_BUILTIN_GUEST_COOKIE


@trace_error_decorator
async def get_tiktok_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object] | None:
    guest_cookie = cookies or _read_tiktok_guest_cookie()
    using_builtin_guest = guest_cookie == _TIKTOK_BUILTIN_GUEST_COOKIE

    def _warn_if_builtin_guest() -> None:
        if using_builtin_guest:
            logger.warning(
                i18n.tr(
                    "TikTok 解析失败，且本次使用的是内置游客 cookie（可能已过期）；"
                    "请在 config.ini 的 [Cookie] tiktok_guest_cookie 配置有效访客 cookie（或设置环境变量 TIKTOK_GUEST_COOKIE）"
                )
            )

    headers = {
        "referer": "https://www.tiktok.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        + "Chrome/141.0.0.0 Safari/537.36",
        "cookie": guest_cookie,
    }

    for _attempt in range(3):
        html_str = _get_str_response(await http.request(url=url, proxy_addr=proxy_addr, headers=headers, http2=False))
        if "We regret to inform you that we have discontinued operating TikTok" in html_str:
            msg = re.search("<p>\n\\s+(We regret to inform you that we have discontinu.*?)\\.\n\\s+</p>", html_str)
            raise ConnectionError(
                "Your proxy node's regional network is blocked from accessing TikTok; please switch to a node in "
                + f"another region to access. {msg.group(1) if msg else ''}"
            )
        if "UNEXPECTED_EOF_WHILE_READING" not in html_str:
            try:
                json_str_matches = cast(
                    list[str],
                    re.findall('<script id="SIGI_STATE" type="application/json">(.*?)</script>', html_str, re.DOTALL),
                )
                if not json_str_matches:
                    raise ConnectionError("Please check if your network can access the TikTok website normally")
                json_str = json_str_matches[0]
            except Exception:
                _warn_if_builtin_guest()
                raise ConnectionError("Please check if your network can access the TikTok website normally")
            return _loads_dict(json_str)

        if _attempt < 2:
            await asyncio.sleep(1)

    _warn_if_builtin_guest()
    raise ConnectionError(
        "Failed to retrieve TikTok data after 3 retries, please check if your network can access "
        + "the TikTok website normally"
    )


@trace_error_decorator
async def get_kuaishou_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    }
    if cookies:
        headers["Cookie"] = cookies
    html_str = _get_str_response(await http.request(url=url, proxy_addr=proxy_addr, headers=headers))
    if not html_str:
        logger.error(
            i18n.tr(
                "Failed to fetch data from {masked_url}: {type_name}",
                masked_url=utils.mask_credentials(url),
                type_name="EmptyResponse",
            )
        )
        return {"type": 1, "is_live": False}

    try:
        json_str_match = re.search("<script>window.__INITIAL_STATE__=(.*?);\\(function\\(\\)\\{var s;", html_str)
        if not json_str_match:
            raise ValueError("Failed to find __INITIAL_STATE__")
        json_str = json_str_match.group(1)
        play_list_matches = cast(list[str], re.findall('(\\{"liveStream".*?),"gameInfo', json_str))
        if not play_list_matches:
            raise ValueError("Failed to find liveStream")
        play_list = _loads_dict(play_list_matches[0] + "}")
    except (ValueError, AttributeError, IndexError, json.JSONDecodeError) as e:
        logger.error(i18n.tr("Failed to parse JSON data from {url}. Error: {e}", url=utils.mask_credentials(url), e=e))
        return {"type": 1, "is_live": False}

    result: dict[str, object] = {"type": 2, "is_live": False}

    if "errorType" in play_list or "liveStream" not in play_list:
        error_type = cast(dict[str, object], play_list.get("errorType") or {})
        title = error_type.get("title", "")
        content = error_type.get("content", "")
        error_msg = (title if isinstance(title, str) else "") + (content if isinstance(content, str) else "")
        logger.error(
            i18n.tr(
                "Failed URL: {url} Error message: {error_msg}", url=utils.mask_credentials(url), error_msg=error_msg
            )
        )
        return result

    live_stream = cast(dict[str, object], play_list.get("liveStream") or {})
    if not live_stream:
        logger.error("IP banned. Please change device or network.")
        return result

    author = cast(dict[str, object], play_list.get("author") or {})
    anchor_name = _dig_str(author, "name")
    result.update({"anchor_name": anchor_name})

    play_urls_obj = live_stream.get("playUrls")
    if play_urls_obj:
        play_url_list: object
        if isinstance(play_urls_obj, dict):
            play_urls_dict = cast(dict[str, object], play_urls_obj)
            play_url_list = []
            for codec_key in ("h264", "h265"):
                codec_block = play_urls_dict.get(codec_key)
                if not isinstance(codec_block, dict):
                    continue
                adaptation = cast(dict[str, object], codec_block.get("adaptationSet") or {})
                if not isinstance(adaptation, dict):
                    continue
                play_url_list = adaptation.get("representation") or []
                if play_url_list:
                    break
            if not play_url_list:
                _warn_api_abnormal(json.dumps(play_urls_dict, ensure_ascii=False), "快手 playUrls", codec_key, {})
                return result
        else:
            play_urls_list = cast(list[object], play_urls_obj) if isinstance(play_urls_obj, list) else []
            if not play_urls_list:
                return result
            first_item = cast(dict[str, object], play_urls_list[0])
            adaptation2 = cast(dict[str, object], first_item.get("adaptationSet") or {})
            play_url_list = adaptation2.get("representation", [])
        result.update({"flv_url_list": play_url_list, "is_live": True})

    return result


def extract_kuaishou_eid(url: str) -> str:
    """从快手直播链接里取备用接口要的 eid（路径 /u/<eid> 的那一段），取不到返回空串。"""
    try:
        path = urllib.parse.urlsplit(url if "://" in url else f"https://{url}").path
    except ValueError:
        return ""
    segments = [seg for seg in path.split("/") if seg]
    if "u" not in segments:
        return ""
    idx = segments.index("u")
    return segments[idx + 1] if idx + 1 < len(segments) else ""


@trace_error_decorator
async def get_kuaishou_stream_data2(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object] | None:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Referer": "https://live.kuaishou.com/",
        "content-type": "application/json",
    }
    if cookies:
        headers["Cookie"] = cookies
    else:
        headers["Cookie"] = await _ensure_kuaishou_did(proxy_addr)
    try:
        eid = extract_kuaishou_eid(url)
        if not eid:
            raise ValueError("Failed to extract eid from kuaishou URL")
        data: dict[str, object] = {"source": 5, "eid": eid, "shareMethod": "card", "clientType": "WEB_OUTSIDE_SHARE_H5"}
        app_api = "https://livev.m.chenzhongtech.com/rest/k/live/byUser?kpn=GAME_ZONE&captchaToken="
        json_str = _get_str_response(await http.request(url=app_api, proxy_addr=proxy_addr, headers=headers, data=data))
        json_data = _loads_dict(json_str)
        live_stream = cast(dict[str, object], json_data.get("liveStream") or {})
        user = cast(dict[str, object], live_stream.get("user") or {})
        anchor_name = user.get("user_name")
        result: dict[str, object] = {
            "type": 2,
            "anchor_name": anchor_name,
            "is_live": False,
        }
        live_status = live_stream.get("living")
        if live_status:
            result["is_live"] = True
            backup_m3u8_url = live_stream.get("hlsPlayUrl")
            play_urls = cast(list[object], live_stream.get("playUrls") or [])
            first_play = cast(dict[str, object], play_urls[0]) if play_urls else {}
            backup_flv_url = first_play.get("url", "")
            multi_hls = cast(list[object], live_stream.get("multiResolutionHlsPlayUrls") or [])
            if multi_hls:
                first_hls = cast(dict[str, object], multi_hls[0])
                result["m3u8_url_list"] = first_hls.get("urls", [])
            multi_play = cast(list[object], live_stream.get("multiResolutionPlayUrls") or [])
            if multi_play:
                first_mp = cast(dict[str, object], multi_play[0])
                result["flv_url_list"] = first_mp.get("urls", [])
            result["backup"] = {"m3u8_url": backup_m3u8_url, "flv_url": backup_flv_url}
        if result["anchor_name"]:
            return result
    except Exception as e:
        logger.error(
            i18n.tr(
                "{e}, Failed URL: {url}, preparing to switch to a backup plan for re-parsing.",
                e=e,
                url=utils.mask_credentials(url),
            )
        )
    return await get_kuaishou_stream_data(url, cookies=cookies, proxy_addr=proxy_addr)


@trace_error_decorator
async def get_huya_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    }
    if cookies:
        headers["Cookie"] = cookies

    html_str = _get_str_response(await http.request(url=url, proxy_addr=proxy_addr, headers=headers))
    json_str_matches = cast(list[str], re.findall('stream: (\\{"data".*?),"iWebDefaultBitRate"', html_str))
    if not json_str_matches:
        raise ValueError("Failed to find stream data")
    json_str = json_str_matches[0]
    return _loads_dict(json_str + "}")


@trace_error_decorator
async def get_huya_app_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "xweb_xhr": "1",
        "referer": "https://servicewechat.com/wx74767bf0b684f7d3/301/page-frame.html",
        "accept-language": "zh-CN,zh;q=0.9",
    }

    if cookies:
        headers["Cookie"] = cookies
    room_id = url.split("?")[0].rstrip("/").rsplit("/", maxsplit=1)[-1]

    if any(char.isalpha() for char in room_id):
        html_str = _get_str_response(await http.request(url, proxy_addr=proxy_addr, headers=headers))
        room_id_match = re.search('"ProfileRoom":(.*?),"sPrivateHost', html_str)
        if room_id_match:
            room_id = room_id_match.group(1).strip().strip('"')
    if not room_id.isdigit():
        raise Exception('Please use "https://www.huya.com/+room_number" for recording')

    params = {
        "m": "Live",
        "do": "profileRoom",
        "roomid": room_id,
        "showSecret": "1",
    }
    wx_app_api = f"https://mp.huya.com/cache.php?{urllib.parse.urlencode(params)}"
    json_str = _get_str_response(await http.request(url=wx_app_api, proxy_addr=proxy_addr, headers=headers))
    json_data = _loads_dict(json_str)
    data_field = cast(dict[str, object], json_data.get("data") or {})
    profile_info = cast(dict[str, object], data_field.get("profileInfo") or {})
    anchor_name = _dig_str(profile_info, "nick")
    live_status = data_field.get("realLiveStatus")
    live_data = cast(dict[str, object], data_field.get("liveData") or {})
    live_title = live_data.get("introduction")
    if live_status != "ON":
        return {"anchor_name": anchor_name, "is_live": False}
    stream_field = cast(dict[str, object], data_field.get("stream") or {})
    base_steam_info_list = cast(list[object], stream_field.get("baseSteamInfoList") or [])
    play_url_list: list[dict[str, object]] = []
    for i_obj in base_steam_info_list:
        i = cast(dict[str, object], i_obj)
        cdn_type = i.get("sCdnType")
        stream_name = i.get("sStreamName")
        flv_anti_code = i.get("sFlvAntiCode")
        s_flv_url = i.get("sFlvUrl")
        s_hls_url = i.get("sHlsUrl")
        hls_anti_code = i.get("sHlsAntiCode")
        if not stream_name:
            continue

        def _normalize(base: object, anti: object, suffix: str) -> str:
            """直接使用接口返回的原始防盗链参数，统一降为 http（实测 https 403），并对所有。"""
            if not (isinstance(base, str) and isinstance(anti, str) and base and anti):
                return ""
            url = f"{base}/{stream_name}.{suffix}?{anti}"
            url = url.replace("https://", "http://")
            return url.replace("&ctype=tars_mp", "&ctype=huya_webh5").replace("&fs=bhct", "&fs=bgct")

        m3u8_url = _normalize(s_hls_url, hls_anti_code, "m3u8")
        flv_url = _normalize(s_flv_url, flv_anti_code, "flv")
        if m3u8_url or flv_url:
            play_url_list.append({"cdn_type": cdn_type, "m3u8_url": m3u8_url, "flv_url": flv_url})

    if not play_url_list:
        return {"anchor_name": anchor_name, "is_live": True}

    cdn_priority = ["HS", "HW", "TX", "AL"]

    def _rank(item: dict[str, object]) -> int:
        try:
            return cdn_priority.index(str(item.get("cdn_type")))
        except ValueError:
            return len(cdn_priority)

    ordered = sorted(play_url_list, key=_rank)
    selected_item = ordered[0]

    selected_m3u8 = selected_item.get("m3u8_url")
    selected_flv = selected_item.get("flv_url")
    selected_m3u8_url: str | None = selected_m3u8 if isinstance(selected_m3u8, str) else None
    selected_flv_url: str | None = selected_flv if isinstance(selected_flv, str) else None

    record_url = selected_flv_url or None

    first_steam_info = cast(dict[str, object], base_steam_info_list[0] if base_steam_info_list else {})
    yyid = profile_info.get("yyid")
    l_channel_id = data_field.get("chTopId") or first_steam_info.get("lChannelId")
    l_sub_channel_id = data_field.get("subChId") or first_steam_info.get("lSubChannelId")

    m3u8_url_list = [c.get("m3u8_url") for c in ordered if isinstance(c.get("m3u8_url"), str) and c.get("m3u8_url")]
    flv_url_list = [c.get("flv_url") for c in ordered if isinstance(c.get("flv_url"), str) and c.get("flv_url")]

    return {
        "anchor_name": anchor_name,
        "is_live": True,
        "m3u8_url": selected_m3u8_url,
        "m3u8_url_list": m3u8_url_list,
        "flv_url": selected_flv_url,
        "flv_url_list": flv_url_list,
        "record_url": record_url,
        "title": live_title,
        "yyid": yyid,
        "lChannelId": l_channel_id,
        "lSubChannelId": l_sub_channel_id,
    }


def md5(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


async def get_token_js(rid: str, did: str, proxy_addr: OptionalStr = None) -> dict[str, object]:
    try:
        key_url = f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            + "Chrome/141.0.0.0 Safari/537.36",
            "Referer": f"https://www.douyu.com/{rid}",
        }
        json_str = _get_str_response(await http.request(url=key_url, proxy_addr=proxy_addr, headers=headers))
        key_data = _loads_dict(json_str)
        if key_data.get("error") != 0:
            return {}
        enc_key = cast(dict[str, object], key_data.get("data") or {})
        ts = int(time.time())
        rand_str_v = enc_key.get("rand_str")
        key_v = enc_key.get("key")
        if not isinstance(rand_str_v, str) or not rand_str_v or not isinstance(key_v, str) or not key_v:
            logger.warning(
                i18n.tr("斗鱼 websec 签名参数不完整: rand_str/key 缺失或类型异常，本轮跳过取流"),
            )
            return {}
        auth = rand_str_v
        key = key_v
        try:
            enc_time = int(str(enc_key.get("enc_time")))
        except TypeError, ValueError:
            logger.warning(
                i18n.tr("斗鱼 websec 签名参数不完整: enc_time 缺失或非数值，本轮跳过取流"),
            )
            return {}
        enc_time = min(max(enc_time, 0), 32)
        sign_str = "" if enc_key.get("is_special") == 1 else f"{rid}{ts}"
        for _ in range(enc_time):
            auth = md5(auth + key)
        auth = md5(auth + key + sign_str)
        return {"enc_data": enc_key.get("enc_data"), "did": did, "ts": ts, "auth": auth}
    except Exception as e:
        logger.error(i18n.tr("Get douyu sign params error: {e}", e=e))
        return {}


def extract_douyu_rid_param(url: str) -> str:
    """斗鱼房间号提取：两种来源（?rid= 查询参数 / 路径首段）与一处归一。"""
    match = re.search(r"[?&]rid=([^&#]*)", url)
    return match.group(1) if match else ""


def extract_douyu_room_id(url: str) -> str:
    masked_url = utils.mask_credentials(url)
    try:
        path = urllib.parse.urlsplit(url if "://" in url else f"https://{url}").path
    except ValueError as e:
        raise ValueError(i18n.tr("斗鱼链接无法解析房间号: {masked_url}", masked_url=masked_url)) from e
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        raise ValueError(i18n.tr("斗鱼链接缺少房间号: {masked_url}", masked_url=masked_url))
    return segments[0]


@trace_error_decorator
async def get_douyu_info_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "Referer": "https://m.douyu.com/3125893?rid=3125893&dyshid=0-96003918aa5365bc6dcb4933000316p1&dyshci=181",
    }
    if cookies:
        headers["Cookie"] = cookies

    query_rid = extract_douyu_rid_param(url)
    if query_rid.isdigit():
        rid = query_rid
    else:
        rid_candidate = extract_douyu_room_id(url)
        html_str = await http.request(
            url=f"https://m.douyu.com/{rid_candidate}", proxy_addr=proxy_addr, headers=headers
        )
        html_str = _get_str_response(html_str)
        json_str_matches = re.findall('<script id="vike_pageContext" type="application/json">(.*?)</script>', html_str)
        if not json_str_matches:
            raise ValueError("Failed to find vike_pageContext")
        json_str = json_str_matches[0]
        json_data = _loads_dict(json_str)
        page_props = cast(dict[str, object], json_data.get("pageProps") or {})
        room_wrap = cast(dict[str, object], page_props.get("room") or {})
        room_info = cast(dict[str, object], room_wrap.get("roomInfo") or {})
        inner_room = cast(dict[str, object], room_info.get("roomInfo") or {})
        if inner_room.get("rid") is None:
            raise ValueError("Failed to find rid in vike_pageContext (WAF page? truncated JSON?)")
        rid = str(inner_room["rid"])
        if not rid.isdigit():
            raise ValueError(
                i18n.tr("斗鱼房间号还原结果非数字: candidate={candidate} rid={rid}", candidate=rid_candidate, rid=rid)
            )

    headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0"
    url2 = f"https://www.douyu.com/betard/{rid}"
    json_str = await http.request(url=url2, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    room = cast(dict[str, object], json_data.get("room") or {})
    if not room:
        reason = (
            i18n.tr("空响应（风控或网络失败）")
            if not json_str
            else i18n.tr("响应缺少 room 字段（房间不存在 / 接口改版 / WAF 页）")
        )
        logger.warning(i18n.tr("斗鱼 betard 响应异常: rid={rid} reason={reason}", rid=rid, reason=reason))
        raise ValueError("douyu betard response has no room info: " + reason)
    nickname_v = room.get("nickname")
    result: dict[str, object] = {"anchor_name": nickname_v if isinstance(nickname_v, str) else "", "is_live": False}
    if room.get("videoLoop") == 0 and room.get("show_status") == 1:
        room_name_v = room.get("room_name")
        result["title"] = room_name_v.replace("&nbsp;", "") if isinstance(room_name_v, str) else ""
        result["is_live"] = True
        result["room_id"] = room.get("room_id")
    return result


@trace_error_decorator
async def get_douyu_stream_data(
    rid: str, rate: str = "-1", proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    did = "10000000000000000000000000003306"
    sign_params = await get_token_js(rid, did, proxy_addr=proxy_addr)
    if not sign_params:
        return {"error": -1, "msg": "Failed to get sign params", "data": {}}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36",
        "Referer": f"https://www.douyu.com/{rid}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if cookies:
        headers["Cookie"] = cookies

    enc_data_raw = sign_params.get("enc_data")
    if not isinstance(enc_data_raw, str) or not enc_data_raw:
        logger.warning("Douyu websec sign params incomplete: enc_data missing, skip getH5PlayV1 this round")
        return {}
    post_data = {
        "enc_data": enc_data_raw,
        "tt": str(sign_params["ts"]),
        "did": str(sign_params["did"]),
        "auth": str(sign_params["auth"]),
        "cdn": "",
        "rate": rate,
        "hevc": "0",
        "fa": "0",
        "ive": "0",
    }
    app_api = f"https://www.douyu.com/lapi/live/getH5PlayV1/{rid}"
    json_str = await http.request(url=app_api, proxy_addr=proxy_addr, headers=headers, data=post_data)
    json_str = _get_str_response(json_str)
    return _loads_dict(json_str)


@trace_error_decorator
async def get_yy_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Referer": "https://www.yy.com/",
    }
    if cookies:
        headers["Cookie"] = cookies

    html_str = await http.request(url=url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    anchor_name_match = re.search('nick: "(.*?)",\n\\s+logo', html_str)
    if not anchor_name_match:
        raise ValueError("Failed to find anchor name")
    anchor_name = anchor_name_match.group(1)
    cid_match = re.search('sid : "(.*?)",\n\\s+ssid', html_str, re.DOTALL)
    if not cid_match:
        raise ValueError("Failed to find cid")
    cid = cid_match.group(1)

    data = (
        '{"head":{"seq":1701869217590,"appidstr":"0","bidstr":"121","cidstr":"'
        + cid
        + '","sidstr":"'
        + cid
        + '","uid64":0,"client_type":108,"client_ver":"5.17.0","stream_sys_ver":1,"app":"yylive_web","playersdk_ver":"5.17.0","thundersdk_ver":"0","streamsdk_ver":"5.17.0"},"client_attribute":{"client":"web","model":"web0","cpu":"","graphics_card":"","os":"chrome","osversion":"0","vsdk_version":"","app_identify":"","app_version":"","business":"","width":"1920","height":"1080","scale":"","client_type":8,"h265":0},"avp_parameter":{"version":1,"client_type":8,"service_type":0,"imsi":0,"send_time":1701869217,"line_seq":-1,"gear":4,"ssl":1,"stream_format":0}}'
    )
    data_bytes = data.encode("utf-8")
    params = {"uid": "0", "cid": cid, "sid": cid, "appid": "0", "sequence": "1701869217590", "encode": "json"}
    url2 = f"https://stream-manager.yy.com/v3/channel/streams?{urllib.parse.urlencode(params)}"
    json_str = await http.request(url=url2, data=data_bytes, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if not json_data:
        _warn_api_abnormal(json_str, "YY stream-manager", "avp_info_res", json_data)
    json_data["anchor_name"] = anchor_name

    params = {
        "uid": "",
        "sid": cid,
        "ssid": cid,
        "_": int(time.time() * 1000),
    }
    detail_api = f"https://www.yy.com/live/detail?{urllib.parse.urlencode(params)}"
    try:
        json_str2 = await http.request(detail_api, proxy_addr=proxy_addr, headers=headers)
        json_str2 = _get_str_response(json_str2)
        json_data2 = _loads_dict(json_str2)
        _detail_data = json_data2.get("data")
        if isinstance(_detail_data, dict):
            json_data["title"] = _detail_data.get("roomName") or json_data.get("title", "")
    except Exception as e:
        logger.warning(i18n.tr("YY 标题补取失败（不影响取流）: {type_name}", type_name=type(e).__name__))
        json_data["title"] = json_data.get("title", "")
    return cast(dict[str, object], json_data)


@trace_error_decorator_or_none
async def get_bilibili_room_info_h5(url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None) -> str:
    headers = {
        "user-agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Mobile Safari/537.36",
        "accept-language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "cookie": "",
        "origin": "https://live.bilibili.com",
        "referer": "https://live.bilibili.com/26066074",
    }
    if cookies:
        headers["cookie"] = cookies

    room_id = _safe_extract_id(url)
    api = f"https://api.live.bilibili.com/xlive/web-room/v1/index/getH5InfoByRoom?room_id={room_id}"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    room_info = _loads_dict(json_str)
    if _dig(room_info, "data") is None:
        _warn_api_abnormal(json_str, "B站 getH5InfoByRoom", "data.room_info.title", room_info)
    return _dig_str(room_info, "data", "room_info", "title")


@trace_error_decorator
async def get_bilibili_room_info(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    }
    if cookies:
        headers["Cookie"] = cookies

    try:
        room_id = _safe_extract_id(url)
        json_str = await http.request(
            f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}", proxy_addr=proxy_addr, headers=headers
        )
        json_str = _get_str_response(json_str)
        room_info = _loads_dict(json_str)
        uid = _dig(room_info, "data", "uid")
        if uid is None:
            _warn_api_abnormal(json_str, "B站 room_init", "data.uid", room_info)
            return {"anchor_name": "", "live_status": False, "room_url": url}
        live_status = _dig(room_info, "data", "live_status") == 1

        api = f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}"
        json_str2 = await http.request(url=api, proxy_addr=proxy_addr, headers=headers)
        json_str2 = _get_str_response(json_str2)
        anchor_info = _loads_dict(json_str2)
        anchor_name = _dig_str(anchor_info, "data", "info", "uname")
        if not anchor_name:
            _warn_api_abnormal(json_str2, "B站 master/info", "data.info.uname", anchor_info)
            return {"anchor_name": "", "live_status": False, "room_url": url}

        title = await get_bilibili_room_info_h5(url, proxy_addr, cookies) or ""
        return {"anchor_name": anchor_name, "live_status": live_status, "room_url": url, "title": title}
    except Exception as e:
        logger.warning(
            i18n.tr(
                "B站房间信息获取失败（下轮重试）: url={url} - {type_name}: {e}",
                url=utils.mask_credentials(url),
                type_name=type(e).__name__,
                e=e,
            )
        )
        return {"anchor_name": "", "live_status": False, "room_url": url}


@trace_error_decorator
async def get_bilibili_stream_data(
    url: str, qn: str = "10000", platform: str = "web", proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object] | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "origin": "https://live.bilibili.com",
        "referer": "https://live.bilibili.com/26066074",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = _safe_extract_id(url)
    params = {"cid": room_id, "qn": qn, "platform": platform}
    play_api = f"https://api.live.bilibili.com/room/v1/Room/playUrl?{urllib.parse.urlencode(params)}"
    json_str = await http.request(play_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if json_data.get("code") == 0:
        play_data = cast(dict[str, object], json_data.get("data") or {})
        durl_list = cast(list[dict[str, object]], play_data.get("durl", []))
        if not durl_list:
            return None
        target_url = None
        for i in durl_list:
            if "d1--cn-gotcha" in str(i.get("url", "")):
                target_url = i["url"]
                break
        if not target_url:
            target_url = durl_list[-1].get("url")
        return {"url": target_url, "current_qn": qn, "accept_qn": [qn]}
    params = {
        "room_id": room_id,
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0,1,2",
        "qn": qn,
        "platform": "web",
        "ptype": "8",
        "dolby": "5",
        "panorama": "1",
        "hdr_type": "0,1",
    }
    api = f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?{urllib.parse.urlencode(params)}"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if not json_data:
        _warn_api_abnormal(json_str, "B站 getRoomPlayInfo", "code + data", json_data)
        return None
    if json_data.get("code") is not None and json_data.get("code") != 0:
        logger.warning(
            i18n.tr("B站接口返回异常 code={code}（疑似风控/签名失效），本轮跳过", code=json_data.get("code"))
        )
        return None
    data_obj = json_data.get("data") or {}
    if not isinstance(data_obj, dict):
        return None
    if data_obj.get("live_status") == 0:
        logger.info("The anchor did not start broadcasting.")
        return None
    playurl_info = data_obj.get("playurl_info") or {}
    if not isinstance(playurl_info, dict):
        return None
    _playurl = playurl_info.get("playurl") or {}
    stream_list = _playurl.get("stream", []) if isinstance(_playurl, dict) else []
    if not stream_list:
        return None
    format_list = _dig_list(stream_list, 0, "format")
    if not format_list:
        return None
    stream_data_list = _dig_list(format_list, 0, "codec")
    if not stream_data_list:
        return None
    sorted_stream_list: list[dict[str, object]] = sorted(
        (cast(dict[str, object], item) for item in stream_data_list if isinstance(item, dict)),
        key=_qn_sort_key,
        reverse=True,
    )
    if not sorted_stream_list:
        return None
    video_quality_options = {"10000": 0, "400": 1, "250": 2, "150": 3, "80": 4}
    qn_count = len(sorted_stream_list)
    requested_index = video_quality_options.get(qn, 0)
    select_stream_index = min(requested_index, qn_count - 1)
    if select_stream_index != requested_index:
        logger.warning(
            i18n.tr(
                "B站请求清晰度 {qn} 不可用，就近降级到 {actual_qn}",
                qn=qn,
                actual_qn=sorted_stream_list[select_stream_index].get("current_qn", ""),
            )
        )
    stream_data: dict[str, object] = sorted_stream_list[select_stream_index]
    base_url = _dig_str(stream_data, "base_url")
    url_info = _dig_list(stream_data, "url_info")
    if not url_info:
        return None
    url_info_list = cast(list[dict[str, object]], url_info)
    host = cast(str, url_info_list[0].get("host", ""))
    extra = cast(str, url_info_list[0].get("extra", ""))
    m3u8_url = host + base_url + extra
    current_qn = str(stream_data.get("current_qn", qn))
    accept_qn = [str(s.get("current_qn")) for s in sorted_stream_list]
    return {"url": m3u8_url, "current_qn": current_qn, "accept_qn": accept_qn}


_MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    6,
    60,
    21,
    57,
    59,
    62,
    11,
    36,
    20,
    51,
    54,
    25,
    1,
    34,
    56,
    30,
    4,
    22,
    44,
    52,
    63,
    0,
]


def _get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def _sign_wbi(params: dict[str, str], img_key: str, sub_key: str) -> dict[str, str]:
    signed = dict(params)
    signed.pop("w_rid", None)
    mixin_key = _get_mixin_key(img_key + sub_key)
    signed["wts"] = str(int(time.time()))
    query = urllib.parse.urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


_bili_buvid_lock = threading.Lock()
_bili_buvid_cached = ""
_bili_buvid_is_fallback = False
_bili_buvid_cached_proxy = ""


def _take_cached_bili_buvid(proxy_addr: OptionalStr) -> str:
    if not _bili_buvid_cached:
        return ""
    if _bili_buvid_cached_proxy != (proxy_addr or ""):
        return ""
    return _bili_buvid_cached


def _buvid_log_form(buvid: str) -> str:
    """buvid3 进日志的唯一形态：只留「前缀 4 位 + 长度」指纹，整值不落盘。buvid3 是设备级标识、。"""
    text = str(buvid or "")
    if not text:
        return ""
    return f"{text[:4]} len={len(text)}"


def invalidate_bili_buvid_cache(proxy_addr: OptionalStr = None) -> None:
    global _bili_buvid_cached, _bili_buvid_is_fallback, _bili_buvid_cached_proxy
    with _bili_buvid_lock:
        _bili_buvid_cached = ""
        _bili_buvid_is_fallback = False
        recorded_proxy = _bili_buvid_cached_proxy
        _bili_buvid_cached_proxy = ""
    keys = {recorded_proxy}
    if proxy_addr is not None:
        keys.add(proxy_addr or "")
    for k in keys:
        credential_cache.invalidate(f"bili_buvid3|{k}")


async def get_bilibili_danmaku_info(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object] | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "origin": "https://live.bilibili.com",
        "referer": "https://live.bilibili.com",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = _safe_extract_id(url)

    real_room_id = room_id
    uid = 0
    init_api = f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}"
    init_str = _get_str_response(await http.request(init_api, proxy_addr=proxy_addr, headers=headers))
    init_data = _loads_dict(init_str)
    init_room: dict[str, object] = {}
    if isinstance(init_data, dict):
        d = init_data.get("data")
        if isinstance(d, dict):
            init_room = d
    if not init_room:
        _warn_api_abnormal(init_str, "B站 room_init", "data.room_id + data.uid", init_data)
    real_room_id = str(init_room.get("room_id") or room_id)
    _uid = init_room.get("uid")
    uid = int(_uid) if isinstance(_uid, int) else 0
    img_key = ""
    sub_key = ""
    try:
        nav_str = await http.request(
            "https://api.bilibili.com/x/web-interface/nav", proxy_addr=proxy_addr, headers=headers
        )
        nav_data = _loads_dict(_get_str_response(nav_str))
        nav_wbi: dict[str, object] = {}
        if isinstance(nav_data, dict):
            w = nav_data.get("data")
            if isinstance(w, dict):
                nw = w.get("wbi_img")
                if isinstance(nw, dict):
                    nav_wbi = nw
        img_url = str(nav_wbi.get("img_url", ""))
        sub_url = str(nav_wbi.get("sub_url", ""))
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    except Exception as e:
        logger.warning(i18n.tr("[B站直播]nav(wbi) 获取失败: {type_name}: {e}", type_name=type(e).__name__, e=e))

    global _bili_buvid_cached, _bili_buvid_is_fallback, _bili_buvid_cached_proxy

    async def _fetch_buvid() -> str:
        b = _take_cached_bili_buvid(proxy_addr)
        if not b and cookies:
            _m = re.search(r"buvid3=([^;\s]+)", str(cookies))
            if _m and _m.group(1).strip():
                b = _m.group(1).strip()
                logger.debug(i18n.tr("[B站直播]使用 cookie 中的 buvid3: {buvid}", buvid=_buvid_log_form(b)))
        if not b:
            for _attempt in range(2):
                try:
                    spi_str = await http.request(
                        "https://api.bilibili.com/x/frontend/finger/spi", proxy_addr=proxy_addr, headers=headers
                    )
                    spi_data = _loads_dict(_get_str_response(spi_str))
                    spi_d: dict[str, object] = {}
                    if isinstance(spi_data, dict):
                        s = spi_data.get("data")
                        if isinstance(s, dict):
                            spi_d = s
                    b = str(spi_d.get("b_3") or spi_d.get("buvid") or "")
                    if b:
                        break
                    if _attempt == 0:
                        await asyncio.sleep(0.5 + random.random() * 0.5)
                    else:
                        logger.warning(
                            i18n.tr("[B站直播]spi 两跳均未返回 buvid（疑似风控 200+空 body），改走首页 Set-Cookie 兜底")
                        )
                except Exception as e:
                    if _attempt == 0:
                        logger.debug(
                            i18n.tr(
                                "[B站直播]buvid 获取失败(将重试): {type_name}: {e}", type_name=type(e).__name__, e=e
                            )
                        )
                        await asyncio.sleep(0.5 + random.random() * 0.5)
                    else:
                        logger.warning(
                            i18n.tr("[B站直播]buvid 获取失败: {type_name}: {e}", type_name=type(e).__name__, e=e)
                        )
        if not b:
            try:
                home_cookies = await fetch_cookies("https://www.bilibili.com/", proxy_addr=proxy_addr, headers=headers)
                b = str(home_cookies.get("buvid3", "")).strip()
                if b:
                    logger.debug(
                        i18n.tr("[B站直播]spi 失败，从首页 Set-Cookie 获取 buvid3: {buvid}", buvid=_buvid_log_form(b))
                    )
            except Exception as e:
                logger.debug(
                    i18n.tr(
                        "[B站直播]首页 Set-Cookie 获取 buvid3 失败: {type_name}: {e}", type_name=type(e).__name__, e=e
                    )
                )
        return b

    buvid = _take_cached_bili_buvid(proxy_addr)
    if not buvid:

        async def _fetch_with_fallback() -> tuple[str, bool]:
            """兜底 UUID 也须走 singleflight：否则并发下各协程生成不同 UUID、后写覆盖先写。"""
            b = await _fetch_buvid()
            if b:
                return b, False
            b = str(uuid.uuid4())
            logger.debug(
                i18n.tr("[B站直播]spi/首页均无 buvid，使用生成兜底 buvid3（未注册，AUTH 可能被拒）: {buvid}", buvid=b)
            )
            return b, True

        got = await credential_cache.get_or_fetch(
            key=f"bili_buvid3|{proxy_addr or ''}",
            factory=_fetch_with_fallback,
            wait_timeout=15,
            cache_falsy=True,  # 兜底 UUID 恒非空；置 True 仅为防御 factory 返回空串
        )
        if isinstance(got, tuple) and len(got) == 2 and isinstance(got[0], str) and got[0]:
            buvid, _bili_buvid_is_fallback = got[0], bool(got[1])
        else:
            buvid = str(uuid.uuid4())
            _bili_buvid_is_fallback = True
    with _bili_buvid_lock:
        _bili_buvid_cached = buvid
        _bili_buvid_cached_proxy = proxy_addr or ""

    danmu_params: dict[str, str] = {"id": real_room_id, "type": "0", "web_location": "444.8"}
    if img_key and sub_key:
        try:
            danmu_params = _sign_wbi(danmu_params, img_key, sub_key)
        except Exception as e:
            logger.warning(i18n.tr("[B站直播]wbi 签名失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
    try:
        danmu_str = await http.request(
            f"https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?"
            f"{urllib.parse.urlencode(danmu_params)}",
            proxy_addr=proxy_addr,
            headers=headers,
        )
        danmu_data = _loads_dict(_get_str_response(danmu_str))
        danmu: dict[str, object] = {}
        if isinstance(danmu_data, dict):
            dd = danmu_data.get("data")
            if isinstance(dd, dict):
                danmu = dd
        if not danmu:
            logger.warning("[B站直播]getDanmuInfo 返回空（可能 -352 风控，检查 wbi 签名/cookie）")
            return None
        token = str(danmu.get("token", ""))
        host_list_raw = danmu.get("host_list")
        host_list: list[str] = []
        if isinstance(host_list_raw, list):
            host_list = [str(h.get("host", "")) for h in host_list_raw if isinstance(h, dict) and h.get("host")]
        if not host_list:
            logger.warning("[B站直播]getDanmuInfo 无可用 host")
            return None
        server_host = host_list[0]
    except Exception as e:
        logger.warning(i18n.tr("[B站直播]getDanmuInfo 失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        return None

    return {
        "room_id": int(real_room_id) if str(real_room_id).isdigit() else real_room_id,
        "uid": uid,
        "token": token,
        "server_host": server_host,
        "host_list": host_list,
        "buvid": buvid,
        "cookie": cookies or "",
    }


_XHS_BUILTIN_SESSION_SID = "session.1722166379345546829388"

_XHS_ALLOWED_HOST_SUFFIXES = ("xiaohongshu.com", "xhslink.com", "xhscdn.com")


def _xhs_is_allowed_host(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _XHS_ALLOWED_HOST_SUFFIXES)


class XhsUrlError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        """code 是稳定的机器可读分类；消息只描述类别，不回显可能携带凭据的原始 URL/path。"""
        self.code = code
        super().__init__(message)


_XHS_MAIN_HOST = "www.xiaohongshu.com"
_XHS_SHORT_HOST = "xhslink.com"
_XHS_QUERY_KEYS = ("host_id", "xsec_token", "xsec_source")
_XHS_LIVE_PATH_RE = re.compile(r"^/livestream/([0-9]+)/?$")
_XHS_LIVE_DYNAMIC_PATH_RE = re.compile(r"^/livestream/[A-Za-z0-9_-]+/([0-9]+)/?$")
_XHS_SHORT_PATH_RE = re.compile(r"^/o/[A-Za-z0-9_-]+/?$")
_XHS_PROFILE_PATH_RE = re.compile(r"^/user/profile/([A-Za-z0-9_-]+)/?$")


def parse_xhs_live_url(url: str) -> dict[str, str]:
    raw = (url or "").strip()
    if not raw:
        raise XhsUrlError("format", "小红书链接为空")
    try:
        parsed = urllib.parse.urlparse(raw)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as e:
        raise XhsUrlError("format", "小红书链接格式非法") from e
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise XhsUrlError("scheme", "小红书链接协议不受支持（只接受 http/https）")
    if parsed.username is not None or parsed.password is not None:
        raise XhsUrlError("userinfo", "小红书链接不允许携带用户信息（userinfo）")
    if port is not None and port != (443 if scheme == "https" else 80):
        raise XhsUrlError("port", "小红书链接不允许指定非默认端口")
    host = host.lower()
    if host not in (_XHS_MAIN_HOST, _XHS_SHORT_HOST):
        raise XhsUrlError("host", f"小红书链接域名不受支持: {host or '空'}")

    path = parsed.path or "/"
    kept = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    pairs = [(key, kept[key][0]) for key in _XHS_QUERY_KEYS if kept.get(key)]
    normalized_url = f"https://{host}{path.rstrip('/')}"
    if pairs:
        normalized_url += "?" + urllib.parse.urlencode(pairs)

    if host == _XHS_SHORT_HOST:
        if not _XHS_SHORT_PATH_RE.match(path):
            raise XhsUrlError("short_path", "小红书短链路径必须是 /o/<短码>")
        return {"kind": "short", "normalized_url": normalized_url, "room_id": "", "user_id": ""}
    profile_match = _XHS_PROFILE_PATH_RE.match(path)
    if profile_match:
        return {
            "kind": "profile",
            "normalized_url": normalized_url,
            "room_id": "",
            "user_id": profile_match.group(1),
        }
    dynamic_match = _XHS_LIVE_DYNAMIC_PATH_RE.match(path)
    if dynamic_match:
        room_id = dynamic_match.group(1)
    else:
        live_match = _XHS_LIVE_PATH_RE.match(path)
        if not live_match:
            if path == "/livestream" or path.startswith("/livestream/"):
                raise XhsUrlError("room_id", "小红书直播链接的房间 ID 缺失或非数字")
            raise XhsUrlError("non_live_path", "小红书链接不是直播间或作者主页地址")
        room_id = live_match.group(1)
    return {
        "kind": "live",
        "normalized_url": normalized_url,
        "room_id": room_id,
        "user_id": dict(pairs).get("host_id", ""),
    }


def _read_xhs_sid() -> str:
    override = os.environ.get("XHS_SESSION_SID", "").strip()
    if override:
        return override
    try:
        cfg_value = config_files.read_ini_value(f"{script_path}/config/config.ini", "Cookie", "xhs_session_sid")
    except Exception:
        cfg_value = None
    if cfg_value and cfg_value.strip():
        return cfg_value.strip()
    return _XHS_BUILTIN_SESSION_SID


@trace_error_decorator
async def get_xhs_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    try:
        link_info = parse_xhs_live_url(url)
    except XhsUrlError as e:
        logger.warning(
            i18n.tr(
                "小红书链接解析失败（不发送任何带凭据请求）: {url} - {code}",
                url=utils.mask_credentials(url),
                code=e.code,
            )
        )
        return {"anchor_name": "", "is_live": False, "error": e.code}
    if not _xhs_is_allowed_host(url):
        logger.warning(
            i18n.tr(
                "小红书直播间地址不在白名单域内，拒绝解析（不发送任何带凭据请求）: {url}",
                url=utils.mask_credentials(url),
            )
        )
        return {"anchor_name": "", "is_live": False}
    sid_value = _read_xhs_sid()
    using_builtin_sid = sid_value == _XHS_BUILTIN_SESSION_SID

    def _warn_if_builtin_sid() -> None:
        if using_builtin_sid:
            logger.warning(
                i18n.tr(
                    "小红书解析失败，且本次使用的是内置会话 sid（可能已过期）；"
                    "请在 config.ini 的 [Cookie] xhs_session_sid 配置有效 sid（或设置环境变量 XHS_SESSION_SID）"
                )
            )

    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "xy-common-params": "platform=iOS&sid=" + sid_value,
        "referer": "https://app.xhs.cn/",
    }
    if cookies:
        headers["Cookie"] = cookies

    if link_info["kind"] == "short":
        url_result = await http.request(url, proxy_addr=proxy_addr, headers=headers, redirect_url=True)
        if not isinstance(url_result, str) or not url_result.strip():
            logger.warning(i18n.tr("小红书短链重定向失败，本轮按解析失败处理: {url}", url=utils.mask_credentials(url)))
            return {"anchor_name": "", "is_live": False, "error": "short_redirect"}
        _landing = url_result.strip()
        if _xhs_is_allowed_host(_landing):
            try:
                _landing_host = (urllib.parse.urlparse(_landing).hostname or "").lower()
                _internal_reason = targets.host_rejection_reason(_landing_host) if _landing_host else "缺少主机名"
            except Exception as e:
                _internal_reason = f"内网判定异常: {type(e).__name__}"
            if _internal_reason is None:
                try:
                    link_info = parse_xhs_live_url(_landing)
                except XhsUrlError as e:
                    logger.warning(
                        i18n.tr(
                            "小红书短链落地页不是可识别的直播间，已忽略跳转: {url} - {code}",
                            url=utils.mask_credentials(_landing),
                            code=e.code,
                        )
                    )
                    return {"anchor_name": "", "is_live": False, "error": e.code}
                url = link_info["normalized_url"]
            else:
                logger.warning(
                    i18n.tr(
                        "小红书短链落地页不可信，已忽略跳转: {url} ({reason})",
                        url=utils.mask_credentials(_landing),
                        reason=_internal_reason,
                    )
                )
        else:
            logger.warning(
                i18n.tr(
                    "小红书短链跳转到非白名单主机，已忽略跳转: {url}",
                    url=utils.mask_credentials(_landing),
                )
            )
    else:
        url = link_info["normalized_url"]

    host_id = get_params(url, "host_id") or link_info["user_id"]
    user_id = link_info["user_id"] or host_id or ""
    result: dict[str, object] = {
        "anchor_name": "",
        "is_live": False,
        "room_id": link_info["room_id"],
        "normalized_url": link_info["normalized_url"],
    }
    html_str = await http.request(url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    match_data = re.search("<script>window.__INITIAL_STATE__=(.*?)</script>", html_str)
    anchor_name = ""

    if match_data:
        json_str = match_data.group(1).replace("undefined", "null")
        json_data = _loads_dict(json_str)
        if not json_data:
            _warn_api_abnormal(json_str, "小红书 __INITIAL_STATE__", "liveStream", json_data)

        if _dig(json_data, "liveStream"):
            stream_data = _dig(json_data, "liveStream")
            anchor_name = _dig_str(stream_data, "roomData", "hostInfo", "nickName")
            if _dig(stream_data, "liveStatus") == "success":
                room_info = _dig(stream_data, "roomData", "roomInfo")
                if not isinstance(room_info, dict):
                    _warn_api_abnormal(json_str, "小红书 __INITIAL_STATE__", "roomData.roomInfo", json_data)
                    return result
                title = _dig_str(room_info, "roomTitle")
                if title and "回放" not in title:
                    live_link = _dig_str(room_info, "deeplink")
                    deeplink_name = get_params(live_link, "host_nickname")
                    if deeplink_name:
                        anchor_name = deeplink_name
                    flv_url = get_params(live_link, "flvUrl")
                    if not flv_url:
                        raise RuntimeError("Failed to get flvUrl")
                    room_id_match = re.search(r"live/([^./?]+)", flv_url)
                    if not room_id_match:
                        raise RuntimeError("Failed to extract room_id from flvUrl")
                    room_id = room_id_match.group(1)
                    flv_url = f"http://live-source-play.xhscdn.com/live/{room_id}.flv"
                    m3u8_url = flv_url.replace(".flv", ".m3u8")
                    result |= {
                        "anchor_name": anchor_name,
                        "is_live": True,
                        "title": title,
                        "flv_url": flv_url,
                        "m3u8_url": m3u8_url,
                        "record_url": flv_url,
                        "room_id": room_id,
                    }
                    return result
        if anchor_name:
            result["anchor_name"] = anchor_name
    else:
        _warn_api_abnormal(html_str, "小红书 房间页", "__INITIAL_STATE__", {})
        if html_str:
            _warn_if_builtin_sid()

    if not result.get("anchor_name") and user_id:
        profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        html_str = await http.request(profile_url, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        anchor_name_match = re.search("<title>@(.*?) 的个人主页</title>", html_str)
        if anchor_name_match:
            result["anchor_name"] = anchor_name_match.group(1)
    if not result.get("anchor_name") and link_info["room_id"]:
        result["anchor_name"] = f"小红书_{link_info['room_id']}"

    return result


@trace_error_decorator
async def get_bigo_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": "https://www.bigo.tv/",
    }
    if cookies:
        headers["Cookie"] = cookies

    if "bigo.tv" not in url:
        html_str = await http.request(url, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        web_url_match = re.search(
            '<meta data-n-head="ssr" data-hid="al:web:url" property="al:web:url" content="(.*?)">', html_str
        )
        if not web_url_match:
            raise ValueError("Failed to find web url")
        web_url = web_url_match.group(1)
        web_url = web_url.replace("&amp;", "&")
        room_id = urllib.parse.parse_qs(urllib.parse.urlsplit(web_url).query).get("h", [""])[0]
        if not room_id:
            room_id = urllib.parse.urlsplit(web_url).path.rstrip("/").rsplit("/", maxsplit=1)[-1]
        if not room_id:
            raise ValueError("Failed to extract bigo room id from web url")
    else:
        if "&h=" in url:
            room_id = url.split("&h=")[-1]
        else:
            room_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]

    data = {"siteId": room_id}  # roomId
    url2 = "https://ta.bigo.tv/official_website/studio/getInternalStudioInfo"
    json_str = await http.request(url=url2, proxy_addr=proxy_addr, headers=headers, data=data)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    studio_info = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(studio_info, "alive") is None:
        _warn_api_abnormal(json_str, "Bigo getInternalStudioInfo", "data.alive", json_data)
        return {"anchor_name": _dig_str(studio_info, "nick_name"), "is_live": False}
    anchor_name = _dig_str(studio_info, "nick_name")
    live_status = _dig(studio_info, "alive")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}

    if live_status == 1:
        m3u8_url = _dig_str(studio_info, "hls_src")
        if not m3u8_url:
            _warn_api_abnormal(json_str, "Bigo getInternalStudioInfo", "data.hls_src", json_data)
            return result
        live_title = _dig_str(studio_info, "roomTopic")
        result["m3u8_url"] = m3u8_url
        result["record_url"] = m3u8_url
        result |= {"title": live_title, "is_live": True, "m3u8_url": m3u8_url, "record_url": m3u8_url}
    elif result["anchor_name"] == "":
        url_path_segments = [seg for seg in urllib.parse.urlsplit(url).path.split("/") if seg]
        site_segment = url_path_segments[0] if url_path_segments else "bigo"
        html_str = await http.request(
            url=f"https://www.bigo.tv/{site_segment}/{room_id}", proxy_addr=proxy_addr, headers=headers
        )
        html_str = _get_str_response(html_str)
        match_anchor_name = re.search("<title>欢迎来到(.*?)的直播间</title>", html_str, re.DOTALL)
        if match_anchor_name:
            anchor_name = match_anchor_name.group(1)
        else:
            match_anchor_name = re.search(
                '<meta data-n-head="ssr" data-hid="og:title" property="og:title" content="(.*?) - BIGO LIVE">',
                html_str,
                re.DOTALL,
            )
            anchor_name = match_anchor_name.group(1) if match_anchor_name else ""
        result["anchor_name"] = anchor_name

    return result


@trace_error_decorator
async def get_blued_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    }
    if cookies:
        headers["Cookie"] = cookies

    html_str = await http.request(url=url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    json_str_match = re.search('decodeURIComponent\\("(.*?)"\\)\\),window\\.Promise', html_str, re.DOTALL)
    if not json_str_match:
        raise ValueError("Failed to find json string")
    json_str = json_str_match.group(1)
    json_str = urllib.parse.unquote(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "userInfo") is None:
        _warn_api_abnormal(json_str, "blued 页面内联数据", "userInfo", json_data)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(json_data, "userInfo", "name")
    live_status = _dig(json_data, "userInfo", "onLive")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}

    if live_status:
        m3u8_url = _dig_str(json_data, "liveInfo", "liveUrl")
        if not m3u8_url:
            _warn_api_abnormal(json_str, "blued 页面内联数据", "liveInfo.liveUrl", json_data)
            return result
        result |= {"is_live": True, "m3u8_url": m3u8_url, "record_url": m3u8_url}
    return result


_SOOP_BJ_LEAD_SEGMENTS = ("play", "channel", "live")


def _soop_bj_id_from_url(url: str) -> str:
    segments = [seg for seg in urllib.parse.urlparse(url).path.split("/") if seg]
    if not segments:
        return ""
    if segments[0].lower() in _SOOP_BJ_LEAD_SEGMENTS and len(segments) > 1:
        return segments[1]
    return segments[0]


@trace_error_decorator_or_none
async def login_sooplive(username: str, password: str, proxy_addr: OptionalStr = None) -> OptionalStr:
    if len(username) < 6 or len(password) < 10:
        raise RuntimeError(
            "sooplive login failed! Please enter the correct account and password for the sooplive "
            "platform in the config.ini file."
        )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://play.sooplive.co.kr",
        "Referer": "https://play.sooplive.co.kr/superbsw123/277837074",
    }

    data = {
        "szWork": "login",
        "szType": "json",
        "szUid": username,
        "szPassword": password,
        "isSaveId": "true",
        "isSavePw": "true",
        "isSaveJoin": "true",
        "isLoginRetain": "Y",
    }

    url = "https://login.sooplive.co.kr/app/LoginAction.php"

    try:
        cookie_result = await http.request(
            url, proxy_addr=proxy_addr, headers=headers, data=data, return_cookies=True, timeout=20
        )
        if isinstance(cookie_result, dict):
            cookie_dict = cookie_result
        elif isinstance(cookie_result, tuple) and len(cookie_result) == 2 and isinstance(cookie_result[1], dict):
            cookie_dict = cookie_result[1]
        else:
            raise RuntimeError("Failed to get cookies from login response")
        return "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
    except Exception as e:
        logger.error(i18n.tr("An error occurred during login: {e}", e=e))
        raise Exception(
            "sooplive login failed, please check if the account password in the configuration file is correct."
        )


@trace_error_decorator
async def get_sooplive_cdn_url(
    broad_no: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Origin": "https://play.sooplive.co.kr",
        "Referer": "https://play.sooplive.co.kr/oul282/249469582",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if cookies:
        headers["Cookie"] = cookies

    params = {
        "return_type": "gcp_cdn",
        "use_cors": "false",
        "cors_origin_url": "play.sooplive.co.kr",
        "broad_key": f"{broad_no}-common-master-hls",
        "time": "8361.086329376785",
    }

    url2 = "http://livestream-manager.sooplive.co.kr/broad_stream_assign.html?" + urllib.parse.urlencode(params)
    json_str = await http.request(url=url2, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "view_url") is None:
        _warn_api_abnormal(json_str, "SOOP broad_stream_assign", "view_url", json_data)
    return json_data


@trace_error_decorator_or_none
async def get_sooplive_tk(
    url: str, rtype: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> str | tuple[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Origin": "https://play.sooplive.co.kr",
        "Referer": "https://play.sooplive.co.kr/secretx/250989857",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    if cookies:
        headers["Cookie"] = cookies

    bj_id = _soop_bj_id_from_url(url)
    room_password = get_params(url, "pwd")
    if not room_password:
        room_password = ""
    data = {
        "bid": bj_id,
        "bno": "",
        "type": rtype,
        "pwd": room_password,
        "player_type": "html5",
        "stream_type": "common",
        "quality": "master",
        "mode": "landing",
        "from_api": "0",
        "is_revive": "false",
    }

    url2 = f"https://live.sooplive.co.kr/afreeca/player_live_api.php?bjid={bj_id}"
    json_str = await http.request(url=url2, proxy_addr=proxy_addr, headers=headers, data=data)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    channel_info = cast(dict[str, object], _dig(json_data, "CHANNEL") or {})
    needed_keys = ("AID",) if rtype == "aid" else ("BJNICK", "BJID", "BNO")
    if any(_dig(channel_info, key) is None for key in needed_keys):
        _warn_api_abnormal(json_str, "SOOP player_live_api", "CHANNEL." + "/".join(needed_keys), json_data)
        raise RuntimeError("Failed to get sooplive CHANNEL data")

    if rtype == "aid":
        token = _dig(channel_info, "AID")
        return cast(str, token)
    bj_name = _dig(channel_info, "BJNICK")
    bj_id_value = _dig(channel_info, "BJID")
    return f"{bj_name}-{bj_id_value}", cast(str, _dig(channel_info, "BNO"))


def get_soop_headers(cookies: OptionalStr = None) -> dict[str, str]:
    headers = {
        "client-id": str(uuid.uuid4()),
        "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, "
        "like Gecko) Version/18.5 Mobile/15E148 Safari/604.1 Edg/141.0.0.0",
    }
    if cookies:
        headers["cookie"] = cookies
    return headers


async def _get_soop_channel_info_global(bj_id: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None) -> str:
    headers = get_soop_headers(cookies)
    api = "https://api.sooplive.com/v2/channel/info/" + str(bj_id)
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    channel_info = _dig(json_data, "data", "streamerChannelInfo")
    if _dig(channel_info, "nickname") is None or _dig(channel_info, "channelId") is None:
        _warn_api_abnormal(json_str, "SOOP channel/info", "data.streamerChannelInfo", json_data)
        return ""
    nickname = _dig(channel_info, "nickname")
    channelId = _dig(channel_info, "channelId")
    return f"{nickname}-{channelId}"


async def _get_soop_stream_info_global(
    bj_id: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> tuple[bool, str]:
    headers = get_soop_headers(cookies)
    api = "https://api.sooplive.com/v2/stream/info/" + str(bj_id)
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    stream_info = _dig(json_data, "data")
    status = _dig(stream_info, "isStream")
    if status is None:
        _warn_api_abnormal(json_str, "SOOP stream/info", "data.isStream", json_data)
        return False, ""
    return cast(bool, status), _dig_str(stream_info, "title")


async def _fetch_web_stream_data_global(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    bj_id = _soop_bj_id_from_url(url)
    anchor_name = await _get_soop_channel_info_global(bj_id, proxy_addr=proxy_addr, cookies=cookies)
    result: dict[str, object] = {"anchor_name": anchor_name or "", "is_live": False, "live_url": url}
    status, title = await _get_soop_stream_info_global(bj_id, proxy_addr=proxy_addr, cookies=cookies)
    if not status:
        return result

    async def _get_url_list(m3u8: str) -> list[str]:
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        }
        if cookies:
            headers["cookie"] = cookies
        resp = await http.request(url=m3u8, proxy_addr=proxy_addr, headers=headers)
        resp = _get_str_response(resp)
        play_url_list = []
        for i in resp.split("\n"):
            line = i.strip()
            if not line.startswith("#") and line:
                play_url_list.append(urllib.parse.urljoin(m3u8, line))
        bandwidth_pattern = _BANDWIDTH_PATTERN
        bandwidth_list = bandwidth_pattern.findall(resp)
        url_to_bandwidth = {purl: int(bandwidth) for bandwidth, purl in zip(bandwidth_list, play_url_list)}
        return sorted(play_url_list, key=lambda purl: url_to_bandwidth.get(purl, 0), reverse=True)

    m3u8_url = "https://global-media.sooplive.com/live/" + bj_id + "/master.m3u8"
    result |= {
        "is_live": True,
        "title": title,
        "m3u8_url": m3u8_url,
        "play_url_list": await _get_url_list(m3u8_url),
    }
    return result


@trace_error_decorator
async def get_sooplive_stream_data(
    url: str,
    proxy_addr: OptionalStr = None,
    cookies: OptionalStr = None,
    username: OptionalStr = None,
    password: OptionalStr = None,
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Referer": "https://m.sooplive.co.kr/",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if cookies:
        headers["Cookie"] = cookies

    if "sooplive.com" in url:
        return await _fetch_web_stream_data_global(url, proxy_addr, cookies)

    bj_id = _soop_bj_id_from_url(url)

    data = {
        "bj_id": bj_id,
        "broad_no": "",
        "agent": "web",
        "confirm_adult": "true",
        "player_type": "webm",
        "mode": "live",
    }

    url2 = "http://api.m.sooplive.co.kr/broad/a/watch"

    json_str = await http.request(url=url2, proxy_addr=proxy_addr, headers=headers, data=data)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    watch_data = cast(dict[str, object], _dig(json_data, "data") or {})

    if _dig(watch_data, "user_nick") is not None:
        anchor_name = _dig_str(watch_data, "user_nick")
        bj_id_value = _dig(watch_data, "bj_id")
        if bj_id_value is not None:
            anchor_name = f"{anchor_name}-{bj_id_value}"
    else:
        anchor_name = ""

    result: dict[str, object] = {"anchor_name": anchor_name or "", "is_live": False}

    async def get_url_list(m3u8: str) -> list[str]:
        resp = await http.request(url=m3u8, proxy_addr=proxy_addr, headers=headers)
        resp = _get_str_response(resp)
        play_url_list = []
        url_prefix = m3u8.rsplit("/", maxsplit=1)[0] + "/"
        for i in resp.split("\n"):
            if i.startswith("auth_playlist"):
                play_url_list.append(url_prefix + i.strip())
        bandwidth_pattern = _BANDWIDTH_PATTERN
        bandwidth_list = bandwidth_pattern.findall(resp)
        url_to_bandwidth = {purl: int(bandwidth) for bandwidth, purl in zip(bandwidth_list, play_url_list)}
        return sorted(play_url_list, key=lambda purl: url_to_bandwidth.get(purl, 0), reverse=True)

    if not anchor_name:

        async def handle_login() -> OptionalStr:
            cookie = await login_sooplive(cast(str, username), cast(str, password), proxy_addr=proxy_addr)
            if cookie and "AuthTicket=" in cookie:
                logger.info("sooplive platform login successful! Starting to fetch live streaming data...")
                return cookie
            return None

        async def fetch_data(cookie: str, _result: dict[str, object]) -> dict[str, object]:
            aid_token = await get_sooplive_tk(url, rtype="aid", proxy_addr=proxy_addr, cookies=cookie)
            _info = await get_sooplive_tk(url, rtype="info", proxy_addr=proxy_addr, cookies=cookie)
            if not isinstance(aid_token, str) or not aid_token:
                return _result
            if not (isinstance(_info, tuple) and len(_info) == 2):
                return _result
            _anchor_name, _broad_no = _info
            _view_url_data = await get_sooplive_cdn_url(_broad_no, proxy_addr=proxy_addr)
            _view_url = cast(str, _view_url_data.get("view_url", ""))
            _m3u8_url = _view_url + "?aid=" + aid_token
            _result |= {
                "anchor_name": _anchor_name,
                "is_live": True,
                "m3u8_url": _m3u8_url,
                "play_url_list": await get_url_list(_m3u8_url),
                "new_cookies": cookie,
            }
            return _result

        soop_code = _dig(watch_data, "code")
        if soop_code is None:
            _warn_api_abnormal(json_str, "SOOP broad/a watch", "data.code", json_data)
            return result

        if soop_code == -3001:
            logger.error("sooplive live stream failed to retrieve, the live stream just ended.")
            return result

        if soop_code == -3002:
            logger.error("sooplive live stream retrieval failed, the live needs 19+, you are not logged in.")
            logger.warning(
                "Attempting to log in to the sooplive live streaming platform with your account and password, "
                "please ensure it is configured."
            )
            new_cookie = await handle_login()
            if new_cookie and len(new_cookie) > 0:
                return await fetch_data(new_cookie, result)
            raise RuntimeError("sooplive login failed, please check if the account and password are correct")

        if soop_code == -3004:
            if cookies:
                reused = await fetch_data(cast(str, cookies), result)
                if reused.get("is_live"):
                    return reused
                logger.warning(
                    "The playback ticket of the cookie you provided was rejected, retrying with the "
                    "sooplive account and password in the configuration file."
                )
            new_cookie = await handle_login()
            if new_cookie and len(new_cookie) > 0:
                return await fetch_data(new_cookie, result)
            raise RuntimeError("sooplive login failed, please check if the account and password are correct")
        if soop_code == -6001:
            logger.error("error message：Please check if the input sooplive live room address is correct.")
            return result
    if _dig(json_data, "result") == 1 and anchor_name:
        broad_no = _dig(watch_data, "broad_no")
        hls_authentication_key = _dig(watch_data, "hls_authentication_key")
        if broad_no is None or hls_authentication_key is None:
            _warn_api_abnormal(json_str, "SOOP broad/a watch", "data.broad_no + hls_authentication_key", json_data)
        else:
            view_url_data = await get_sooplive_cdn_url(cast(str, broad_no), proxy_addr=proxy_addr)
            view_url = cast(str, view_url_data.get("view_url", ""))
            m3u8_url = view_url + "?aid=" + cast(str, hls_authentication_key)
            result |= {"is_live": True, "m3u8_url": m3u8_url, "play_url_list": await get_url_list(m3u8_url)}
    result.setdefault("new_cookies", None)
    return result


@trace_error_decorator
async def get_netease_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://cc.163.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies
    url = url + "/" if url[-1] != "/" else url

    html_str = await http.request(url=url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    json_str_match = re.search(
        '<script id="__NEXT_DATA__" .* crossorigin="anonymous">(.*?)</script></body>', html_str, re.DOTALL
    )
    if not json_str_match:
        raise ValueError("Failed to find __NEXT_DATA__")
    json_str = json_str_match.group(1)
    json_data = _loads_dict(json_str)
    room_data = _dig(json_data, "props", "pageProps", "roomInfoInitData")
    live_data = _dig(room_data, "live")
    if not isinstance(live_data, dict):
        _warn_api_abnormal(json_str, "网易CC __NEXT_DATA__", "roomInfoInitData.live", json_data)
        return {"anchor_name": _dig_str(room_data, "nickname"), "is_live": False}
    live_data = cast(dict[str, object], live_data)
    result: dict[str, object] = {"is_live": False}
    live_status = live_data.get("status") == 1
    result["anchor_name"] = _dig_str(live_data, "nickname") or _dig_str(room_data, "nickname")
    if live_status:
        result |= {
            "is_live": True,
            "title": _dig_str(live_data, "title"),
            "stream_list": live_data.get("quickplay"),
            "m3u8_url": _dig_str(live_data, "sharefile"),
        }
    return result


@trace_error_decorator
async def get_qiandurebo_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://qiandurebo.com/web/index.php",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    html_str = await http.request(url=url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    data_match = re.search("var user = (.*?)\r\n\\s+user\\.play_url", html_str, re.DOTALL)
    if not data_match:
        return {"anchor_name": "", "is_live": False}
    data = data_match.group(1)
    anchor_name = re.findall('"zb_nickname": "(.*?)",\r\n', data)

    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    if len(anchor_name) > 0:
        result["anchor_name"] = anchor_name[0]
        play_url = re.findall('"play_url": "(.*?)",\r\n', data)

        play_url_value = play_url[0].strip() if play_url else ""
        if play_url_value and 'common-text-center" style="display:block' not in html_str:
            result |= {
                "anchor_name": anchor_name[0],
                "is_live": True,
                "flv_url": play_url_value,
                "record_url": play_url_value,
            }
        elif play_url and not play_url_value:
            _warn_api_abnormal(data, "千度热播 user.play_url", "play_url", {})
    return result


@trace_error_decorator
async def get_pandatv_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "origin": "https://www.pandalive.co.kr",
        "referer": "https://www.pandalive.co.kr/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    user_id = _safe_extract_id(url)
    url2 = "https://api.pandalive.co.kr/v1/live/play"
    data = {
        "userId": user_id,
        "info": "media fanGrade",
    }
    room_password = get_params(url, "pwd")
    if not room_password:
        room_password = ""
    data2 = {
        "action": "watch",
        "userId": user_id,
        "password": room_password,
        "shareLinkType": "",
    }

    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    json_str = await http.request(
        "https://api.pandalive.co.kr/v1/member/bj", proxy_addr=proxy_addr, headers=headers, data=data
    )
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    bj_info = _dig(json_data, "bjInfo")
    if bj_info is None:
        _warn_api_abnormal(json_str, "PandaTV member/bj", "bjInfo", json_data)
        raise RuntimeError(cast(str, json_data.get("message", "Unknown error")))
    anchor_id = _dig(bj_info, "id")
    anchor_nick = _dig(bj_info, "nick")
    anchor_name = f"{anchor_nick}-{anchor_id}"
    result["anchor_name"] = anchor_name
    live_status = "media" in json_data

    if live_status:
        json_str = await http.request(url2, proxy_addr=proxy_addr, headers=headers, data=data2)
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        error_data = _dig(json_data, "errorData")
        if error_data is not None:
            error_code = _dig(error_data, "code")
            if error_code == "needAdult":
                raise RuntimeError(
                    f"{utils.mask_credentials(url)} "
                    f"The live room requires login and is only accessible to adults. Please "
                    f"correctly fill in the login cookie in the configuration file."
                )
            raise RuntimeError(cast(object, error_code), _dig(json_data, "message"))
        play_url = _dig_str(json_data, "PlayList", "hls", 0, "url")
        if not play_url:
            _warn_api_abnormal(json_str, "PandaTV live/play", "PlayList.hls[0].url", json_data)
            return result
        play_url_list = await get_play_url_list(m3u8=play_url, proxy=proxy_addr, header=headers)
        result |= {"is_live": True, "m3u8_url": play_url, "play_url_list": play_url_list}
    return result


@trace_error_decorator
async def get_maoerfm_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://fm.missevan.com/live/868895007",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = _safe_extract_id(url)
    url2 = f"https://fm.missevan.com/api/v2/live/{room_id}"

    json_str = await http.request(url=url2, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "info") is None:
        _warn_api_abnormal(json_str, "猫耳FM api/v2/live", "info", json_data)
        return {"anchor_name": "", "is_live": False}
    info = _dig(json_data, "info")
    anchor_name = _dig_str(info, "creator", "username")
    if not anchor_name:
        _warn_api_abnormal(json_str, "猫耳FM api/v2/live", "info.creator.username", json_data)
    live_status: object = False
    room = cast(dict[str, object], _dig(info, "room") or {})
    if room:
        live_status = _dig(room, "status", "broadcasting")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": live_status}
    if live_status:
        m3u8_url = _dig_str(room, "channel", "hls_pull_url")
        flv_url = _dig_str(room, "channel", "flv_pull_url")
        title = _dig_str(room, "name")
        if not flv_url and not m3u8_url:
            _warn_api_abnormal(json_str, "猫耳FM api/v2/live", "room.channel", json_data)
            return {"anchor_name": anchor_name, "is_live": False}
        result |= {"is_live": True, "title": title, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator_or_none
async def get_winktv_bj_info(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> tuple[str, object]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "content-type": "application/x-www-form-urlencoded",
        "referer": "https://www.winktv.co.kr/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies
    user_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    data = {
        "userId": user_id,
        "info": "media",
    }

    info_api = "https://api.winktv.co.kr/v1/member/bj"
    json_str = await http.request(url=info_api, proxy_addr=proxy_addr, headers=headers, data=data)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    live_status = "media" in json_data
    bj_info = _dig(json_data, "bjInfo")
    if bj_info is None:
        _warn_api_abnormal(json_str, "WinkTV member/bj", "bjInfo", json_data)
        raise RuntimeError(cast(str, json_data.get("message", "Unknown error")))
    anchor_id = _dig(bj_info, "id")
    anchor_nick = _dig(bj_info, "nick")
    anchor_name = f"{anchor_nick}-{anchor_id}"
    return anchor_name, live_status


@trace_error_decorator
async def get_winktv_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "content-type": "application/x-www-form-urlencoded",
        "referer": "https://www.winktv.co.kr",
        "origin": "https://www.winktv.co.kr",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies
    user_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    room_password = get_params(url, "pwd")
    if not room_password:
        room_password = ""
    data = {
        "action": "watch",
        "userId": user_id,
        "password": room_password,
        "shareLinkType": "",
    }

    bj_info = await get_winktv_bj_info(url=url, proxy_addr=proxy_addr, cookies=cookies)
    if not bj_info:
        return {"anchor_name": "", "is_live": False}
    anchor_name, live_status = bj_info
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": live_status}
    if live_status:
        play_api = "https://api.winktv.co.kr/v1/live/play"
        json_str = await http.request(url=play_api, proxy_addr=proxy_addr, headers=headers, data=data)
        json_str = _get_str_response(json_str)
        if "403: Forbidden" in json_str:
            raise ConnectionError(f"Your network has been banned from accessing WinkTV ({json_str})")
        json_data = _loads_dict(json_str)
        error_data = _dig(json_data, "errorData")
        if error_data is not None:
            error_code = _dig(error_data, "code")
            if error_code == "needAdult":
                raise RuntimeError(
                    f"{utils.mask_credentials(url)} "
                    f"The live stream is only accessible to logged-in adults. Please ensure that "
                    f"the cookie is correctly filled in the configuration file after logging in."
                )
            raise RuntimeError(cast(object, error_code), _dig(json_data, "message"))
        m3u8_url = _dig_str(json_data, "PlayList", "hls", 0, "url")
        if not m3u8_url:
            _warn_api_abnormal(json_str, "WinkTV live/play", "PlayList.hls[0].url", json_data)
            result["is_live"] = False
            return result
        play_url_list = await get_play_url_list(m3u8=m3u8_url, proxy=proxy_addr, header=headers)
        result["m3u8_url"] = m3u8_url
        result["play_url_list"] = play_url_list
    return result


@trace_error_decorator_or_none
async def login_flextv(username: str, password: str, proxy_addr: OptionalStr = None) -> OptionalStr:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "content-type": "application/json;charset=UTF-8",
        "referer": "https://www.ttinglive.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    data = {
        "loginId": username,
        "password": password,
        "loginKeep": True,
        "saveId": True,
        "device": "PCWEB",
    }

    url = "https://www.ttinglive.com/v2/api/auth/signin"

    try:
        logger.info("Logging into FlexTV platform...")
        cookie_result = await http.request(
            url, proxy_addr=proxy_addr, headers=headers, json_data=data, return_cookies=True, timeout=20
        )

        if isinstance(cookie_result, dict):
            cookie_dict = cookie_result
        elif isinstance(cookie_result, tuple) and len(cookie_result) == 2 and isinstance(cookie_result[1], dict):
            cookie_dict = cookie_result[1]
        else:
            cookie_dict = {}

        if cookie_dict and "flx_oauth_access" in cookie_dict:
            return "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
        logger.warning("Please check if the FlexTV account and password in the configuration file are correct.")
        return None

    except Exception as e:
        logger.error(i18n.tr("FlexTV login request exception: {e}", e=e))
        raise Exception(
            "FlexTV login failed, please check if the account and password in the configuration file are correct."
        )


async def get_flextv_stream_url(url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None) -> str | None:
    async def fetch_data(cookie: OptionalStr = None) -> dict[str, object]:
        """获取 TTingLive(原Flextv) 直播流地址：返回 str|None（None 表示未开播/无源）。"""
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "referer": "https://www.ttinglive.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        }
        user_id = url.split("/live")[0].rsplit("/", maxsplit=1)[-1]
        if cookie:
            headers["Cookie"] = cookie
        play_api = f"https://www.ttinglive.com/api/channels/{user_id}/stream?option=all"
        json_str = await http.request(play_api, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        if "HTTP Error 400: Bad Request" in json_str:
            raise ConnectionError(
                "Failed to retrieve FlexTV live streaming data, please switch to a different proxy and try again."
            )
        json_data = _loads_dict(json_str)
        if _dig(json_data, "sources") is None:
            _warn_api_abnormal(json_str, "TTingLive channels/stream", "sources", json_data)
        return json_data

    json_data = await fetch_data(cookies)
    sources = json_data.get("sources")
    if sources and isinstance(sources, list) and len(sources) > 0:
        first_source = sources[0]
        if isinstance(first_source, dict):
            return cast(str, first_source.get("url", ""))
    return None


@trace_error_decorator
async def get_flextv_stream_data(
    url: str,
    proxy_addr: OptionalStr = None,
    cookies: OptionalStr = None,
    username: OptionalStr = None,
    password: OptionalStr = None,
) -> dict[str, object]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://www.ttinglive.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies
    user_id = url.split("/live")[0].rsplit("/", maxsplit=1)[-1]
    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    new_cookies = None
    try:
        url2 = f"https://www.ttinglive.com/channels/{user_id}/live"
        html_str = await http.request(url2, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        json_str_match = re.search('<script id="__NEXT_DATA__" type=".*">(.*?)</script>', html_str)
        if not json_str_match:
            raise ValueError("Failed to find __NEXT_DATA__")
        json_str = json_str_match.group(1)
        json_data = _loads_dict(json_str)
        channel_data = _dig(json_data, "props", "pageProps", "channel")
        if not isinstance(channel_data, dict):
            _warn_api_abnormal(json_str, "TTingLive channel 页(__NEXT_DATA__)", "props.pageProps.channel", json_data)
            raise ValueError("Failed to get FlexTV channel data")
        login_need = "로그인후 이용이 가능합니다." in _dig_str(channel_data, "message")
        if login_need:
            logger.error(
                "FlexTV live stream retrieval failed [not logged in]: 19+ live streams are only available for "
                "logged-in adults."
            )
            logger.warning(
                "Attempting to log in to the FlexTV live streaming platform, please ensure your account and "
                "password are correctly filled in the configuration file."
            )
            if not username or not password or len(username) < 6 or len(password) < 8:
                raise RuntimeError(
                    i18n.tr(
                        "TTingLive(原Flextv)登录失败！请在config.ini配置文件中填写正确的TTingLive(原Flextv)平台的账号和密码"
                    )
                )
            new_cookies = await login_flextv(username, password, proxy_addr=proxy_addr)
            if new_cookies:
                logger.info("Logged into FlexTV platform successfully! Starting to fetch live streaming data...")
            else:
                raise RuntimeError("TTingLive(原Flextv) login failed")
            cookies = new_cookies if new_cookies else cookies
            if cookies:
                headers["Cookie"] = cookies
            html_str = await http.request(url2, proxy_addr=proxy_addr, headers=headers)
            html_str = _get_str_response(html_str)
            json_str_match = re.search('<script id="__NEXT_DATA__" type=".*">(.*?)</script>', html_str)
            if not json_str_match:
                raise ValueError("Failed to find __NEXT_DATA__")
            json_str = json_str_match.group(1)
            json_data = _loads_dict(json_str)
            channel_data = _dig(json_data, "props", "pageProps", "channel")
            if not isinstance(channel_data, dict):
                _warn_api_abnormal(
                    json_str, "TTingLive channel 页(__NEXT_DATA__)", "props.pageProps.channel", json_data
                )
                raise ValueError("Failed to get FlexTV channel data")

        live_status = _dig(channel_data, "message") is None
        if live_status:
            anchor_id = _dig(channel_data, "owner", "loginId")
            if anchor_id is None:
                _warn_api_abnormal(
                    json_str, "TTingLive channel 页(__NEXT_DATA__)", "props.pageProps.channel.owner", json_data
                )
                raise ValueError("Failed to get FlexTV owner data")
            anchor_nick = _dig(channel_data, "owner", "nickname")
            anchor_name = f"{anchor_nick}-{anchor_id}"
            result["anchor_name"] = anchor_name
            play_url = await get_flextv_stream_url(url=url, proxy_addr=proxy_addr, cookies=cookies)
            if play_url:
                result["is_live"] = True
                if ".m3u8" in play_url:
                    play_url_list = await get_play_url_list(m3u8=play_url, proxy=proxy_addr, header=headers)
                    if play_url_list:
                        result["m3u8_url"] = play_url
                        result["play_url_list"] = play_url_list
                else:
                    result["flv_url"] = play_url
                    result["record_url"] = play_url
        else:
            url2 = f"https://www.ttinglive.com/channels/{user_id}"
            html_str = await http.request(url2, proxy_addr=proxy_addr, headers=headers)
            html_str = _get_str_response(html_str)
            anchor_name_match = re.search('<meta name="twitter:title" content="(.*?)의', html_str)
            if anchor_name_match:
                anchor_name = anchor_name_match.group(1)
                result["anchor_name"] = anchor_name
    except Exception as e:
        logger.error("Failed to retrieve data from FlexTV live room " + str(e))
    result["new_cookies"] = new_cookies
    return result


def get_looklive_secret_data(text: str | dict[str, str]) -> tuple[str, str]:

    modulus = (
        "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee"
        "341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe487"
        "5d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
    )
    nonce = b"0CoJUm6Qyw8W8jud"
    public_key = "010001"
    import base64
    import binascii
    import secrets

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    def create_secret_key(size: int) -> bytes:
        charset = "1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()_+-=[]{}|;:,.<>?"
        return "".join(secrets.choice(charset) for _ in range(size)).encode("utf-8")

    def aes_encrypt(_text: str | bytes, _sec_key: str | bytes) -> bytes:
        if isinstance(_text, str):
            _text = _text.encode("utf-8")
        if isinstance(_sec_key, str):
            _sec_key = _sec_key.encode("utf-8")
        _sec_key = _sec_key[:16]  # AES-128 固定 16 字节密钥
        iv = bytes("0102030405060708", "utf-8")
        encryptor = AES.new(_sec_key, AES.MODE_CBC, iv)
        padded_text = pad(_text, AES.block_size)
        ciphertext = encryptor.encrypt(padded_text)
        return base64.b64encode(ciphertext)

    def rsa_encrypt(_text: str | bytes, pub_key: str, mod: str) -> str:
        if isinstance(_text, str):
            _text = _text.encode("utf-8")
        text_reversed = _text[::-1]
        text_int = int(binascii.hexlify(text_reversed), 16)
        encrypted_int = pow(text_int, int(pub_key, 16), int(mod, 16))
        return format(encrypted_int, "x").zfill(256)

    sec_key = create_secret_key(16)
    enc_text = aes_encrypt(aes_encrypt(json.dumps(text), nonce), sec_key)
    enc_sec_key = rsa_encrypt(sec_key, public_key, modulus)
    return enc_text.decode(), enc_sec_key


@trace_error_decorator
async def get_looklive_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    """与其余 50+ 平台入口保持一致，必须保留兜底装饰器：缺失时任意异常（含 room_id 正则。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept": "application/json, text/javascript",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://look.163.com/",
    }

    if cookies:
        headers["Cookie"] = cookies

    room_id_match = re.search(r"live\?id=([^&?#]+)", url)
    if not room_id_match:
        raise ValueError("Failed to find room id in url")
    room_id = room_id_match.group(1)
    params, secretkey = get_looklive_secret_data({"liveRoomNo": room_id})
    request_data = {"params": params, "encSecKey": secretkey}
    api = "https://api.look.163.com/weapi/livestream/room/get/v3"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers, data=request_data)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    room_block = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(room_block, "liveStatus") is None:
        _warn_api_abnormal(json_str, "Look 房间接口 room/get/v3", "data.liveStatus", json_data)
        return {"anchor_name": _dig_str(room_block, "anchor", "nickName"), "is_live": False}
    anchor_name = _dig_str(room_block, "anchor", "nickName")
    live_status = _dig(room_block, "liveStatus")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        result["is_live"] = True
        room_info = cast(dict[str, object], _dig(room_block, "roomInfo") or {})
        if _dig(room_info, "liveType") == 1:
            logger.info("Look live currently only supports audio live streaming, not video live streaming!")
        else:
            play_url_list = cast(dict[str, object], _dig(room_info, "liveUrl") or {})
            live_title = _dig_str(room_info, "title")
            flv_url = _dig_str(play_url_list, "httpPullUrl")
            m3u8_url = _dig_str(play_url_list, "hlsPullUrl")
            if not flv_url and not m3u8_url:
                _warn_api_abnormal(json_str, "Look 房间接口 room/get/v3", "data.roomInfo.liveUrl", json_data)
                result["is_live"] = False
                return result
            result |= {
                "title": live_title,
                "flv_url": flv_url,
                "m3u8_url": m3u8_url,
            }
            result["record_url"] = m3u8_url or flv_url
    return result


@trace_error_decorator_or_none
async def login_popkontv(
    username: str, password: str, proxy_addr: OptionalStr = None, code: OptionalStr = "P-00001"
) -> tuple[str, str]:
    """本函数返回二元组 (token, partnerCode)，必须挂 _or_none 而非 trace_error_decorator。"""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Authorization": "Basic " + _popkontv_credential(),
        "Content-Type": "application/json",
        "Origin": "https://www.popkontv.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    data = {
        "partnerCode": code,
        "signId": username,
        "signPwd": password,
    }

    url = "https://www.popkontv.com/api/proxy/member/v1/login"

    try:
        proxy_addr = network_urls.normalize_proxy_url(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr, timeout=20, verify=policy.verify_tls) as client:
            response = await client.post(url, json=data, headers=headers)
            response.raise_for_status()

            json_data = response.json()
            login_status_code = json_data.get("statusCd")

            if login_status_code == "E4010":
                raise Exception("popkontv login failed, please reconfigure the correct login account or password!")
            if login_status_code == "S2000":
                token = json_data["data"].get("token")
                partner_code = json_data["data"].get("partnerCode")
                return token, partner_code
            raise Exception(f"popkontv login failed, {json_data.get('statusMsg', 'unknown error')}")
    except httpx.HTTPStatusError as e:
        logger.error(
            i18n.tr("HTTP status error occurred during login: {status_code}", status_code=e.response.status_code)
        )
        raise
    except Exception as e:
        logger.error(
            i18n.tr(
                "An exception occurred during popkontv login: {type_name}: {e}",
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
        raise


@trace_error_decorator_or_none
async def get_popkontv_stream_data(
    url: str,
    proxy_addr: OptionalStr = None,
    cookies: OptionalStr = None,
    username: OptionalStr = None,
    code: OptionalStr = "P-00001",
) -> tuple[str, list[object] | None]:
    """本函数返回二元组，失败时必须回 None 而不是 {"is_live": False}——后者会让调用点解包。"""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Content-Type": "application/json",
        "Origin": "https://www.popkontv.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies
    if "mcid" in url:
        anchor_id_match = re.search("mcid=(.*?)(?=&|#|$)", url)
    else:
        anchor_id_match = re.search("castId=(.*?)(?=&|$)", url)
    if not anchor_id_match:
        raise ValueError("Failed to find anchor id in url")
    anchor_id = anchor_id_match.group(1)

    data = {
        "partnerCode": code,
        "searchKeyword": anchor_id,
        "signId": username,
    }

    api = "https://www.popkontv.com/api/proxy/broadcast/v1/search/all"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers, json_data=data)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    search_data = _dig(json_data, "data")
    if search_data is None:
        _warn_api_abnormal(json_str, "PopkonTV search/all", "data.broadCastList", json_data)
        raise RuntimeError("Failed to retrieve popkontv broadcast list")

    partner_code: str | None = ""
    anchor_name = "Unknown"
    for item in _dig_list(search_data, "broadCastList"):
        if _dig(item, "mcSignId") == anchor_id:
            mc_name = _dig(item, "nickName")
            anchor_name = f"{mc_name}-{anchor_id}"
            partner_code = cast(str | None, _dig(item, "mcPartnerCode")) if item else None
            break

    if not partner_code:
        if "mcPartnerCode" in url:
            regex_result = re.search("mcPartnerCode=(P-\\d+)", url)
        else:
            regex_result = re.search("partnerCode=(P-\\d+)", url)
        partner_code = regex_result.group(1) if regex_result else code
        notices_url = f"https://www.popkontv.com/channel/notices?mcid={anchor_id}&mcPartnerCode={partner_code}"
        notices_response = await http.request(notices_url, proxy_addr=proxy_addr, headers=headers)
        notices_response = _get_str_response(notices_response)
        mc_name_match = re.search(r'"mcNickName":"([^"]+)"', notices_response)
        mc_name = mc_name_match.group(1) if mc_name_match else "Unknown"
        anchor_name = f"{anchor_id}-{mc_name}"

    live_url = f"https://www.popkontv.com/live/view?castId={anchor_id}&partnerCode={partner_code}"
    html_str2 = await http.request(live_url, proxy_addr=proxy_addr, headers=headers)
    html_str2 = _get_str_response(html_str2)
    json_str2_match = re.search('<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_str2)
    if not json_str2_match:
        return anchor_name, None
    json_str2 = json_str2_match.group(1)
    json_data2 = _loads_dict(json_str2)
    page_props = _dig(json_data2, "props", "pageProps")
    if page_props is None:
        _warn_api_abnormal(json_str2, "PopkonTV live/view(__NEXT_DATA__)", "props.pageProps", json_data2)
        raise RuntimeError("Failed to retrieve popkontv live view page data")
    if _dig(page_props, "mcData") is None:
        return anchor_name, None
    room_data = _dig(page_props, "mcData", "data")
    room_keys = ("mc_isPrivate", "mc_castStartDate", "mc_signId", "castType")
    if not isinstance(room_data, dict) or any(_dig(room_data, key) is None for key in room_keys):
        _warn_api_abnormal(json_str2, "PopkonTV live/view(__NEXT_DATA__)", "props.pageProps.mcData.data", json_data2)
        raise RuntimeError("Failed to retrieve popkontv room data")
    is_private = _dig(room_data, "mc_isPrivate")
    cast_start_date_code = _dig(room_data, "mc_castStartDate")
    mc_sign_id = _dig(room_data, "mc_signId")
    cast_type = _dig(room_data, "castType")
    return anchor_name, [cast_start_date_code, partner_code, mc_sign_id, cast_type, is_private]


@trace_error_decorator
async def get_popkontv_stream_url(
    url: str,
    proxy_addr: OptionalStr = None,
    access_token: OptionalStr = None,
    username: OptionalStr = None,
    password: OptionalStr = None,
    partner_code: OptionalStr = "P-00001",
) -> dict[str, object]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "ClientKey": "Client " + _popkontv_credential(),
        "Content-Type": "application/json",
        "Origin": "https://www.popkontv.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    if access_token:
        _bare_access_token = access_token.strip().removeprefix("Bearer ").strip()
        headers["Authorization"] = f"Bearer {_bare_access_token}"

    popkon_data = await get_popkontv_stream_data(url, proxy_addr=proxy_addr, code=partner_code, username=username)
    if not popkon_data:
        return {"anchor_name": "", "is_live": False}
    anchor_name, room_info = popkon_data
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    new_token = None
    if room_info:
        cast_start_date_code, cast_partner_code, mc_sign_id, cast_type, is_private = room_info
        result["is_live"] = True
        room_password = get_params(url, "pwd")
        if parse_config_bool(cast("str | bool | int | None", is_private), False) and not room_password:
            raise RuntimeError(
                f"Failed to retrieve live room data because {anchor_name}'s room is a private room. "
                f"Please configure the room password and try again."
            )

        current_partner_code = partner_code  # 跟踪当前有效的 partner_code，登录刷新后更新

        async def fetch_data(header: dict[str, str] | None = None, code: OptionalStr = None) -> str:
            data = {
                "androidStore": 0,
                "castCode": f"{mc_sign_id}-{cast_start_date_code}",
                "castPartnerCode": cast_partner_code,
                "castSignId": mc_sign_id,
                "castType": cast_type,
                "commandType": 0,
                "exePath": 5,
                "isSecret": is_private,
                "partnerCode": code,
                "password": room_password,
                "signId": username,
                "version": "4.6.2",
            }
            play_api = "https://www.popkontv.com/api/proxy/broadcast/v1/castwatchonoffguest"
            resp = await http.request(play_api, proxy_addr=proxy_addr, json_data=data, headers=header)
            return _get_str_response(resp)

        json_str = await fetch_data(headers, current_partner_code)
        json_str = _get_str_response(json_str)

        if "HTTP Error 400" in json_str or 'statusCd":"E5000' in json_str:
            logger.error(
                "Failed to retrieve popkontv live stream [token does not exist or has expired]: Please log in to watch."
            )
            logger.warning(
                "Attempting to log in to the popkontv live streaming platform, please ensure your account "
                "and password are correctly filled in the configuration file."
            )
            if not username or not password or len(username) < 4 or len(password) < 10:
                raise RuntimeError(
                    "popkontv login failed! Please enter the correct account and password for the "
                    "popkontv platform in the config.ini file."
                )
            logger.info("Logging into popkontv platform...")
            login_result = await login_popkontv(
                username=username, password=password, proxy_addr=proxy_addr, code=current_partner_code
            )
            if not login_result:
                raise RuntimeError("popkontv login failed, please check if the account and password are correct")
            new_access_token, new_partner_code = login_result
            if new_access_token and len(new_access_token) == 640:
                logger.info("Logged into popkontv platform successfully! Starting to fetch live streaming data...")
                headers["Authorization"] = f"Bearer {new_access_token}"
                new_token = new_access_token
                current_partner_code = new_partner_code
                json_str = await fetch_data(headers, current_partner_code)
                json_str = _get_str_response(json_str)
            else:
                raise RuntimeError("popkontv login failed, please check if the account and password are correct")
        json_data = _loads_dict(json_str)
        status_cd = _dig_str(json_data, "statusCd")
        status_msg = _dig_str(json_data, "statusMsg")
        if not status_cd:
            _warn_api_abnormal(json_str, "PopkonTV castwatchonoffguest", "statusCd", json_data)
            raise RuntimeError("Failed to retrieve live stream source, no statusCd in response")
        if status_cd == "L000A":
            logger.error("Failed to retrieve live stream source, " + str(status_msg))
            raise RuntimeError(
                "You are an unverified member. After logging into the popkontv official website, "
                "please verify your mobile phone at the bottom of the 'My Page' > 'Edit My "
                "Information' to use the service."
            )
        if status_cd == "L0001":
            json_str = await fetch_data(headers, current_partner_code)
            json_str = _get_str_response(json_str)
            json_data = _loads_dict(json_str)
            m3u8_url = _dig_str(json_data, "data", "castHlsUrl")
            if not m3u8_url:
                _warn_api_abnormal(json_str, "PopkonTV castwatchonoffguest", "data.castHlsUrl", json_data)
                raise RuntimeError("Failed to retrieve live stream source, no castHlsUrl in response")
            result |= {"m3u8_url": m3u8_url, "record_url": m3u8_url}
        elif status_cd == "L0000":
            m3u8_url = _dig_str(json_data, "data", "castHlsUrl")
            if not m3u8_url:
                _warn_api_abnormal(json_str, "PopkonTV castwatchonoffguest", "data.castHlsUrl", json_data)
                raise RuntimeError("Failed to retrieve live stream source, no castHlsUrl in response")
            result |= {"m3u8_url": m3u8_url, "record_url": m3u8_url}
        else:
            raise RuntimeError("Failed to retrieve live stream source,", status_msg)
    result["new_token"] = new_token
    return result


@trace_error_decorator_or_none
async def login_twitcasting(
    account_type: str, username: str, password: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> OptionalStr:
    """本函数返回 OptionalStr（cookie 串或 None），必须挂 _or_none：trace_error_decorator。"""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://twitcasting.tv/indexcaslogin.php?redir=%2Findexloginwindow.php%3Fnext%3D%252F&keep=1",
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }

    if cookies:
        headers["Cookie"] = cookies

    if account_type == "twitter":
        login_url = "https://twitcasting.tv/indexpasswordlogin.php"
        login_api = "https://twitcasting.tv/indexpasswordlogin.php?redir=/indexloginwindow.php?next=%2F&keep=1"
    else:
        login_url = "https://twitcasting.tv/indexcaslogin.php?redir=%2F&keep=1"
        login_api = "https://twitcasting.tv/indexcaslogin.php?redir=/indexloginwindow.php?next=%2F&keep=1"

    html_str = await http.request(login_url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    cs_session_id_match = re.search('<input type="hidden" name="cs_session_id" value="(.*?)">', html_str)
    if not cs_session_id_match:
        raise ValueError("Failed to find cs_session_id")
    cs_session_id = cs_session_id_match.group(1)

    data = {
        "username": username,
        "password": password,
        "action": "login",
        "cs_session_id": cs_session_id,
    }
    try:
        cookie_result = await http.request(
            login_api, proxy_addr=proxy_addr, headers=headers, data=data, return_cookies=True, timeout=20
        )
        if isinstance(cookie_result, dict):
            cookie_dict = cookie_result
        elif isinstance(cookie_result, tuple) and len(cookie_result) == 2 and isinstance(cookie_result[1], dict):
            cookie_dict = cookie_result[1]
        else:
            cookie_dict = {}

        if "tc_ss" in cookie_dict:
            return network_urls.format_cookies(cookie_dict)
    except Exception as e:
        logger.error("TwitCasting login error, " + str(e))
    return None


@trace_error_decorator
async def get_twitcasting_stream_url(
    url: str,
    proxy_addr: OptionalStr = None,
    cookies: OptionalStr = None,
    account_type: OptionalStr = None,
    username: OptionalStr = None,
    password: OptionalStr = None,
) -> dict[str, object]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Referer": "https://twitcasting.tv/?ch0",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    parts = url.split("?")[0].split("/")
    if len(parts) < 4 or not parts[3].strip():
        raise RuntimeError(
            i18n.tr("无法从链接中解析 TwitCasting 主播 ID: {masked_url}", masked_url=utils.mask_credentials(url))
        )
    anchor_id = parts[3]
    if cookies:
        headers["Cookie"] = cookies

    async def get_data(header: dict[str, str]) -> tuple[str, str, str]:
        html_str = await http.request(url, proxy_addr=proxy_addr, headers=header)
        html_str = _get_str_response(html_str)
        anchor = re.search("<title>(.*?) \\(@(.*?)\\)  的直播 - Twit", html_str)
        title = re.search('<meta name="twitter:title" content="(.*?)">\n\\s+<meta', html_str)
        status = re.search('data-is-onlive="(.*?)"\n\\s+data-view-mode', html_str)
        movie_id = re.search('data-movie-id="(.*?)" data-audience-id', html_str)
        if not anchor or not title or not status or not movie_id:
            raise ValueError("Failed to parse page data")
        return f"{anchor.group(1).strip()}-{anchor.group(2)}-{movie_id.group(1)}", status.group(1), title.group(1)

    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    new_cookie = None
    anchor_name = ""
    live_status = ""
    live_title = ""
    try:
        to_login = get_params(url, "login")
        if to_login == "true":
            logger.info("Attempting to log in to TwitCasting...")
            new_cookie = await login_twitcasting(
                account_type=cast(str, account_type),
                username=cast(str, username),
                password=cast(str, password),
                proxy_addr=proxy_addr,
                cookies=cookies,
            )
            if not isinstance(new_cookie, str) or not new_cookie:
                raise RuntimeError(
                    "TwitCasting login failed, please check if the account password in the "
                    "configuration file is correct"
                )
            logger.info("TwitCasting login successful! Starting to fetch data...")
            headers["Cookie"] = new_cookie
        anchor_name, live_status, live_title = await get_data(headers)
    except AttributeError, ValueError:
        logger.error("Failed to retrieve TwitCasting data, attempting to log in...")
        new_cookie = await login_twitcasting(
            account_type=cast(str, account_type),
            username=cast(str, username),
            password=cast(str, password),
            proxy_addr=proxy_addr,
            cookies=cookies,
        )
        if not isinstance(new_cookie, str) or not new_cookie:
            raise RuntimeError(
                "TwitCasting login failed, please check if the account and password in the "
                "configuration file are correct"
            )
        logger.info("TwitCasting login successful! Starting to fetch data...")
        headers["Cookie"] = new_cookie
        anchor_name, live_status, live_title = await get_data(headers)

    result["anchor_name"] = anchor_name
    if live_status == "true":
        url_streamserver = f"https://twitcasting.tv/streamserver.php?target={anchor_id}&mode=client&player=pc_web"
        stream_data = await http.request(url_streamserver, proxy_addr=proxy_addr, headers=headers)
        stream_data = _get_str_response(stream_data)
        json_data = _loads_dict(stream_data)
        stream_dict = _dig(json_data, "tc-hls", "streams")
        if not json_data:
            _warn_api_abnormal(stream_data, "TwitCasting streamserver", "tc-hls.streams", json_data)
        if not stream_dict:
            raise RuntimeError("No m3u8_url,please check the url")

        streams = cast(dict[str, object], stream_dict)
        quality_order = {"high": 0, "medium": 1, "low": 2}
        sorted_streams = sorted(streams.items(), key=lambda item: quality_order.get(item[0], 99))
        play_url_list = [url for _, url in sorted_streams]
        result |= {"title": live_title, "is_live": True, "play_url_list": play_url_list}
    result["new_cookies"] = new_cookie
    return result


@trace_error_decorator
async def get_baidu_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Connection": "keep-alive",
        "Referer": "https://live.baidu.com/",
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }
    if cookies:
        headers["Cookie"] = cookies

    uid = random.choice(
        [
            "h5-683e85bdf741bf2492586f7ca39bf465",
            "h5-c7c6dc14064a136be4215b452fab9eea",
            "h5-4581281f80bb8968bd9a9dfba6050d3a",
        ]
    )
    room_id_match = re.search("room_id=(.*?)(?=&|#|$)", url)
    if not room_id_match:
        raise ValueError("Failed to find room_id in url")
    room_id = room_id_match.group(1)
    params = {
        "cmd": "371",
        "action": "star",
        "service": "bdbox",
        "osname": "baiduboxapp",
        "data": '{"data":{"room_id":"' + room_id + '","device_id":"h5-683e85bdf741bf2492586f7ca39bf465",'
        '"source_type":0,"osname":"baiduboxapp"},"replay_slice":0,'
        '"nid":"","schemeParams":{"src_pre":"pc","src_suf":"other",'
        '"bd_vid":"","share_uid":"","share_cuk":"","share_ecid":"",'
        '"zb_tag":"","shareTaskInfo":"{\\"room_id\\":\\"9175031377\\"}",'
        '"share_from":"","ext_params":"","nid":""}}',
        "ua": "360_740_ANDROID_0",
        "bd_vid": "",
        "uid": uid,
        "_": str(int(time.time() * 1000)),
    }
    app_api = f"https://mbd.baidu.com/searchbox?{urllib.parse.urlencode(params)}"
    json_str = await http.request(url=app_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    data_dict = cast(dict[str, object], json_data.get("data") or {})
    if not data_dict:
        _warn_api_abnormal(json_str, "百度 searchbox cmd=371", "data", json_data)
        return {"anchor_name": "", "is_live": False}
    data: object = None
    room_key = _dig(data_dict, room_id)
    if isinstance(room_key, dict):
        data = room_key
    else:
        for _v in data_dict.values():
            if isinstance(_v, dict):
                data = _v
                break
    anchor_name = _dig_str(data, "host", "name")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if _dig(data, "status") == "0":
        result["is_live"] = True
        live_title = _dig_str(data, "video", "title")
        play_url_list = _dig_list(data, "video", "url_clarity_list")
        url_list = []
        prefix = "https://hls.liveshow.bdstatic.com/live/"
        if play_url_list:
            for i in play_url_list:
                flv = _dig_str(i, "urls", "flv")
                flv_id = flv.rsplit(".", maxsplit=1)[0].rsplit("/", maxsplit=1)
                url_list.append(prefix + (flv_id[1] if len(flv_id) > 1 else "") + ".m3u8")
        else:
            play_url_list = _dig_list(data, "video", "url_list")
            for i in play_url_list:
                urls = _dig_list(i, "urls")
                hls = _dig_str(urls[0], "hls") if urls else ""
                hls_id = hls.rsplit("?", maxsplit=1)[0].rsplit("/", maxsplit=1)
                url_list.append(prefix + (hls_id[1] if len(hls_id) > 1 else ""))

        if url_list:
            result |= {"is_live": True, "title": live_title, "play_url_list": url_list}
        else:
            _warn_api_abnormal(json_str, "百度 searchbox cmd=371", "video.url_clarity_list", json_data)
            result["is_live"] = False
    return result


@trace_error_decorator
async def get_weibo_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Referer": "https://weibo.com/u/5885340893",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = ""
    if "show/" in url:
        room_id = url.split("?")[0].split("show/")[1]
    else:
        parts = url.split("?")[0].rsplit("/u/", maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            raise RuntimeError(
                i18n.tr("无法从链接中解析微博用户 ID: {masked_url}", masked_url=utils.mask_credentials(url))
            )
        uid = parts[1]
        web_api = f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0"
        json_str = await http.request(web_api, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        if not json_data:
            _warn_api_abnormal(json_str, "微博 mymblog", "data.list", json_data)
        for i in _dig_list(json_data, "data", "list"):
            if _dig(i, "page_info", "object_type") == "live":
                room_id = _dig_str(i, "page_info", "object_id")
                break

    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    if room_id:
        app_api = f"https://weibo.com/l/pc/anchor/live?live_id={room_id}"
        json_str = await http.request(url=app_api, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        if _dig(json_data, "data", "user_info") is None or _dig(json_data, "data", "item") is None:
            _warn_api_abnormal(json_str, "微博 pc/anchor/live", "data.user_info / data.item", json_data)
            return result
        anchor_name = _dig_str(json_data, "data", "user_info", "name")
        result["anchor_name"] = anchor_name
        live_status = _dig(json_data, "data", "item", "status")
        if live_status == 1:
            result["is_live"] = True
            live_title = _dig_str(json_data, "data", "item", "desc")
            play_url_list = _dig(json_data, "data", "item", "stream_info", "pull")
            m3u8_url = _dig_str(play_url_list, "live_origin_hls_url")
            flv_url = _dig_str(play_url_list, "live_origin_flv_url")
            if not m3u8_url and not flv_url:
                _warn_api_abnormal(json_str, "微博 pc/anchor/live", "item.stream_info.pull", json_data)
                result["is_live"] = False
                return result
            result["title"] = live_title
            result["play_url_list"] = [
                {"m3u8_url": m3u8_url, "flv_url": flv_url},
                {
                    "m3u8_url": m3u8_url.split("_")[0] + ".m3u8" if m3u8_url else "",
                    "flv_url": flv_url.split("_")[0] + ".flv" if flv_url else "",
                },
            ]
            if not m3u8_url:
                result["record_url"] = flv_url
    return result


@trace_error_decorator
async def get_kugou_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Referer": "https://fanxing2.kugou.com/",
    }
    if cookies:
        headers["Cookie"] = cookies

    if "roomId" in url:
        room_id_match = re.search("roomId=(\\d+)", url)
        if not room_id_match:
            raise ValueError("Failed to find roomId in url")
        room_id = room_id_match.group(1)
    else:
        room_id = _safe_extract_id(url)

    app_api = f"https://service2.fanxing.kugou.com/roomcen/room/web/cdn/getEnterRoomInfo?roomId={room_id}"
    json_str = await http.request(url=app_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "data", "normalRoomInfo") is None:
        _warn_api_abnormal(json_str, "酷狗 getEnterRoomInfo", "data.normalRoomInfo", json_data)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(json_data, "data", "normalRoomInfo", "nickName")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if not anchor_name:
        raise RuntimeError(
            "Music channel live rooms are not supported for recording, please switch to a different live room."
        )
    live_status = _dig(json_data, "data", "liveType")
    if live_status is None:
        _warn_api_abnormal(json_str, "酷狗 getEnterRoomInfo", "data.liveType", json_data)
        return result
    if live_status != -1:
        params = {
            "std_rid": room_id,
            "std_plat": "7",
            "std_kid": "0",
            "streamType": "1-2-4-5-8",
            "ua": "fx-flash",
            "targetLiveTypes": "1-5-6",
            "version": "1000",
            "supportEncryptMode": "1",
            "appid": "1010",
            "_": str(int(time.time() * 1000)),
        }
        api = f"https://fx1.service.kugou.com/video/pc/live/pull/mutiline/streamaddr?{urllib.parse.urlencode(params)}"
        json_str2 = await http.request(api, proxy_addr=proxy_addr, headers=headers)
        json_str2 = _get_str_response(json_str2)
        json_data2 = _loads_dict(json_str2)
        if _dig(json_data2, "data") is None:
            _warn_api_abnormal(json_str2, "酷狗 mutiline/streamaddr", "data.lines", json_data2)
            return result
        stream_data = _dig_list(json_data2, "data", "lines")
        if stream_data:
            flv_url = _dig_str(stream_data[-1], "streamProfiles", 0, "httpsFlv", 0)
            if not flv_url:
                _warn_api_abnormal(
                    json_str2, "酷狗 mutiline/streamaddr", "lines[-1].streamProfiles[0].httpsFlv[0]", json_data2
                )
                return result
            result |= {"is_live": True, "flv_url": flv_url, "record_url": flv_url}
    return result


async def get_twitchtv_room_info(
    url: str, token: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> tuple[str, bool]:
    client_id = await _ensure_twitch_client_id(proxy_addr)
    if not client_id:
        logger.warning("Twitch Client-Id unavailable (homepage extraction failed), skip GQL request this round")
        raise RuntimeError("Failed to obtain Twitch Client-Id")
    headers = {
        "User-Agent": _TWITCH_WEB_UA,
        "Accept-Language": "zh-CN",
        "Referer": "https://www.twitch.tv/",
        "Client-Id": client_id,
        "Client-Integrity": token,
        "Content-Type": "text/plain;charset=UTF-8",
    }
    if cookies:
        headers["Cookie"] = cookies
    uid = url.split("?")[0].rsplit("/", maxsplit=1)[-1]

    data = [
        {
            "operationName": "ChannelShell",
            "variables": {"login": uid},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "580ab410bcd0c1ad194224957ae2241e5d252b2c5173d8e0cce9d32d5bb14efe",
                }
            },
        },
    ]

    json_str = await http.request("https://gql.twitch.tv/gql", proxy_addr=proxy_addr, headers=headers, json_data=data)
    json_str = _get_str_response(json_str)
    try:
        json_data = cast(object, json.loads(json_str))
    except json.JSONDecodeError as e:
        logger.warning("Twitch GQL response is not valid JSON (WAF page? truncated?): " + type(e).__name__)
        raise RuntimeError("Failed to parse Twitch GQL response") from e
    if not json_data or not isinstance(json_data, list) or not isinstance(json_data[0], dict):
        raise RuntimeError("Failed to retrieve Twitch user data")
    first_item = cast(dict[str, object], json_data[0])
    gql_errors = first_item.get("errors")
    if gql_errors:
        logger.warning("Twitch GQL returned errors for ChannelShell: " + str(gql_errors)[:300])
        raise RuntimeError("Twitch GQL errors: see log")
    user_data = cast(dict[str, object], cast(dict[str, object], first_item.get("data") or {}).get("userOrError") or {})
    login_name = user_data.get("login", "")
    nickname = f"{user_data.get('displayName', '')}-{login_name}"
    status = bool(user_data.get("stream"))
    return nickname, status


@trace_error_decorator
async def get_twitchtv_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    client_id = await _ensure_twitch_client_id(proxy_addr)
    if not client_id:
        logger.warning("Twitch Client-Id unavailable (homepage extraction failed), skip GQL request this round")
        raise RuntimeError("Failed to obtain Twitch Client-Id")
    headers = {
        "User-Agent": _TWITCH_WEB_UA,
        "Accept-Language": "en-US",
        "Referer": "https://www.twitch.tv/",
        "Client-ID": client_id,
        "device-id": generate_random_string(16).lower(),
    }

    if cookies:
        headers["Cookie"] = cookies
    uid = url.split("?")[0].rsplit("/", maxsplit=1)[-1]

    data = {
        "operationName": "PlaybackAccessToken_Template",
        "query": "query PlaybackAccessToken_Template($login: String!, $isLive: Boolean!, $vodID: ID!, "
        "$isVod: Boolean!, $playerType: String!) {  streamPlaybackAccessToken(channelName: $login, "
        'params: {platform: "web", playerBackend: "mediaplayer", playerType: $playerType}) @include(if: '
        "$isLive) {    value    signature   authorization { isForbidden forbiddenReasonCode }   __typename  "
        '}  videoPlaybackAccessToken(id: $vodID, params: {platform: "web", playerBackend: "mediaplayer", '
        "playerType: $playerType}) @include(if: $isVod) {    value    signature   __typename  }}",
        "variables": {"isLive": True, "login": uid, "isVod": False, "vodID": "", "playerType": "site"},
    }

    json_str = await http.request("https://gql.twitch.tv/gql", proxy_addr=proxy_addr, headers=headers, json_data=data)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if json_data.get("errors"):
        logger.warning("Twitch GQL returned errors for PlaybackAccessToken: " + str(json_data["errors"])[:300])
        raise RuntimeError("Twitch GQL errors: see log")
    _pb_data = cast(dict[str, object], json_data.get("data") or {})
    _access = cast(dict[str, object], _pb_data.get("streamPlaybackAccessToken") or {})
    token = _access.get("value")
    sign = _access.get("signature")
    if not isinstance(token, str) or not token or not isinstance(sign, str) or not sign:
        raise RuntimeError("Twitch PlaybackAccessToken missing value/signature (risk control or expired token)")

    anchor_name, live_status = await get_twitchtv_room_info(
        url=url, token=token, proxy_addr=proxy_addr, cookies=cookies
    )
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": live_status}
    if live_status:
        play_session_id = _generate_twitch_play_session_id()
        params = {
            "acmb": "e30=",
            "allow_source": "true",
            "browser_family": "firefox",
            "browser_version": _TWITCH_BROWSER_VERSION,
            "cdm": "wv",
            "fast_bread": "true",
            "os_name": "Windows",
            "os_version": "NT 10.0",
            "p": "3553732",
            "platform": "web",
            "play_session_id": play_session_id,
            "player_backend": "mediaplayer",
            "player_version": "1.28.0-rc.1",
            "playlist_include_framerate": "true",
            "reassignments_supported": "true",
            "sig": sign,
            "token": token,
            "transcode_mode": "cbr_v1",
        }
        access_key = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        m3u8_url = f"https://usher.ttvnw.net/api/channel/hls/{uid}.m3u8?{access_key}"
        play_url_list = await get_play_url_list(m3u8=m3u8_url, proxy=proxy_addr, header=headers)
        result |= {"m3u8_url": m3u8_url, "play_url_list": play_url_list}
    return result


@trace_error_decorator
async def get_liveme_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "origin": "https://www.liveme.com",
        "referer": "https://www.liveme.com",
        "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }
    if cookies:
        headers["Cookie"] = cookies

    if "index.html" not in url:
        html_str = await http.request(url, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        match_url = re.search('<meta property="og:url" content="(.*?)">', html_str)
        if match_url:
            url = match_url.group(1)

    room_id = url.split("/index.html")[0].rsplit("/", maxsplit=1)[-1]
    sign_data = cast(
        dict[str, object],
        await javascript.run_js_async(
            f"{JS_SCRIPT_PATH}/liveme.js", "sign", room_id, f"{JS_SCRIPT_PATH}/crypto-js.min.js"
        ),
    )
    lm_s_sign = str(sign_data.pop("lm_s_sign"))
    tongdun_black_box = sign_data.pop("tongdun_black_box")
    platform = sign_data.pop("os")
    headers["lm-s-sign"] = lm_s_sign

    params = {
        "alias": "liveme",
        "tongdun_black_box": tongdun_black_box,
        "os": platform,
    }

    api = f"https://live.liveme.com/live/queryinfosimple?{urllib.parse.urlencode(params)}"
    json_str = await http.request(api, data=sign_data, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    stream_data = cast(dict[str, object], _dig(json_data, "data", "video_info") or {})
    if _dig(stream_data, "status") is None:
        _warn_api_abnormal(json_str, "LiveMe queryinfosimple", "data.video_info.status", json_data)
        return {"anchor_name": _dig_str(stream_data, "uname"), "is_live": False}
    anchor_name = _dig_str(stream_data, "uname")
    live_status = _dig(stream_data, "status")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == "0":
        m3u8_url = _dig_str(stream_data, "hlsvideosource")
        flv_url = _dig_str(stream_data, "videosource")
        if not m3u8_url and not flv_url:
            _warn_api_abnormal(json_str, "LiveMe queryinfosimple", "data.video_info.videosource", json_data)
            return result
        result |= {"is_live": True, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": m3u8_url or flv_url}
    return result


async def get_huajiao_sn(
    url: str, cookies: OptionalStr = None, proxy_addr: OptionalStr = None
) -> tuple[str, str, str, str] | None:
    headers = {
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://www.huajiao.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    if cookies:
        headers["Cookie"] = cookies

    live_id = _safe_extract_id(url)
    api = f"https://www.huajiao.com/l/{live_id}"
    html_str = _get_str_response(await http.request(url=api, proxy_addr=proxy_addr, headers=headers))
    if not html_str:
        logger.warning(
            i18n.tr(
                "[花椒直播]房间页返回空响应（网络失败或风控），未改动配置文件: {masked_url}",
                masked_url=utils.mask_credentials(api),
            )
        )
        raise RuntimeError(
            "Failed to retrieve live room data, the Huajiao live room address is not fixed, please use "
            "the anchor's homepage address for recording."
        )
    try:
        json_str_match = re.search("var feed = (.*?});", html_str)
        if not json_str_match:
            raise ValueError("Failed to find feed data")
        json_str = json_str_match.group(1)
        json_data = _loads_dict(json_str)
        sn = _dig(json_data, "feed", "sn")
        uid = _dig(json_data, "author", "uid")
        nickname = _dig(json_data, "author", "nickname")
        if sn is None or uid is None or nickname is None:
            _warn_api_abnormal(json_str, "花椒 房间页 var feed", "feed.sn + author.uid", json_data)
            raise KeyError("Missing feed/author data in huajiao page")
        live_id = _safe_extract_id(url)
        return cast(str, nickname), cast(str, sn), cast(str, uid), live_id
    except (ValueError, KeyError) as e:
        logger.warning(
            i18n.tr(
                "[花椒直播]房间页确认无有效直播数据，已注释该房间: {type_name}: {e}",
                type_name=type(e).__name__,
                e=e,
            )
        )
        config_files.replace_url(f"{script_path}/config/URL_config.ini", old=url, new="#" + url)
        raise RuntimeError(
            "Failed to retrieve live room data, the Huajiao live room address is not fixed, please use "
            "the anchor's homepage address for recording."
        ) from e
    except Exception as e:
        logger.warning(
            i18n.tr(
                "[花椒直播]解析异常（非内容失效，未改动配置文件）: {type_name}: {e}",
                type_name=type(e).__name__,
                e=e,
            )
        )
        raise RuntimeError(
            "Failed to retrieve live room data, the Huajiao live room address is not fixed, please use "
            "the anchor's homepage address for recording."
        ) from e


async def get_huajiao_user_info(
    url: str, cookies: OptionalStr = None, proxy_addr: OptionalStr = None
) -> OptionalStreamDict:
    headers = {
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://www.huajiao.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    if cookies:
        headers["Cookie"] = cookies

    if "user" in url:
        uid = url.split("?")[0].split("user/")[1]
        params = {
            "uid": uid,
            "fmt": "json",
            "_": str(int(time.time() * 1000)),
        }

        api = f"https://webh.huajiao.com/User/getUserFeeds?{urllib.parse.urlencode(params)}"
        json_str = await http.request(url=api, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        feeds = _dig_list(json_data, "data", "feeds")
        if _dig(json_data, "data") is None:
            _warn_api_abnormal(json_str, "花椒 getUserFeeds", "data.feeds", json_data)

        html_str = await http.request(url=f"https://www.huajiao.com/user/{uid}", proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        anchor_name_match = re.search("<title>(.*?)的主页.*</title>", html_str)
        anchor_name = anchor_name_match.group(1) if anchor_name_match else ""
        first_feed = _dig(feeds, 0) if feeds else None
        if feeds and _dig(first_feed, "feed", "sn") is not None:
            feed = cast(dict[str, object], _dig(first_feed, "feed") or {})
            return {
                "anchor_name": anchor_name,
                "title": cast(str, _dig(feed, "title")),
                "is_live": True,
                "sn": cast(str, _dig(feed, "sn")),
                "liveid": cast(str, _dig(feed, "relateid")),
                "uid": uid,
            }
        return {"anchor_name": anchor_name, "is_live": False}
    return None


async def get_huajiao_stream_url_app(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> OptionalStreamDict:
    headers = {
        "User-Agent": "living/9.4.0 (com.huajiao.seeding; build:2410231746; iOS 17.0.0) Alamofire/9.4.0",
        "accept-language": "zh-Hans-US;q=1.0",
        "sdk_version": "1",
    }
    if cookies:
        headers["Cookie"] = cookies
    room_id = _safe_extract_id(url)
    api = f"https://live.huajiao.com/feed/getFeedInfo?relateid={room_id}"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "errmsg") is None or _dig(json_data, "data") is None:
        _warn_api_abnormal(json_str, "花椒 getFeedInfo", "errmsg + data.creatime", json_data)
        return None

    if _dig(json_data, "errmsg") or not _dig(json_data, "data", "creatime"):
        logger.error(
            "Failed to retrieve live room data, the Huajiao live room address is not fixed, please manually change "
            "the address for recording."
        )
        return None
    data = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(data, "feed", "sn") is None or _dig(data, "author", "nickname") is None:
        _warn_api_abnormal(json_str, "花椒 getFeedInfo", "data.feed.sn + data.author.nickname", json_data)
        return None
    return {
        "anchor_name": _dig_str(data, "author", "nickname"),
        "title": cast(str, _dig(data, "feed", "title")),
        "is_live": True,
        "sn": cast(str, _dig(data, "feed", "sn")),
        "liveid": cast(str, _dig(data, "feed", "relateid")),
        "uid": cast(str, _dig(data, "author", "uid")),
    }


@trace_error_decorator
async def get_huajiao_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://www.huajiao.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    result: dict[str, object] = {"anchor_name": "", "is_live": False}

    if "user/" in url:
        if not cookies:
            return result
        room_data = await get_huajiao_user_info(url, cookies, proxy_addr)
    else:
        url_result = await http.request(url, proxy_addr=proxy_addr, headers=headers, redirect_url=True)
        if isinstance(url_result, str) and url_result:
            url = url_result
        else:
            logger.warning(
                i18n.tr(
                    "[花椒直播]短链重定向无响应（网络失败或风控），本轮按未开播重试: {masked_url}",
                    masked_url=utils.mask_credentials(url),
                )
            )
            return result

        if url.rstrip("/") == "https://www.huajiao.com":
            logger.error(
                "Failed to retrieve live room data, the Huajiao live room address is not fixed, please manually change "
                "the address for recording."
            )
            return result
        room_data = await get_huajiao_stream_url_app(url, proxy_addr, cookies)

    if room_data:
        result["anchor_name"] = room_data.pop("anchor_name")
        live_status = room_data.pop("is_live")

        if live_status:
            result["title"] = room_data.pop("title")
            params = {"time": int(time.time() * 1000), "version": "1.0.0", **room_data, "encode": "h264"}

            api = f"https://live.huajiao.com/live/substream?{urllib.parse.urlencode(params)}"
            json_str = await http.request(url=api, proxy_addr=proxy_addr, headers=headers)
            json_str = _get_str_response(json_str)
            json_data = _loads_dict(json_str)
            _sub_data = json_data.get("data")
            h264_v = _sub_data.get("h264_url") if isinstance(_sub_data, dict) else None
            if isinstance(h264_v, str) and h264_v:
                result |= {
                    "is_live": True,
                    "flv_url": h264_v,
                    "record_url": h264_v,
                }
            else:
                logger.warning("Huajiao substream response has no h264_url (API changed or risk control)")
    return result


@trace_error_decorator
async def get_liuxing_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Referer": "https://wap.7u66.com/198189?promoters=0",
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = _safe_extract_id(url)
    params = {"promoters": "0", "roomidx": room_id, "currentUrl": f"https://www.7u66.com/{room_id}?promoters=0"}
    api = f"https://wap.7u66.com/api/ui/room/v1.0.0/live.ashx?{urllib.parse.urlencode(params)}"
    json_str = await http.request(url=api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    room_info = cast(dict[str, object], _dig(json_data, "data", "roomInfo") or {})
    if _dig(room_info, "live_stat") is None:
        _warn_api_abnormal(json_str, "流星 live.ashx", "data.roomInfo.live_stat", json_data)
        return {"anchor_name": _dig_str(room_info, "nickname"), "is_live": False}
    anchor_name = _dig_str(room_info, "nickname")
    live_status = _dig(room_info, "live_stat")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        idx = _dig(room_info, "idx")
        live_id = _dig(room_info, "liveId1")
        if idx is None or live_id is None:
            _warn_api_abnormal(json_str, "流星 live.ashx", "data.roomInfo.idx + liveId1", json_data)
            return result
        flv_url = f"https://txpull1.5see.com/live/{idx}/{live_id}.flv"
        result |= {"is_live": True, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_showroom_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    if "/room/profile" in url:
        room_id = url.split("room_id=")[-1]
    else:
        html_str = await http.request(url, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        room_id_match = re.search('href="/room/profile\\?room_id=(.*?)"', html_str)
        if not room_id_match:
            raise ValueError("Failed to find room_id")
        room_id = room_id_match.group(1)
    info_api = f"https://www.showroom-live.com/api/live/live_info?room_id={room_id}"
    json_str = await http.request(info_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "live_status") is None:
        _warn_api_abnormal(json_str, "ShowRoom live_info", "live_status", json_data)
        return {"anchor_name": _dig_str(json_data, "room_name"), "is_live": False}
    anchor_name = _dig_str(json_data, "room_name")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    live_status = _dig(json_data, "live_status")
    if live_status == 2:
        result["is_live"] = True
        web_api = f"https://www.showroom-live.com/api/live/streaming_url?room_id={room_id}&abr_available=1"
        json_str = await http.request(web_api, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        if json_str:
            json_data = _loads_dict(json_str)
            streaming_url_list = _dig_list(json_data, "streaming_url_list")
            if not streaming_url_list:
                _warn_api_abnormal(json_str, "ShowRoom streaming_url", "streaming_url_list", json_data)
                result["is_live"] = False
                return result

            for i in streaming_url_list:
                if _dig(i, "type") == "hls_all":
                    m3u8_url = _dig_str(i, "url")
                    if m3u8_url:
                        result["m3u8_url"] = m3u8_url
                        m3u8_url_list = await get_play_url_list(m3u8_url, proxy=proxy_addr, header=headers)
                        if m3u8_url_list:
                            result["play_url_list"] = [urllib.parse.urljoin(m3u8_url, i) for i in m3u8_url_list]
                        else:
                            result["play_url_list"] = [m3u8_url]
                        _play_url_list = cast(list[str], result["play_url_list"])
                        result["play_url_list"] = [i.replace("https://", "http://") for i in _play_url_list]
                        break
            else:
                _warn_api_abnormal(json_str, "ShowRoom streaming_url", "streaming_url_list", json_data)
                result["is_live"] = False
    return result


@trace_error_decorator_or_none
async def get_acfun_sign_params(
    proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> tuple[object, str, object]:
    """返回三元组，失败必须回 None（同 get_popkontv_stream_data）：旧装饰器回 dict，调用方。"""
    did = f"web_{utils.generate_random_string(16)}"
    cookie_header = f"_did={did};"
    if cookies:
        cookie_header = f"_did={did}; {cookies}"
    headers = {
        "referer": "https://live.acfun.cn/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Cookie": cookie_header,
    }
    data = {
        "sid": "acfun.api.visitor",
    }
    api = "https://id.app.acfun.cn/rest/app/visitor/login"
    json_str = await http.request(api, data=data, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    user_id = _dig(json_data, "userId")
    visitor_st = _dig(json_data, "acfun.api.visitor_st")
    if user_id is None or visitor_st is None:
        _warn_api_abnormal(json_str, "AcFun visitor/login", "userId + acfun.api.visitor_st", json_data)
        raise RuntimeError("Failed to get AcFun visitor sign params")
    return user_id, did, visitor_st


@trace_error_decorator
async def get_acfun_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "referer": "https://live.acfun.cn/live/17912421",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    author_id = _safe_extract_id(url)
    user_info_api = f"https://live.acfun.cn/rest/pc-direct/user/userInfo?userId={author_id}"
    json_str = await http.request(user_info_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    profile = cast(dict[str, object], _dig(json_data, "profile") or {})
    if not profile:
        _warn_api_abnormal(json_str, "AcFun userInfo", "profile", json_data)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(profile, "name")
    status = "liveId" in profile
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if status:
        result["is_live"] = True
        _sign_params = await get_acfun_sign_params(proxy_addr=proxy_addr, cookies=cookies)
        if not _sign_params:
            logger.warning("Acfun 游客签名参数获取失败，跳过本轮取流（visitor/login 未拿到三件套）")
            result["is_live"] = False
            return result
        user_id, did, visitor_st = _sign_params
        params = {
            "subBiz": "mainApp",
            "kpn": "ACFUN_APP",
            "kpf": "PC_WEB",
            "userId": user_id,
            "did": did,
            "acfun.api.visitor_st": visitor_st,
        }

        data = {
            "authorId": author_id,
            "pullStreamType": "FLV",
        }
        play_api = f"https://api.kuaishouzt.com/rest/zt/live/web/startPlay?{urllib.parse.urlencode(params)}"
        json_str = await http.request(play_api, data=data, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        if _dig(json_data, "data") is None:
            _warn_api_abnormal(json_str, "AcFun startPlay", "data", json_data)
            result["is_live"] = False
            return result
        live_title = _dig_str(json_data, "data", "caption")
        video_play_res = _dig(json_data, "data", "videoPlayRes")
        if not isinstance(video_play_res, str):
            _warn_api_abnormal(json_str, "AcFun startPlay", "data.videoPlayRes", json_data)
            result["is_live"] = False
            return result
        res_data = _loads_dict(video_play_res)
        play_url_list = cast(
            list[dict[str, object]], _dig_list(res_data, "liveAdaptiveManifest", 0, "adaptationSet", "representation")
        )
        if not play_url_list:
            _warn_api_abnormal(
                video_play_res,
                "AcFun startPlay(videoPlayRes)",
                "liveAdaptiveManifest[0].adaptationSet.representation",
                res_data,
            )
            result["is_live"] = False
            return result
        play_url_list = sorted(play_url_list, key=itemgetter("bitrate"), reverse=True)
        result |= {"play_url_list": play_url_list, "title": live_title}
    return result


@trace_error_decorator
async def get_changliao_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Referer": "https://wap.tlclw.com/phone/15777?promoters=0",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    params = {
        "roomidx": room_id,
        "currentUrl": f"https://wap.tlclw.com/{room_id}",
    }
    play_api = f"https://wap.tlclw.com/api/ui/room/v1.0.0/live.ashx?{urllib.parse.urlencode(params)}"
    json_str = await http.request(play_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    room_info = cast(dict[str, object], _dig(json_data, "data", "roomInfo") or {})
    if not room_info:
        _warn_api_abnormal(json_str, "畅聊 live.ashx", "data.roomInfo", json_data)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(room_info, "nickname")
    live_status = _dig(room_info, "live_stat")

    async def get_live_domain(page_url: str) -> tuple[str, str]:
        html_str = await http.request(page_url, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        config_json_match = re.findall("var config = (.*?)config.webskins", html_str, re.DOTALL)
        if not config_json_match:
            raise ValueError("Failed to find config data")
        config_json_str = config_json_match[0].rsplit(";", maxsplit=1)[0].strip()
        config_json_data = _loads_dict(config_json_str)
        stream_flv_domain = _dig_str(config_json_data, "domainpullstream_flv")
        stream_hls_domain = _dig_str(config_json_data, "domainpullstream_hls")
        if not stream_flv_domain or not stream_hls_domain:
            _warn_api_abnormal(
                config_json_str, "畅聊 房间页 var config", "domainpullstream_flv / _hls", config_json_data
            )
        return stream_flv_domain, stream_hls_domain

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        flv_domain, hls_domain = await get_live_domain(url)
        if not flv_domain or not hls_domain:
            return result
        live_id = _dig(room_info, "liveID")
        if live_id is None:
            _warn_api_abnormal(json_str, "畅聊 live.ashx", "data.roomInfo.liveID", json_data)
            return result
        flv_url = f"{flv_domain}/{str(live_id)}.flv"
        m3u8_url = f"{hls_domain}/{str(live_id)}.m3u8"
        result |= {"is_live": True, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_yingke_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "Referer": "https://www.inke.cn/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    parsed_url = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    uid = query_params.get("uid", [""])[0]
    live_id = query_params.get("id", [""])[0]
    if not uid or not live_id:
        raise ValueError("Failed to extract uid or live_id from inke URL")
    params = {
        "uid": uid,
        "id": live_id,
        "_t": str(int(time.time())),
    }

    api = f"https://webapi.busi.inke.cn/web/live_share_pc?{urllib.parse.urlencode(params)}"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    live_data = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(live_data, "status") is None:
        _warn_api_abnormal(json_str, "映客 live_share_pc", "data.status", json_data)
        return {"anchor_name": _dig_str(live_data, "media_info", "nick"), "is_live": False}
    anchor_name = _dig_str(live_data, "media_info", "nick")
    live_status = _dig(live_data, "status")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        first_addr = _dig(live_data, "live_addr", 0)
        m3u8_url = _dig_str(first_addr, "hls_stream_addr")
        flv_url = _dig_str(first_addr, "stream_addr")
        if not m3u8_url and not flv_url:
            _warn_api_abnormal(json_str, "映客 live_share_pc", "data.live_addr[0].hls_stream_addr", json_data)
            return result
        result |= {"is_live": True, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": m3u8_url}
    return result


@trace_error_decorator
async def get_yinbo_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Referer": "https://live.ybw1666.com/800005143?promoters=0",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    params = {
        "roomidx": room_id,
        "currentUrl": f"https://wap.ybw1666.com/{room_id}",
    }
    play_api = f"https://wap.ybw1666.com/api/ui/room/v1.0.0/live.ashx?{urllib.parse.urlencode(params)}"
    json_str = await http.request(play_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    room_data = cast(dict[str, object], _dig(json_data, "data", "roomInfo") or {})
    if not room_data:
        _warn_api_abnormal(json_str, "音播 live.ashx", "data.roomInfo", json_data)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(room_data, "nickname")
    live_status = _dig(room_data, "live_stat")

    async def get_live_domain(page_url: str) -> tuple[str, str]:
        html_str = await http.request(page_url, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        config_json_match = re.findall("var config = (.*?)config.webskins", html_str, re.DOTALL)
        if not config_json_match:
            raise ValueError("Failed to find config data")
        config_json_str = config_json_match[0].rsplit(";", maxsplit=1)[0].strip()
        config_json_data = _loads_dict(config_json_str)
        stream_flv_domain = _dig_str(config_json_data, "domainpullstream_flv")
        stream_hls_domain = _dig_str(config_json_data, "domainpullstream_hls")
        if not stream_flv_domain or not stream_hls_domain:
            _warn_api_abnormal(
                config_json_str, "音播 房间页 var config", "domainpullstream_flv / _hls", config_json_data
            )
        return stream_flv_domain, stream_hls_domain

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        flv_domain, hls_domain = await get_live_domain(url)
        if not flv_domain or not hls_domain:
            return result
        live_id = _dig(room_data, "liveID")
        if live_id is None:
            _warn_api_abnormal(json_str, "音播 live.ashx", "data.roomInfo.liveID", json_data)
            return result
        flv_url = f"{flv_domain}/{str(live_id)}.flv"
        m3u8_url = f"{hls_domain}/{str(live_id)}.m3u8"
        result |= {"is_live": True, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_zhihu_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    if "people/" in url:
        user_id = url.split("?")[0].split("people/")[1].split("/")[0]
        if not user_id:
            raise ValueError("Failed to find zhihu user id")
        api = f"https://api.zhihu.com/people/{user_id}/profile?profile_new_version="
        json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        if not json_data:
            _warn_api_abnormal(json_str, "知乎 people/profile", "drama.living_theater.theater_url", json_data)
            return {"anchor_name": "", "is_live": False}
        drama = _dig(json_data, "drama") or {}
        living_theater = _dig(drama, "living_theater") or {}
        theater_url = _dig_str(living_theater, "theater_url")
        if not theater_url:
            return {"anchor_name": "", "is_live": False}
        live_page_url = theater_url
    else:
        live_page_url = url

    web_id = live_page_url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    html_str = await http.request(live_page_url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    json_str2_match = re.findall('<script id="js-initialData" type="text/json">(.*?)</script>', html_str)
    if not json_str2_match:
        raise ValueError("Failed to find initialData")
    json_str2 = json_str2_match[0]
    json_data2 = _loads_dict(json_str2)
    live_data = _dig(json_data2, "initialState", "theater", "theaters", web_id)
    if not isinstance(live_data, dict):
        _warn_api_abnormal(json_str2, "知乎 js-initialData", f"initialState.theater.theaters[{web_id}]", json_data2)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(live_data, "actor", "name")
    live_status = _dig(live_data, "drama", "status")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        live_title = _dig_str(live_data, "theme")
        play_url = _dig(live_data, "drama", "playInfo")
        hls_url = _dig_str(play_url, "hlsUrl")
        if not hls_url:
            _warn_api_abnormal(json_str2, "知乎 js-initialData", "drama.playInfo.hlsUrl", json_data2)
            return result
        result |= {
            "is_live": True,
            "title": live_title,
            "m3u8_url": hls_url,
            "flv_url": _dig_str(play_url, "playUrl"),
            "record_url": hls_url,
        }
    return result


@trace_error_decorator
async def get_chzzk_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "origin": "https://chzzk.naver.com",
        "referer": "https://chzzk.naver.com/live/458f6ec20b034f49e0fc6d03921646d2",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    play_api = f"https://api.chzzk.naver.com/service/v3/channels/{room_id}/live-detail"
    json_str = await http.request(play_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    live_data = cast(dict[str, object], _dig(json_data, "content") or {})
    if not live_data:
        _warn_api_abnormal(json_str, "CHZZK live-detail", "content", json_data)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(live_data, "channel", "channelName")
    live_status = _dig(live_data, "status")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == "OPEN":
        raw_playback = _dig_str(live_data, "livePlaybackJson")
        if not raw_playback:
            _warn_api_abnormal(json_str, "CHZZK live-detail", "content.livePlaybackJson", json_data)
            return result
        play_data = _loads_dict(raw_playback)
        m3u8_url = _dig_str(play_data, "media", 0, "path")
        if not m3u8_url:
            _warn_api_abnormal(raw_playback, "CHZZK livePlaybackJson", "media[0].path", play_data)
            return result
        m3u8_url_list = await get_play_url_list(m3u8_url, proxy=proxy_addr, header=headers)
        prefix = m3u8_url.split("?")[0].rsplit("/", maxsplit=1)[0]
        m3u8_url_list = [urllib.parse.urljoin(prefix + "/", i) for i in m3u8_url_list]
        result |= {"is_live": True, "m3u8_url": m3u8_url, "play_url_list": m3u8_url_list}
    return result


def _read_haixiu_token_override(is_haixiu: bool) -> str:
    """注意：@decorator 与 def 之间夹注释时，装饰器仍绑到紧随其后的 def——本仓曾因此把兜底。"""
    env_key = "HAIXIU_ACCESS_TOKEN" if is_haixiu else "HAIHAI_ACCESS_TOKEN"
    cfg_key = "haixiu_access_token" if is_haixiu else "haihai_access_token"
    override = os.environ.get(env_key, "").strip()
    if override:
        return override
    try:
        cfg_value = config_files.read_ini_value(f"{script_path}/config/config.ini", "Cookie", cfg_key)
    except Exception:
        return ""
    return (cfg_value or "").strip()


@trace_error_decorator
async def get_haixiu_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    """兜底装饰器绑在平台入口本身（不能落到上方的同步 helper 上）。"""
    headers = {
        "origin": "https://www.haixiutv.com",
        "referer": "https://www.haixiutv.com/",
        "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }
    if cookies:
        headers["Cookie"] = cookies

    room_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    _is_haixiu = "haixiutv" in url
    access_token = _read_haixiu_token_override(_is_haixiu)
    if not access_token:
        if _is_haixiu:
            access_token = "pLXSC%252FXJ0asc1I21tVL5FYZhNJn2Zg6d7m94umCnpgL%252BuVm31GQvyw%253D%253D"
        else:
            access_token = "s7FUbTJ%252BjILrR7kicJUg8qr025ZVjd07DAnUQd8c7g%252Fo4OH9pdSX6w%253D%253D"

    params = {"accessToken": access_token, "tku": "3000006", "c": "10138100100000", "_st1": int(time.time() * 1000)}
    ajax_data = cast(
        str,
        await javascript.run_js_async(
            f"{JS_SCRIPT_PATH}/haixiu.js", "sign", params, f"{JS_SCRIPT_PATH}/crypto-js.min.js"
        ),
    )

    params["accessToken"] = urllib.parse.unquote(urllib.parse.unquote(access_token))
    params["_ajaxData1"] = ajax_data
    params["_"] = int(time.time() * 1000)

    if "haixiutv" in url:
        api = f"https://service.haixiutv.com/v2/room/{room_id}/media/advanceInfoRoom?{urllib.parse.urlencode(params)}"
    else:
        headers["origin"] = "https://www.lehaitv.com"
        headers["referer"] = "https://www.lehaitv.com"
        api = f"https://service.lehaitv.com/v2/room/{room_id}/media/advanceInfoRoom?{urllib.parse.urlencode(params)}"

    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    stream_data = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(stream_data, "live_status") is None:
        _warn_api_abnormal(json_str, "嗨秀 advanceInfoRoom", "data.live_status", json_data)
        return {"anchor_name": _dig_str(stream_data, "nickname"), "is_live": False}
    anchor_name = _dig_str(stream_data, "nickname")
    live_status = _dig(stream_data, "live_status")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        flv_url = _dig_str(stream_data, "media_url_web")
        if not flv_url:
            _warn_api_abnormal(json_str, "嗨秀 advanceInfoRoom", "data.media_url_web", json_data)
            return result
        result |= {"is_live": True, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_vvxqiu_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "Access-Control-Request-Method": "GET",
        "Origin": "https://h5webcdn-pro.vvxqiu.com",
        "Referer": "https://h5webcdn-pro.vvxqiu.com/",
    }

    if cookies:
        headers["Cookie"] = cookies

    room_id = get_params(url, "roomId")
    api_1 = f"https://h5p.vvxqiu.com/activity-center/fanclub/activity/captain/banner?roomId={room_id}&product=vvstar"
    json_str = await http.request(api_1, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "data") is None:
        _warn_api_abnormal(json_str, "VV星球 captain/banner", "data.anchorName", json_data)
    anchor_name = _dig_str(json_data, "data", "anchorName")
    if not anchor_name:
        params = {
            "sessionId": "",
            "userId": "",
            "product": "vvstar",
            "tickToken": "",
            "roomId": room_id,
        }
        json_str = await http.request(
            f"https://h5p.vvxqiu.com/activity-center/halloween2023/banner?{urllib.parse.urlencode(params)}",
            proxy_addr=proxy_addr,
            headers=headers,
        )
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        anchor_name = _dig_str(json_data, "data", "memberVO", "memberName")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if not room_id:
        return result
    m3u8_url = f"https://liveplay-pro.wasaixiu.com/live/1400442770_{room_id}_{room_id[2:]}_single.m3u8"
    resp = await http.request(m3u8_url, proxy_addr=proxy_addr, headers=headers)
    resp = _get_str_response(resp)
    if resp and "Not Found" not in resp:
        result |= {"is_live": True, "m3u8_url": m3u8_url, "record_url": m3u8_url}
    return result


@trace_error_decorator
async def get_17live_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "origin": "https://17.live",
        "referer": "https://17.live/",
        "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }

    if cookies:
        headers["Cookie"] = cookies

    room_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    api_1 = f"https://wap-api.17app.co/api/v1/user/room/{room_id}"
    json_str = await http.request(api_1, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "displayName") is None:
        _warn_api_abnormal(json_str, "17Live user/room", "displayName", json_data)
    anchor_name = _dig_str(json_data, "displayName")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    request_payload: dict[str, str] = {
        "liveStreamID": room_id,
    }
    api_1 = f"https://wap-api.17app.co/api/v1/lives/{room_id}/viewers/alive"
    json_str = await http.request(api_1, json_data=request_payload, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    live_status = _dig(json_data, "status")
    if live_status and live_status == 2:
        flv_url = _dig_str(json_data, "pullURLsInfo", "rtmpURLs", 0, "urlHighQuality")
        if not flv_url:
            _warn_api_abnormal(json_str, "17Live viewers/alive", "pullURLsInfo.rtmpURLs[0].urlHighQuality", json_data)
            return result
        result |= {"is_live": True, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_langlive_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "origin": "https://www.lang.live",
        "referer": "https://www.lang.live/",
        "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }

    if cookies:
        headers["Cookie"] = cookies

    room_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    api_1 = f"https://api.lang.live/langweb/v1/room/liveinfo?room_id={room_id}"
    json_str = await http.request(api_1, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    live_info = cast(dict[str, object], _dig(json_data, "data", "live_info") or {})
    if _dig(live_info, "live_status") is None:
        _warn_api_abnormal(json_str, "浪Live room/liveinfo", "data.live_info.live_status", json_data)
        return {"anchor_name": _dig_str(live_info, "nickname"), "is_live": False}
    anchor_name = _dig_str(live_info, "nickname")
    live_status = _dig(live_info, "live_status")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        flv_url = _dig_str(live_info, "liveurl")
        m3u8_url = _dig_str(live_info, "liveurl_hls")
        if not flv_url and not m3u8_url:
            _warn_api_abnormal(json_str, "浪Live room/liveinfo", "data.live_info.liveurl", json_data)
            return result
        result |= {"is_live": True, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": m3u8_url}
    return result


@trace_error_decorator
async def get_pplive_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://m.pp.weimipopo.com",
        "Referer": "https://m.pp.weimipopo.com/",
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }

    if cookies:
        headers["Cookie"] = cookies

    room_id = get_params(url, "anchorUid")
    req_body = {
        "inviteUuid": "",
        "anchorUuid": room_id,
    }

    if "catshow" in url:
        api = "https://api.catshow168.com/live/preview"
        headers["Origin"] = "https://h.catshow168.com"
        headers["Referer"] = "https://h.catshow168.com"
    else:
        api = "https://api.pp.weimipopo.com/live/preview"
    json_str = await http.request(api, json_data=req_body, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    live_info = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(live_info, "living") is None:
        _warn_api_abnormal(json_str, "飘飘 live/preview", "data.living", json_data)
        return {"anchor_name": _dig_str(live_info, "name"), "is_live": False}
    anchor_name = _dig_str(live_info, "name")
    live_status = _dig(live_info, "living")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status:
        m3u8_url = _dig_str(live_info, "pullUrl")
        if not m3u8_url:
            _warn_api_abnormal(json_str, "飘飘 live/preview", "data.pullUrl", json_data)
            return result
        result |= {"is_live": True, "m3u8_url": m3u8_url, "record_url": m3u8_url}
    return result


@trace_error_decorator
async def get_6room_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://ios.6.cn/?ver=8.0.3&build=4",
        "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }

    if cookies:
        headers["Cookie"] = cookies

    room_id = _safe_extract_id(url)
    html_str = await http.request(f"https://v.6.cn/{room_id}", proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    room_id_match = re.search("rid: '(.*?)',\n\\s+roomid", html_str)
    if not room_id_match:
        raise ValueError("Failed to find room_id")
    room_id = room_id_match.group(1)
    data = {
        "av": "3.1",
        "encpass": "",
        "logiuid": "",
        "project": "v6iphone",
        "rate": "1",
        "rid": "",
        "ruid": room_id,
    }
    api = "https://v.6.cn/coop/mobile/index.php?padapi=coop-mobile-inroom.php"
    json_str = await http.request(api, data=data, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    liveinfo = cast(dict[str, object], _dig(json_data, "content", "liveinfo") or {})
    if _dig(liveinfo, "flvtitle") is None:
        _warn_api_abnormal(json_str, "六间房 coop-mobile-inroom", "content.liveinfo.flvtitle", json_data)
        return {"anchor_name": _dig_str(json_data, "content", "roominfo", "alias"), "is_live": False}
    flv_title = _dig_str(liveinfo, "flvtitle")
    anchor_name = _dig_str(json_data, "content", "roominfo", "alias")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if flv_title:
        flv_url = f"https://wlive.6rooms.com/httpflv/{flv_title}.flv"
        result |= {"is_live": True, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_shopee_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "referer": "https://live.shopee.sg/share?from=live&session=802458&share_user_id=",
        "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    }

    if cookies:
        headers["Cookie"] = cookies

    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    is_living = False

    landing_untrusted = False
    if "live.shopee" not in url and "uid" not in url:
        url_result = await http.request(url, proxy_addr=proxy_addr, headers=headers, redirect_url=True)
        if isinstance(url_result, str) and url_result:
            _landing = url_result
            if _shopee_is_allowed_host(_landing):
                try:
                    _landing_host = (urllib.parse.urlparse(_landing).hostname or "").lower()
                    _internal_reason = targets.host_rejection_reason(_landing_host) if _landing_host else "缺少主机名"
                except Exception as e:
                    _internal_reason = f"内网判定异常: {type(e).__name__}"
                if _internal_reason is None:
                    url = _landing
                else:
                    landing_untrusted = True
                    logger.warning(
                        i18n.tr(
                            "Shopee 短链落地页不可信，已忽略跳转: {url} ({reason})",
                            url=utils.mask_credentials(_landing),
                            reason=_internal_reason,
                        )
                    )
            else:
                landing_untrusted = True
                logger.warning(
                    i18n.tr(
                        "Shopee 短链跳转到非白名单主机，已忽略跳转: {url}",
                        url=utils.mask_credentials(_landing),
                    )
                )

    if landing_untrusted:
        headers.pop("Cookie", None)

    if "://" not in url or len(url.split("/")) < 3 or not url.split("/")[2]:
        return result

    host_suffix = _shopee_host_suffix(url)
    if "live.shopee" in url:
        is_living = get_params(url, "uid") is None

    uid = get_params(url, "uid")
    api_host = f"https://live.shopee.{host_suffix}"
    if not _shopee_is_allowed_host(api_host):
        logger.warning(
            i18n.tr(
                "Shopee 接口域名不在白名单内，本轮按未开播返回: {host}",
                host=utils.mask_credentials(api_host),
            )
        )
        return result
    session_id = get_params(url, "session")
    if uid:
        json_str = await http.request(
            f"{api_host}/api/v1/shop_page/live/ongoing?uid={urllib.parse.quote(uid, safe='')}",
            proxy_addr=proxy_addr,
            headers=headers,
        )
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        if _dig(json_data, "data") is None:
            _warn_api_abnormal(json_str, "Shopee shop_page/live/ongoing", "data", json_data)
            return result
        if _dig(json_data, "data", "ongoing_live"):
            session_id = _dig_str(json_data, "data", "ongoing_live", "session_id")
            if not session_id:
                _warn_api_abnormal(json_str, "Shopee shop_page/live/ongoing", "data.ongoing_live.session_id", json_data)
                return result
            is_living = True
        else:
            json_str = await http.request(
                f"{api_host}/api/v1/shop_page/live/replay_list?offset=0&limit=1&uid={urllib.parse.quote(uid, safe='')}",
                proxy_addr=proxy_addr,
                headers=headers,
            )
            json_str = _get_str_response(json_str)
            json_data = _loads_dict(json_str)
            if _dig(json_data, "data") is None:
                _warn_api_abnormal(json_str, "Shopee replay_list", "data.replay", json_data)
                return result
            replays = _dig_list(json_data, "data", "replay")
            if replays:
                result["anchor_name"] = _dig_str(replays[0], "nick_name")
                return result

    if not session_id:
        logger.debug(i18n.tr("Shopee 房间链接未带 session 且无进行中的直播，本轮按未开播返回"))
        return result

    json_str = await http.request(
        f"{api_host}/api/v1/session/{urllib.parse.quote(session_id, safe='')}",
        proxy_addr=proxy_addr,
        headers=headers,
    )
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if not json_data.get("data"):
        if not json_data:
            _warn_api_abnormal(json_str, "Shopee session", "data.session", json_data)
        else:
            logger.error(
                "Fetch shopee live data failed, please update the address of the live broadcast room and try again."
            )
        return result
    if _dig(json_data, "data", "session") is None:
        _warn_api_abnormal(json_str, "Shopee session", "data.session", json_data)
        return result
    session = _dig(json_data, "data", "session")
    session_uid = _dig(session, "uid")
    anchor_name = _dig_str(session, "nickname")
    live_status = _dig(session, "status")
    result["anchor_name"] = anchor_name
    result["uid"] = f"uid={session_uid}&session={session_id}"
    if live_status == 1 and is_living:
        flv_url = _dig_str(session, "play_url")
        if not flv_url:
            _warn_api_abnormal(json_str, "Shopee session", "data.session.play_url", json_data)
            return result
        title = _dig_str(session, "title")
        result |= {"is_live": True, "title": title, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_youtube_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    if cookies:
        headers["Cookie"] = cookies

    html_str = await http.request(url, proxy_addr=proxy_addr, headers=headers)
    html_str = _get_str_response(html_str)
    json_str_match = re.search("var ytInitialPlayerResponse = (.*?);var meta = document\\.createElement", html_str)
    if not json_str_match:
        raise ValueError("Failed to find ytInitialPlayerResponse")
    json_str = json_str_match.group(1)
    json_data = _loads_dict(json_str)
    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    video_details = _dig(json_data, "videoDetails")
    if video_details is None:
        if not json_data:
            _warn_api_abnormal(json_str, "YouTube ytInitialPlayerResponse", "videoDetails", json_data)
        logger.error("Error: Please log in to YouTube on your device's webpage and configure cookies in the config.ini")
        return result
    result["anchor_name"] = _dig_str(video_details, "author")
    live_status = _dig(video_details, "isLive")
    if live_status:
        live_title = _dig_str(video_details, "title")
        m3u8_url = _dig_str(json_data, "streamingData", "hlsManifestUrl")
        if not m3u8_url:
            _warn_api_abnormal(json_str, "YouTube ytInitialPlayerResponse", "streamingData.hlsManifestUrl", json_data)
            return result
        play_url_list = await get_play_url_list(m3u8_url, proxy=proxy_addr, header=headers)
        result |= {"is_live": True, "title": live_title, "m3u8_url": m3u8_url, "play_url_list": play_url_list}
    return result


_TAOBAO_TICKET_KEYS = ("_m_h5_tk", "_m_h5_tk_enc")


def _cookie_str_to_dict(cookie_str: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for segment in cookie_str.split(";"):
        name, sep, value = segment.strip().partition("=")
        if sep and name:
            parsed[name] = value.strip()
    return parsed


@trace_error_decorator
async def get_taobao_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "Referer": "https://huodong.m.taobao.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        "Cookie": "",
    }

    if cookies:
        headers["Cookie"] = cookies

    live_id = get_params(url, "liveId")
    if not live_id:
        html_str = await http.request(url, proxy_addr=proxy_addr, headers=headers)
        html_str = _get_str_response(html_str)
        redirect_url_match = re.findall("var url = '(.*?)';", html_str)
        if not redirect_url_match:
            raise ValueError("Failed to find redirect_url")
        redirect_url = redirect_url_match[0]
        live_id = get_params(redirect_url, "id")

    if not live_id:
        raise ValueError("Failed to find live_id")

    params = {
        "jsv": "2.7.0",
        "appKey": "12574478",
        "t": "1733104933120",
        "sign": "",
        "AntiFlood": "true",
        "AntiCreep": "true",
        "api": "mtop.mediaplatform.live.livedetail",
        "v": "4.0",
        "preventFallback": "true",
        "type": "jsonp",
        "dataType": "jsonp",
        "callback": "mtopjsonp1",
        "data": '{"liveId":"' + live_id + '","creatorId":null}',
    }

    for _ in range(2):
        t13 = int(time.time() * 1000)
        params["t"] = str(t13)

        if "_m_h5_tk" in headers.get("Cookie", ""):
            app_key = "12574478"
            cookie_str = headers.get("Cookie", "")
            _m_h5_tk = _cookie_str_to_dict(cookie_str).get("_m_h5_tk", "")
            if _m_h5_tk:
                pre_sign_str = f"{_m_h5_tk.split('_')[0]}&{t13}&{app_key}&" + params["data"]
                sign = hashlib.md5(pre_sign_str.encode("utf-8")).hexdigest()
                params["sign"] = sign
        api = f"https://h5api.m.taobao.com/h5/mtop.mediaplatform.live.livedetail/4.0/?{urllib.parse.urlencode(params)}"
        result_tuple = await http.request(
            url=api, proxy_addr=proxy_addr, headers=headers, timeout=20, return_cookies=True, include_cookies=True
        )
        if isinstance(result_tuple, tuple) and len(result_tuple) == 2:
            jsonp_str, new_cookie = result_tuple
        else:
            jsonp_str = str(result_tuple) if result_tuple else ""
            new_cookie = {}
        try:
            json_data = network_urls.parse_jsonp(jsonp_str)
        except Exception as e:
            logger.warning(i18n.tr("淘宝响应不是合法 JSONP，本轮重试: {type_name}", type_name=type(e).__name__))
            json_data = {}

        if new_cookie and "_m_h5_tk" in new_cookie:
            merged_cookie = _cookie_str_to_dict(headers.get("Cookie", ""))
            for _ticket_key in _TAOBAO_TICKET_KEYS:
                if _ticket_key in new_cookie:
                    merged_cookie[_ticket_key] = new_cookie[_ticket_key]
            new_cookie_str = network_urls.format_cookies(merged_cookie)
            headers["Cookie"] = new_cookie_str
            try:
                from src.recording import state

                _cfg_path = f"{script_path}/config/config.ini"
                _old_cookie = config_files.read_ini_value(_cfg_path, "Cookie", "taobao_cookie")
                if _old_cookie != new_cookie_str:
                    with state.file_update_lock:
                        config_files.update_config(_cfg_path, "Cookie", "taobao_cookie", new_cookie_str)
                    logger.info(
                        i18n.tr(
                            "The value of {key} under [{section}] in the configuration file has been updated.",
                            key="taobao_cookie",
                            section="Cookie",
                        )
                    )
            except Exception as e:
                logger.warning(
                    i18n.tr(
                        "淘宝 cookie 回写配置失败（已忽略）: {type_name}: {e}",
                        type_name=type(e).__name__,
                        e=utils.mask_credentials(str(e)),
                    )
                )

        if json_data and "ret" in json_data:
            ret_value = json_data["ret"]
            if isinstance(ret_value, list) and len(ret_value) > 0:
                if "哎哟喂,被挤爆啦,请稍后重试" in str(ret_value[0]):
                    raise RuntimeError(f"Please change your taobao cookie: {ret_value}")

                ret_msg = ret_value
                if str(ret_msg[0]).startswith("SUCCESS"):
                    live_status_obj = _dig(json_data, "data")
                    if not isinstance(live_status_obj, dict):
                        _warn_api_abnormal(jsonp_str, "淘宝 mtop livedetail", "data", json_data)
                        return {"anchor_name": "", "is_live": False}
                    live_status_data = cast(dict[str, object], live_status_obj)
                    broadcaster = live_status_data.get("broadCaster")
                    if isinstance(broadcaster, dict):
                        bc_name = broadcaster.get("accountName") or broadcaster.get("nick")
                        anchor_name = bc_name if isinstance(bc_name, str) else ""
                    else:
                        anchor_name = broadcaster if isinstance(broadcaster, str) else ""
                    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
                    live_status = live_status_data.get("streamStatus")

                    def get_sort_key(item: dict[str, object]) -> int:
                        definition_priority = {"lld": 0, "ld": 1, "md": 2, "hd": 3, "ud": 4}
                        def_value = item.get("definition") or item.get("newDefinition")
                        priority = definition_priority.get(str(def_value), -1)
                        return int(priority)

                    if live_status == "1":
                        live_title = live_status_data.get("title")
                        play_url_list = cast(list[dict[str, object]], live_status_data.get("liveUrlList", []))
                        play_url_list = sorted(play_url_list, key=get_sort_key, reverse=True)
                        result |= {
                            "is_live": True,
                            "title": live_title,
                            "play_url_list": play_url_list,
                            "live_id": live_id,
                        }

                    return result
            else:
                logger.debug("淘宝 mtop 返回的 ret 为空/非数组，本轮无结果")
    if "_m_h5_tk" not in _cookie_str_to_dict(headers.get("Cookie", "")):
        raise RuntimeError("Try to update cookie failed, please update the cookies in the configuration file")
    return {"anchor_name": "", "is_live": False}


@trace_error_decorator
async def get_jd_stream_url(url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None) -> dict[str, object]:
    headers = {
        "User-Agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
        "origin": "https://lives.jd.com",
        "referer": "https://lives.jd.com/",
        "x-referer-page": "https://lives.jd.com/",
    }

    if cookies:
        headers["Cookie"] = cookies

    redirect_url_result = await http.request(url, proxy_addr=proxy_addr, headers=headers, redirect_url=True)
    if isinstance(redirect_url_result, str) and redirect_url_result:
        redirect_url = redirect_url_result
    else:
        redirect_url = url

    author_id = get_params(redirect_url, "authorId")
    result: dict[str, object] = {"anchor_name": "", "is_live": False}
    live_id_str: str = ""
    if not author_id:
        live_id_match = re.search("#/(.*?)\\?origin", redirect_url)
        if not live_id_match:
            return result
        live_id_str = live_id_match.group(1)
        result["anchor_name"] = f"jd_{live_id_str}"
    else:
        data = {
            "functionId": "talent_head_findTalentMsg",
            "appid": "dr_detail",
            "body": '{"authorId":"' + author_id + '","monitorSource":"1","userId":""}',
        }
        info_api = "https://api.m.jd.com/talent_head_findTalentMsg"
        json_str = await http.request(info_api, data=data, proxy_addr=proxy_addr, headers=headers)
        json_str = _get_str_response(json_str)
        json_data = _loads_dict(json_str)
        jd_result = cast(dict[str, object], _dig(json_data, "result") or {})
        if not jd_result:
            _warn_api_abnormal(json_str, "京东 talent_head_findTalentMsg", "result", json_data)
            return result
        anchor_name = _dig_str(jd_result, "talentName")
        result["anchor_name"] = anchor_name
        if "livingRoomJump" not in jd_result:
            return result
        live_id_str = _dig_str(jd_result, "livingRoomJump", "params", "id")
        if not live_id_str:
            _warn_api_abnormal(json_str, "京东 talent_head_findTalentMsg", "result.livingRoomJump.params.id", json_data)
            return result
    params = {"body": '{"liveId": "' + live_id_str + '"}', "functionId": "getImmediatePlayToM", "appid": "h5-live"}

    api = f"https://api.m.jd.com/client.action?{urllib.parse.urlencode(params)}"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "data") is None:
        _warn_api_abnormal(json_str, "京东 getImmediatePlayToM", "data", json_data)
        return result
    live_status = _dig(json_data, "data", "status")
    if live_status == 1:
        if author_id:
            data = {
                "functionId": "jdTalentContentList",
                "appid": "dr_detail",
                "body": '{"authorId":"' + author_id + '","type":1,"userId":"","page":1,"offset":"-1",'
                '"monitorSource":"1","pageSize":1}',
            }
            json_str2 = await http.request(
                "https://api.m.jd.com/jdTalentContentList", data=data, proxy_addr=proxy_addr, headers=headers
            )
            json_str2 = _get_str_response(json_str2)
            json_data2 = _loads_dict(json_str2)
            contents = _dig_list(json_data2, "result", "content")
            if contents:
                result["title"] = _dig_str(contents[0], "title")
            else:
                _warn_api_abnormal(json_str2, "京东 jdTalentContentList", "result.content[0].title", json_data2)

        flv_url = _dig_str(json_data, "data", "videoUrl")
        m3u8_url = _dig_str(json_data, "data", "h5VideoUrl")
        if not flv_url and not m3u8_url:
            _warn_api_abnormal(json_str, "京东 getImmediatePlayToM", "data.videoUrl / data.h5VideoUrl", json_data)
            return result
        result |= {"is_live": True, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": m3u8_url}
    return result


@trace_error_decorator
async def get_faceit_stream_data(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "Referer": "https://www.faceit.com/zh/players/qpjzz/stream",
        "faceit-referer": "web-next",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }

    if cookies:
        headers["Cookie"] = cookies
    nickname_matches = re.findall("/players/(.*?)/stream", url)
    if not nickname_matches:
        raise ValueError("Failed to find faceit nickname in url")
    nickname = nickname_matches[0]
    api = f"https://www.faceit.com/api/users/v1/nicknames/{nickname}"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    user_id = _dig(json_data, "payload", "id")
    if user_id is None:
        _warn_api_abnormal(json_str, "Faceit users/nicknames", "payload.id", json_data)
        raise RuntimeError("Failed to retrieve Faceit user id")
    api2 = f"https://www.faceit.com/api/stream/v1/streamings?userId={user_id}"
    json_str2 = await http.request(api2, proxy_addr=proxy_addr, headers=headers)
    json_str2 = _get_str_response(json_str2)
    json_data2 = _loads_dict(json_str2)
    platform_info = _dig(json_data2, "payload", 0)
    if platform_info is None:
        if _dig(json_data2, "payload") is None:
            _warn_api_abnormal(json_str2, "Faceit stream/streamings", "payload", json_data2)
        return {"anchor_name": "", "is_live": False}
    anchor_name = _dig_str(platform_info, "userNickname")
    anchor_id = _dig_str(platform_info, "platformId")
    platform = _dig_str(platform_info, "platform")
    if platform == "twitch":
        result = await get_twitchtv_stream_data(f"https://www.twitch.tv/{anchor_id}", proxy_addr, cookies)
        result["anchor_name"] = anchor_name
    else:
        result = {"anchor_name": anchor_name, "is_live": False}
    return result


@trace_error_decorator
async def get_migu_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "origin": "https://www.miguvideo.com",
        "referer": "https://www.miguvideo.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "appCode": "miguvideo_default_www",
        "appId": "miguvideo",
        "channel": "H5",
    }

    if cookies:
        headers["Cookie"] = cookies

    web_id = url.split("?")[0].rsplit("/")[-1]
    api = f"https://vms-sc.miguvideo.com/vms-match/v6/staticcache/basic/basic-data/{web_id}/miguvideo"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    body = cast(dict[str, object], _dig(json_data, "body") or {})
    if not body:
        _warn_api_abnormal(json_str, "咪咕 basic-data", "body", json_data)
    anchor_name = _dig_str(body, "title")
    detail_title = _dig_str(body, "detailPageTitle")
    live_title = f"{anchor_name}-{detail_title}" if detail_title else anchor_name
    room_id = _dig(body, "pId")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if not room_id:
        return result

    params = {
        "contId": room_id,
        "rateType": "3",
        "clientId": str(uuid.uuid4()),
        "timestamp": int(time.time() * 1000),
        "flvEnable": "true",
        "xh265": "false",
        "chip": "mgwww",
        "channelId": "",
    }

    api = f"https://webapi.miguvideo.com/gateway/playurl/v3/play/playurl?{urllib.parse.urlencode(params)}"
    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    if _dig(json_data, "body", "content") is None:
        _warn_api_abnormal(json_str, "咪咕 playurl", "body.content.currentLive", json_data)
        return result
    live_status = _dig(json_data, "body", "content", "currentLive")
    if live_status != "1":
        return result
    result["title"] = live_title
    source_url = _dig_str(json_data, "body", "urlInfo", "url")
    if not source_url:
        _warn_api_abnormal(json_str, "咪咕 playurl", "body.urlInfo.url", json_data)
        return result

    async def _get_dd_calcu(url: str) -> str:
        try:
            return await javascript.run_node_script_async(f"{JS_SCRIPT_PATH}/migu.js", url, timeout=30)
        except Exception:
            raise ProgramError("Failed to execute JS code. Please check if the Node.js environment")

    real_source_url = await _get_dd_calcu(source_url)
    if ".m3u8" in real_source_url:
        m3u8_url = await http.request(real_source_url, proxy_addr=proxy_addr, headers=headers, redirect_url=True)
        m3u8_url = _get_str_response(m3u8_url)
        if not m3u8_url:
            return result
        result["m3u8_url"] = m3u8_url
        result["record_url"] = m3u8_url
    else:
        result["flv_url"] = real_source_url
        result["record_url"] = real_source_url
    result["is_live"] = True
    return result


@trace_error_decorator
async def get_lianjie_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "accept-language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    }

    if cookies:
        headers["cookie"] = cookies

    room_id = url.split("?")[0].rsplit("lailianjie.com/", maxsplit=1)[-1]
    play_api = f"https://api.lailianjie.com/ApiServices/service/live/getRoomInfo?&_$t=&_sign=&roomNumber={room_id}"
    json_str = await http.request(play_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    room_data = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(room_data, "isonline") is None:
        _warn_api_abnormal(json_str, "连接直播 getRoomInfo", "data.isonline", json_data)
        return {"anchor_name": _dig_str(room_data, "nickname"), "is_live": False}
    anchor_name = _dig_str(room_data, "nickname")
    live_status = _dig(room_data, "isonline")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status == 1:
        title = _dig_str(room_data, "defaultRoomTitle")
        webrtc_url = _dig_str(room_data, "videoUrl")
        if not str(webrtc_url).startswith("webrtc://"):
            return result
        https_url = "https://" + webrtc_url.split("webrtc://")[1]
        if "?" not in https_url:
            _warn_api_abnormal(json_str, "连接直播 getRoomInfo", "data.videoUrl", json_data)
            return result
        flv_url = https_url.replace("?", ".flv?")
        m3u8_url = https_url.replace("?", ".m3u8?")
        result |= {"is_live": True, "title": title, "m3u8_url": m3u8_url, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_laixiu_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    def generate_uuid(ua_type: str) -> str:
        """获取来秀直播流地址。"""
        if ua_type == "mobile":
            return str(uuid.uuid4())
        return str(uuid.uuid4()).replace("-", "")

    def calculate_sign(ua_type: str = "pc") -> dict[str, int | str]:
        a = int(time.time() * 1000)
        s = generate_uuid(ua_type)
        u = "kk792f28d6ff1f34ec702c08626d454b39pro"

        input_str = f"web{s}{a}{u}"
        md5_hash = hashlib.md5(input_str.encode("utf-8")).hexdigest()

        return {"timestamp": a, "imei": s, "requestId": md5_hash, "inputString": input_str}

    sign_data = calculate_sign(ua_type="pc")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "mobileModel": "web",
        "timestamp": str(sign_data["timestamp"]),
        "loginType": "2",
        "versionCode": "10003",
        "imei": str(sign_data["imei"]),
        "requestId": str(sign_data["requestId"]),
        "channel": "9",
        "version": "1.0.0",
        "os": "web",
        "platform": "WEB",
        "Origin": "https://www.imkktv.com",
        "Referer": "https://www.imkktv.com/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    }

    if cookies:
        headers["cookie"] = cookies

    pattern = r"(?:roomId|anchorId)=(.*?)(?=&|$)"
    match = re.search(pattern, url)
    room_id = match.group(1) if match else ""
    play_api = f"https://api.imkktv.com/liveroom/getShareLiveVideo?roomId={room_id}"
    json_str = await http.request(play_api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    room_data = cast(dict[str, object], _dig(json_data, "data") or {})
    if _dig(room_data, "playStatus") is None:
        _warn_api_abnormal(json_str, "来秀 getShareLiveVideo", "data.playStatus", json_data)
        return {"anchor_name": _dig_str(room_data, "nickname"), "is_live": False}
    anchor_name = _dig_str(room_data, "nickname")
    live_status = _dig(room_data, "playStatus") == 0

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if live_status:
        flv_url = _dig_str(room_data, "playUrl")
        if not flv_url:
            _warn_api_abnormal(json_str, "来秀 getShareLiveVideo", "data.playUrl", json_data)
            return result
        result |= {"is_live": True, "flv_url": flv_url, "record_url": flv_url}
    return result


@trace_error_decorator
async def get_picarto_stream_url(
    url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None
) -> dict[str, object]:
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "accept-language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    }

    if cookies:
        headers["cookie"] = cookies

    anchor_id = url.split("?")[0].rsplit("/", maxsplit=1)[-1]
    api = f"https://ptvintern.picarto.tv/api/channel/detail/{anchor_id}"

    json_str = await http.request(api, proxy_addr=proxy_addr, headers=headers)
    json_str = _get_str_response(json_str)
    json_data = _loads_dict(json_str)
    channel = cast(dict[str, object], _dig(json_data, "channel") or {})
    if _dig(channel, "online") is None:
        _warn_api_abnormal(json_str, "Picarto channel detail", "channel.online", json_data)
        return {"anchor_name": _dig_str(channel, "name"), "is_live": False}
    anchor_name = _dig_str(channel, "name")
    live_status = _dig(channel, "online")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": live_status}
    if live_status:
        title = _dig_str(channel, "title")
        m3u8_url = f"https://1-edge1-us-newyork.picarto.tv/stream/hls/golive+{anchor_name}/index.m3u8"
        result |= {"is_live": True, "title": title, "m3u8_url": m3u8_url, "record_url": m3u8_url}
    return result
