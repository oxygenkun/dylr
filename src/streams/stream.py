import json
import re
import urllib.parse
from typing import TypedDict, TypeVar, cast

from loguru import logger

from src.network.http import is_reachable
from src.streams.errors import trace_error_decorator
from src.streams.spider import get_bilibili_stream_data, get_douyu_stream_data
from src.support import i18n

_PadT = TypeVar("_PadT")


class DouyinStreamUrl(TypedDict, total=False):
    anchor_name: str | None
    status: int
    stream_url: "DouyinStreamInner"


class DouyinStreamInner(TypedDict, total=False):
    flv_pull_url: dict[str, str]
    hls_pull_url_map: dict[str, str]
    hevc_flv_url: str


class TiktokStreamUrl(TypedDict, total=False):
    LiveRoom: "TiktokLiveRoom"


class TiktokLiveRoom(TypedDict, total=False):
    liveRoomUserInfo: "TiktokUserInfo"
    liveRoom: "TiktokLiveDetail"
    title: str


class TiktokUserInfo(TypedDict, total=False):
    user: "TiktokUser"


class TiktokUser(TypedDict, total=False):
    status: int
    nickname: str
    uniqueId: str


class TiktokLiveDetail(TypedDict, total=False):
    title: str
    streamData: "TiktokStreamDataOuter"


class TiktokStreamDataOuter(TypedDict, total=False):
    pull_data: "TiktokPullData"


class TiktokPullData(TypedDict, total=False):
    stream_data: str


class KuaishouM3u8Item(TypedDict, total=False):
    url: str


class KuaishouFlvItem(TypedDict, total=False):
    url: str
    bitrate: int


class KuaishouStreamUrl(TypedDict, total=False):
    type: int
    is_live: bool
    anchor_name: str
    m3u8_url_list: list[KuaishouM3u8Item]
    flv_url_list: list[KuaishouFlvItem]


class HuyaStreamUrl(TypedDict, total=False):
    data: list["HuyaDataItem"]


class HuyaDataItem(TypedDict, total=False):
    gameLiveInfo: "HuyaGameLiveInfo"
    gameStreamInfoList: list["HuyaStreamInfo"]


class HuyaGameLiveInfo(TypedDict, total=False):
    introduction: str
    nick: str
    bitRate: int


class HuyaStreamInfo(TypedDict, total=False):
    sCdnType: str
    sFlvUrl: str
    sStreamName: str
    sFlvUrlSuffix: str
    sHlsUrl: str
    sHlsUrlSuffix: str
    sFlvAntiCode: str
    sHlsAntiCode: str


class DouyuStreamUrl(TypedDict, total=False):
    is_live: bool
    anchor_name: str | None
    room_id: str | int


class DouyuFlvData(TypedDict, total=False):
    rtmp_url: str
    rtmp_live: str
    rate: int | str


class YyStreamUrl(TypedDict, total=False):
    anchor_name: str
    title: str
    avp_info_res: "YyAvpInfoRes"


class YyAvpInfoRes(TypedDict, total=False):
    stream_line_addr: dict[str, "YyCdnInfo"]


class YyCdnInfo(TypedDict, total=False):
    cdn_info: "YyCdnDetail"


class YyCdnDetail(TypedDict, total=False):
    url: str


class BilibiliStreamUrl(TypedDict, total=False):
    anchor_name: str
    live_status: int
    title: str
    room_url: str


class BilibiliPlayData(TypedDict, total=False):
    current_qn: int | str
    accept_qn: list[int]
    url: str


class NeteaseStreamUrl(TypedDict, total=False):
    is_live: bool
    anchor_name: str
    title: str
    m3u8_url: str
    stream_list: "NeteaseStreamList"


class NeteaseStreamList(TypedDict, total=False):
    resolution: dict[str, "NeteaseResolution"]


class NeteaseResolution(TypedDict, total=False):
    cdn: dict[str, str]


class GenericStreamUrl(TypedDict, total=False):
    is_live: bool
    anchor_name: str
    title: str
    m3u8_url: str
    flv_url: str
    play_url_list: list[str | dict[str, str]]


class StreamQuality(TypedDict, total=False):
    url: str
    vbitrate: int
    resolution: tuple[int, int]


class TiktokSdkParams(TypedDict, total=False):
    vbitrate: int
    VCodec: str
    resolution: str


QUALITY_MAPPING = {"OD": 0, "BD": 1, "UHD": 2, "HD": 3, "SD": 4, "LD": 5}
QUALITY_MAPPING_BIT = {
    "OD": 99999,
    "BD": 4000,
    "BD30": 30000,
    "BD20": 20000,
    "BD8": 8000,
    "BD4": 4000,
    "UHD": 2000,
    "HD": 1000,
    "SD": 800,
    "LD": 600,
}

QUALITY_LEVEL = {"OD": 0, "BD": 0, "BD30": 1, "BD20": 2, "BD8": 3, "BD4": 4, "UHD": 5, "HD": 6, "SD": 7, "LD": 8}

QUALITY_CODE_TO_ZH = {
    "OD": "原画",
    "BD": "蓝光",
    "BD30": "蓝光30M",
    "BD20": "蓝光20M",
    "BD8": "蓝光8M",
    "BD4": "蓝光4M",
    "UHD": "超清",
    "HD": "高清",
    "SD": "标清",
    "LD": "流畅",
}

BD_SUB_TIERS = frozenset({"BD30", "BD20", "BD8", "BD4"})

NETEASE_QUALITY_MAP = {"blueray": "OD", "ultra": "UHD", "high": "HD", "standard": "SD"}

DOUYIN_KEY_TO_CODE: dict[str, str] = {
    "ORIGIN": "OD",
    "FULL_HD1": "BD",
    "HD1": "HD",
    "SD1": "SD",
    "SD2": "LD",
}

_PLAY_URL_KEY_ORDER: tuple[str, ...] = ("url", "play_url", "m3u8_url", "flv_url")

HUYA_FIXED_TIERS: tuple[tuple[str, int], ...] = (("BD30", 30000), ("BD20", 20000), ("BD8", 8000), ("BD4", 4000))
HUYA_RATIO_TO_CODE = {
    "30000": "BD30",
    "20000": "BD20",
    "8000": "BD8",
    "4000": "BD4",
    "2000": "UHD",
    "1000": "HD",
    "500": "LD",
    "250": "LD",
}
HUYA_RATIO_BY_CODE: dict[str, int] = {
    "BD30": 30000,
    "BD20": 20000,
    "BD8": 8000,
    "BD4": 4000,
    "UHD": 2000,
    "HD": 1000,
    "SD": 800,
    "LD": 500,
}
HUYA_CODE_ORDER: tuple[str, ...] = ("BD30", "BD20", "BD8", "BD4", "UHD", "HD", "SD", "LD")


def huya_code_for_ratio(ratio: int) -> str:
    exact = HUYA_RATIO_TO_CODE.get(str(ratio))
    if exact:
        return exact
    best_code = ""
    best_key: tuple[int, int] | None = None
    for code, tier_ratio in HUYA_RATIO_BY_CODE.items():
        key = (abs(tier_ratio - ratio), -QUALITY_LEVEL[code])
        if best_key is None or key < best_key:
            best_key, best_code = key, code
    return best_code


DOUYU_RATE_BY_CODE = {
    "OD": "0",
    "BD": "0",
    "BD30": "8200",
    "BD20": "8200",
    "BD8": "8200",
    "BD4": "4000",
    "UHD": "3",
    "HD": "2",
    "SD": "1",
    "LD": "1",
}
DOUYU_RATE_TO_CODE = {
    "0": "OD",
    "1": "SD",
    "2": "HD",
    "3": "UHD",
    "4": "BD4",
    "8200": "BD8",
    "4000": "BD4",
    "2000": "UHD",
}
DOUYU_RATE_DESC = ("8200", "4000", "3", "2", "1")


def bitrate_to_quality(bitrate: int) -> str:
    if not bitrate or bitrate <= 0:
        return "OD"
    for code in ("LD", "SD", "HD", "UHD", "BD", "OD"):
        if bitrate <= QUALITY_MAPPING_BIT[code]:
            return code
    return "OD"


def code_to_zh(code: str | None) -> str:
    if not code:
        return code or ""
    return QUALITY_CODE_TO_ZH.get(code, code)


def is_downgrade(requested: str | None, actual: str | None) -> bool:
    if not requested or not actual:
        return False
    req_level = QUALITY_LEVEL.get(requested)
    act_level = QUALITY_LEVEL.get(actual)
    if req_level is None or act_level is None:
        return False
    return act_level > req_level


def _pad_list(url_list: list[_PadT], min_length: int = 6) -> list[_PadT] | list[None]:
    if not url_list:
        return [None] * min_length
    while len(url_list) < min_length:
        url_list.append(url_list[-1])
    return url_list


def get_quality_index(quality: str | int | None) -> tuple[str, int]:
    if not quality:
        return list(QUALITY_MAPPING.items())[0]

    quality_str = str(quality).upper()
    if quality_str.isdigit():
        quality_int = int(quality_str[0])
        keys = list(QUALITY_MAPPING.keys())
        if quality_int >= len(keys):
            quality_int = 0
        quality_str = keys[quality_int]
    if quality_str in BD_SUB_TIERS:
        quality_str = "BD"
    if quality_str not in QUALITY_MAPPING:
        quality_str = list(QUALITY_MAPPING.keys())[0]
    return quality_str, QUALITY_MAPPING[quality_str]


def _probe_headers(platform: str) -> dict[str, str]:
    from src.streams.selection import MOBILE_UA, get_record_user_agent

    return {"User-Agent": get_record_user_agent(platform) or MOBILE_UA}


@trace_error_decorator
async def get_douyin_stream_url(
    json_data: dict[str, object], video_quality: str | None = None, proxy_addr: str | None = None
) -> dict[str, object]:
    d = cast(DouyinStreamUrl, cast(object, json_data))
    anchor_name = d.get("anchor_name")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    status = d.get("status", 4)

    if status == 2:
        stream_url = d.get("stream_url") or {}
        flv_pull_url: dict[str, str] = stream_url.get("flv_pull_url") or {}
        m3u8_pull_url: dict[str, str] = stream_url.get("hls_pull_url_map") or {}
        hevc_flv_url = stream_url.get("hevc_flv_url")

        def _sort_quality_items(dd: dict[str, str]) -> list[tuple[str, str]]:
            """保留画质标签：将 dict items 按画质等级降序（OD>BD>UHD>HD>SD>LD）排序。"""
            order = {name: QUALITY_MAPPING.get(code, 99) for name, code in DOUYIN_KEY_TO_CODE.items()}
            order.update(QUALITY_MAPPING)
            return sorted(dd.items(), key=lambda kv: order.get(kv[0].upper(), 99))

        flv_pairs = _sort_quality_items(flv_pull_url)
        m3u8_pairs = _sort_quality_items(m3u8_pull_url)

        def _norm_code(name: str) -> str:
            """可用画质档位（统一为代码：ORIGIN→OD，FULL_HD1/HD1/SD1/SD2 → BD/HD/SD/LD）。"""
            key = name.upper()
            code = DOUYIN_KEY_TO_CODE.get(key)
            if code:
                return code
            if key and key not in QUALITY_LEVEL:
                logger.warning(
                    i18n.tr(
                        "[抖音直播] 未知画质键 {name}，无法映射为统一画质代码（按最低档参与排序与降级判定）",
                        name=name,
                    )
                )
            return key

        available_qualities = (
            [_norm_code(k) for k, _ in flv_pairs] if flv_pairs else [_norm_code(k) for k, _ in m3u8_pairs]
        )

        video_quality, quality_index = get_quality_index(video_quality)
        flv_idx = min(quality_index, len(flv_pairs) - 1) if flv_pairs else 0
        m3u8_idx = min(quality_index, len(m3u8_pairs) - 1) if m3u8_pairs else 0
        flv_quality_name, flv_url = flv_pairs[flv_idx] if flv_pairs else ("", "")
        m3u8_quality_name, m3u8_url = m3u8_pairs[m3u8_idx] if m3u8_pairs else ("", "")
        actual_quality = _norm_code(flv_quality_name or m3u8_quality_name)

        m3u8_codec = urllib.parse.parse_qs(urllib.parse.urlparse(m3u8_url or "").query).get("codec", [""])[0]
        m3u8_is_hevc = "h265" in m3u8_codec.lower() or "hevc" in m3u8_codec.lower()
        flv_url_list: list[str] = []
        use_hevc_flv = quality_index == 0 and bool(hevc_flv_url) and not m3u8_is_hevc
        if use_hevc_flv and hevc_flv_url:
            from src.streams.selection import _is_h265

            if flv_url and flv_url != hevc_flv_url and not _is_h265(flv_url):
                flv_url_list.append(flv_url)
            flv_url = hevc_flv_url
        if m3u8_url:
            ok = await is_reachable(
                url=m3u8_url, proxy_addr=proxy_addr, platform="抖音直播", headers=_probe_headers("抖音直播")
            )
        else:
            ok = True
        if not ok:
            if flv_pairs:
                index = flv_idx + 1 if flv_idx < len(flv_pairs) - 1 else max(flv_idx - 1, 0)
            else:
                index = m3u8_idx
            if m3u8_pairs:
                m3u8_index = min(index, len(m3u8_pairs) - 1)
                m3u8_quality_name, m3u8_url = m3u8_pairs[m3u8_index]
            if not use_hevc_flv and flv_pairs:
                flv_quality_name, flv_url = flv_pairs[index]
            actual_quality = _norm_code(flv_quality_name or m3u8_quality_name)
        result |= {
            "is_live": True,
            "quality": video_quality,
            "actual_quality": actual_quality,
            "available_qualities": available_qualities,
            "m3u8_url": m3u8_url,
            "flv_url": flv_url,
            "flv_url_list": flv_url_list,
            "record_url": m3u8_url or flv_url,
        }
    return result


@trace_error_decorator
async def get_tiktok_stream_url(
    json_data: dict[str, object] | None, video_quality: str | None = None, proxy_addr: str | None = None
) -> dict[str, object]:
    if not json_data:
        return {"anchor_name": None, "is_live": False}

    def get_video_quality_url(stream: dict[str, object], q_key: str) -> list[StreamQuality]:
        play_list: list[StreamQuality] = []
        for key in stream:
            try:
                url_info = cast(dict[str, object], stream[key])
                main_info = cast(dict[str, object], url_info.get("main") or {})
                sdk_params_raw = main_info.get("sdk_params")
                sdk_params: TiktokSdkParams = {}
                if isinstance(sdk_params_raw, str):
                    sdk_params = cast(TiktokSdkParams, json.loads(sdk_params_raw))
                vbitrate = int(sdk_params.get("vbitrate", 0))
                v_codec = sdk_params.get("VCodec", "")

                play_url = ""
                url_value = cast(str, url_info.get(q_key) or "")
                if url_value:
                    if url_value.endswith((".flv", ".m3u8")):
                        play_url = url_value + "?codec=" + v_codec
                    else:
                        play_url = url_value + "&codec=" + v_codec

                resolution = sdk_params.get("resolution", "")
                if vbitrate != 0 and resolution:
                    width, height = map(int, resolution.split("x"))
                    play_list.append({"url": play_url, "vbitrate": vbitrate, "resolution": (width, height)})
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                logger.debug(
                    i18n.tr(
                        "TikTok 流条目解析异常，跳过该条目: {key} - {type_name}: {e}",
                        key=str(key),
                        type_name=type(e).__name__,
                        e=e,
                    )
                )
                continue

        play_list.sort(
            key=lambda x: (-x.get("vbitrate", 0), -x.get("resolution", (0, 0))[0], -x.get("resolution", (0, 0))[1])
        )
        return play_list

    t = cast(TiktokStreamUrl, cast(object, json_data))
    live_room = t.get("LiveRoom") or {}
    user_info = live_room.get("liveRoomUserInfo") or {}
    user = user_info.get("user") or {}
    anchor_name = f"{user.get('nickname', '')}-{user.get('uniqueId', '')}"
    status = user.get("status", 4)

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}

    if status == 2:
        live_detail = live_room.get("liveRoom") or {}
        stream_data_outer = live_detail.get("streamData") or {}
        pull_data = stream_data_outer.get("pull_data") or {}
        stream_data_raw = pull_data.get("stream_data", "")
        parsed: dict[str, object] = cast(dict[str, object], json.loads(stream_data_raw)) if stream_data_raw else {}
        stream_data = cast(dict[str, object], parsed.get("data", {}) if parsed else {})
        flv_url_list = get_video_quality_url(stream_data, "flv")
        m3u8_url_list = get_video_quality_url(stream_data, "hls")

        if not flv_url_list and not m3u8_url_list:
            return result

        flv_url_list = cast(list[StreamQuality], _pad_list(flv_url_list))
        m3u8_url_list = cast(list[StreamQuality], _pad_list(m3u8_url_list))
        flv_url_list = [x for x in flv_url_list if x and x.get("url")]
        m3u8_url_list = [x for x in m3u8_url_list if x and x.get("url")]
        video_quality, quality_index = get_quality_index(video_quality)
        flv_quality_index = min(quality_index, len(flv_url_list) - 1) if flv_url_list else 0
        m3u8_quality_index = min(quality_index, len(m3u8_url_list) - 1) if m3u8_url_list else 0
        flv_dict: StreamQuality | dict[str, str] = flv_url_list[flv_quality_index] if flv_url_list else {"url": ""}
        m3u8_dict: StreamQuality | dict[str, str] = m3u8_url_list[m3u8_quality_index] if m3u8_url_list else {"url": ""}

        check_url = cast(str, m3u8_dict.get("url") or flv_dict.get("url"))
        if not check_url:
            ok = False
        else:
            ok = await is_reachable(
                url=check_url,
                proxy_addr=proxy_addr,
                http2=False,
                platform="TikTok直播",
                headers=_probe_headers("TikTok直播"),
            )

        if not ok:
            fallback_base = quality_index + 1 if quality_index < 4 else max(quality_index - 1, 0)
            if flv_url_list:
                flv_dict = flv_url_list[min(fallback_base, len(flv_url_list) - 1)]
            if m3u8_url_list:
                m3u8_dict = m3u8_url_list[min(fallback_base, len(m3u8_url_list) - 1)]

        flv_url = cast(str, flv_dict.get("url", ""))
        m3u8_url = cast(str, m3u8_dict.get("url", ""))
        _selected = flv_dict if flv_url else m3u8_dict
        actual_quality = bitrate_to_quality(int(cast(int, _selected.get("vbitrate", 0)) or 0))
        available_qualities = (
            [bitrate_to_quality(x.get("vbitrate", 0)) for x in flv_url_list if x] if flv_url_list else None
        )
        result |= {
            "is_live": True,
            "title": (live_room.get("liveRoom") or {}).get("title", ""),
            "quality": video_quality,
            "actual_quality": actual_quality,
            "available_qualities": available_qualities,
            "m3u8_url": m3u8_url,
            "flv_url": flv_url,
            "record_url": m3u8_url or flv_url,
        }
    return result


@trace_error_decorator
async def get_kuaishou_stream_url(json_data: dict[str, object], video_quality: str | None = None) -> dict[str, object]:
    k = cast(KuaishouStreamUrl, cast(object, json_data))
    if k.get("type") == 1 and not k.get("is_live"):
        return json_data
    live_status = k.get("is_live", False)

    result: dict[str, object] = {"type": 2, "anchor_name": k.get("anchor_name", ""), "is_live": live_status}

    if live_status:
        _, quality_index = get_quality_index(video_quality)
        actual_quality: str | None = None
        available_qualities: list[str] | None = None
        m3u8_list = k.get("m3u8_url_list")
        if m3u8_list:
            m3u8_url_list = m3u8_list[::-1]
            idx = min(quality_index, len(m3u8_url_list) - 1)
            result["m3u8_url"] = m3u8_url_list[idx].get("url", "")

        flv_list = k.get("flv_url_list")
        if flv_list:
            if "bitrate" in flv_list[0]:
                flv_sorted = sorted(flv_list, key=lambda x: x.get("bitrate", 0), reverse=True)
                quality_str = video_quality.upper() if video_quality else "OD"
                if quality_str.isdigit():
                    code, _ = get_quality_index(quality_str)
                    video_quality = code
                    quality_index_bitrate_value = QUALITY_MAPPING_BIT.get(code, 99999)
                else:
                    quality_index_bitrate_value = QUALITY_MAPPING_BIT.get(quality_str, 99999)
                    video_quality = quality_str
                sel_index = next(
                    (i for i, x in enumerate(flv_sorted) if x.get("bitrate", 0) <= quality_index_bitrate_value), None
                )
                if sel_index is None:
                    sel_index = len(flv_sorted) - 1
                selected = flv_sorted[sel_index]
                actual_quality = bitrate_to_quality(selected.get("bitrate", 0))
                available_qualities = [bitrate_to_quality(x.get("bitrate", 0)) for x in flv_sorted]
                result["flv_url"] = selected.get("url", "")
                result["record_url"] = selected.get("url", "")
            else:
                flv_rev = flv_list[::-1]
                idx = min(quality_index, len(flv_rev) - 1)
                flv_url = flv_rev[idx].get("url", "")
                result["flv_url"] = flv_url
                result["record_url"] = flv_url
        result["quality"] = video_quality
        result["actual_quality"] = actual_quality
        result["available_qualities"] = available_qualities
    return result


@trace_error_decorator
async def get_huya_stream_url(json_data: dict[str, object], video_quality: str | None = None) -> dict[str, object]:
    h = cast(HuyaStreamUrl, cast(object, json_data))
    data_list: list[HuyaDataItem] = h.get("data") or []
    if not data_list:
        return {"anchor_name": "", "is_live": False}
    item0 = data_list[0]
    game_live_info = item0.get("gameLiveInfo") or {}
    live_title = game_live_info.get("introduction", "")
    stream_info_list = item0.get("gameStreamInfoList") or []
    anchor_name = game_live_info.get("nick", "")

    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    if not stream_info_list:
        return result

    first_anti = stream_info_list[0].get("sFlvAntiCode") or ""
    quality_list = first_anti.split("&exsphd=")
    bit_rate = game_live_info.get("bitRate") or 0
    try:
        max_ratio = int(bit_rate)
    except TypeError, ValueError:
        max_ratio = 0
    exsphd_ratios: set[int] = set()
    if len(quality_list) > 1:
        for v in cast(list[str], re.findall(r"(?<=264_)\d+", quality_list[1])):
            r = int(v)
            if r > 0:
                exsphd_ratios.add(r)
        if exsphd_ratios:
            max_ratio = max(max_ratio, max(exsphd_ratios))

    actual_quality = video_quality  # OD/BD 默认即请求值
    available_qualities: list[str] | None = None
    ratio_val: str = ""
    available_ratios: set[int] = set()

    if video_quality in BD_SUB_TIERS:
        target_ratio = dict(HUYA_FIXED_TIERS)[video_quality]
        if exsphd_ratios:
            available_ratios = exsphd_ratios
        elif max_ratio > 0:
            available_ratios = {r for r in (30000, 20000, 8000, 4000, 2000, 500) if r <= max_ratio}
        else:
            available_ratios = set()
        if not available_ratios or target_ratio in available_ratios:
            ratio_val = str(target_ratio)
            actual_quality = video_quality
        elif any(r < target_ratio for r in available_ratios):
            chosen = max(r for r in available_ratios if r < target_ratio)
            ratio_val = str(chosen)
            actual_quality = huya_code_for_ratio(chosen) or video_quality
            logger.warning(
                i18n.tr(
                    "[虎牙直播] 请求档位 {video_quality}(ratio={target_ratio}) 不可用(房间最高码率={max_ratio})，降级为 {actual_quality}(ratio={chosen})",
                    video_quality=code_to_zh(video_quality),
                    target_ratio=target_ratio,
                    max_ratio=max_ratio,
                    actual_quality=code_to_zh(actual_quality),
                    chosen=chosen,
                )
            )
        else:
            actual_quality = "OD"
            logger.warning(
                i18n.tr(
                    "[虎牙直播] 请求档位 {video_quality}(ratio={target_ratio}) 不可用(房间最高码率={max_ratio}，无更低档位)，按原画拉流",
                    video_quality=code_to_zh(video_quality),
                    target_ratio=target_ratio,
                    max_ratio=max_ratio,
                )
            )
        available_qualities = (
            ["OD"] + [c for c, r in HUYA_FIXED_TIERS if max_ratio > 0 and r <= max_ratio] + ["UHD", "LD"]
        )
    elif video_quality and video_quality not in ["OD", "BD"]:
        if exsphd_ratios:
            available_ratios = exsphd_ratios
        elif max_ratio > 0:
            available_ratios = {r for r in (30000, 20000, 8000, 4000, 2000, 500) if r <= max_ratio}
        target_ratio = HUYA_RATIO_BY_CODE.get(video_quality or "", 0)
        room_codes = {huya_code_for_ratio(r) for r in available_ratios} - {""}
        available_qualities = ["OD", "BD"] + [c for c in HUYA_CODE_ORDER if c in room_codes]
        if not available_ratios or target_ratio <= 0:
            ratio_val = ""
            actual_quality = "OD"
            logger.warning(
                i18n.tr(
                    "[虎牙直播] 请求档位 {video_quality}(ratio={target_ratio}) 不可用(房间最高码率={max_ratio}，无更低档位)，按原画拉流",
                    video_quality=code_to_zh(video_quality),
                    target_ratio=target_ratio,
                    max_ratio=max_ratio,
                )
            )
        elif target_ratio in available_ratios:
            ratio_val = str(target_ratio)
            actual_quality = video_quality
        else:
            lower = [r for r in available_ratios if r < target_ratio]
            chosen = max(lower) if lower else min(available_ratios)
            ratio_val = str(chosen)
            actual_quality = huya_code_for_ratio(chosen) or video_quality
            logger.warning(
                i18n.tr(
                    "[虎牙直播] 请求档位 {video_quality}(ratio={target_ratio}) 不可用(房间最高码率={max_ratio})，降级为 {actual_quality}(ratio={chosen})",
                    video_quality=code_to_zh(video_quality),
                    target_ratio=target_ratio,
                    max_ratio=max_ratio,
                    actual_quality=code_to_zh(actual_quality),
                    chosen=chosen,
                )
            )

    cdn_priority = ["HS", "HW", "TX", "AL"]

    def _rank(cdn: object) -> int:
        try:
            return cdn_priority.index(str(cdn))
        except ValueError:
            return len(cdn_priority)

    candidates: list[dict[str, str]] = []
    for cdn in sorted(stream_info_list, key=lambda c: _rank(c.get("sCdnType"))):
        s_cdn = cdn.get("sCdnType", "") or ""
        s_stream_name = cdn.get("sStreamName", "")
        s_flv_url = cdn.get("sFlvUrl", "")
        s_flv_suffix = cdn.get("sFlvUrlSuffix", "")
        s_flv_anti = cdn.get("sFlvAntiCode") or ""
        s_hls_url = cdn.get("sHlsUrl", "")
        s_hls_suffix = cdn.get("sHlsUrlSuffix", "")
        s_hls_anti = cdn.get("sHlsAntiCode") or ""
        if not s_stream_name or not s_flv_anti:
            continue
        flv_url = f"{str(s_flv_url).replace('https://', 'http://')}/{s_stream_name}.{s_flv_suffix}?{s_flv_anti}"
        if ratio_val:
            flv_url = flv_url + "&ratio=" + str(ratio_val)
        hls_url = ""
        if s_hls_anti and s_hls_url and s_hls_suffix:
            hls_url = f"{str(s_hls_url).replace('https://', 'http://')}/{s_stream_name}.{s_hls_suffix}?{s_hls_anti}"
            if ratio_val:
                hls_url = hls_url + "&ratio=" + str(ratio_val)
        if hls_url or flv_url:
            candidates.append({"cdn_type": s_cdn, "m3u8_url": hls_url, "flv_url": flv_url})

    if not candidates:
        return result

    primary = candidates[0]
    m3u8_url_list = [c["m3u8_url"] for c in candidates if c["m3u8_url"]]
    flv_url_list = [c["flv_url"] for c in candidates if c["flv_url"]]
    record_url = primary["flv_url"] or primary["m3u8_url"]
    result |= {
        "is_live": True,
        "title": live_title,
        "quality": video_quality,
        "actual_quality": actual_quality,
        "available_qualities": available_qualities,
        "m3u8_url": primary["m3u8_url"],
        "m3u8_url_list": m3u8_url_list,
        "flv_url": primary["flv_url"],
        "flv_url_list": flv_url_list,
        "record_url": record_url,
    }
    return result


@trace_error_decorator
async def get_douyu_stream_url(
    json_data: dict[str, object], video_quality: str | None = None, cookies: str = "", proxy_addr: str | None = None
) -> dict[str, object]:
    dy = cast(DouyuStreamUrl, cast(object, json_data))
    if not dy.get("is_live"):
        return {"anchor_name": dy.get("anchor_name"), "is_live": False}

    rid = str(dy.get("room_id", ""))
    requested_code = video_quality or "OD"
    rate = DOUYU_RATE_BY_CODE.get(requested_code, "0")
    if requested_code in ("BD30", "BD20") and rate == "8200":
        logger.info("[斗鱼直播] 斗鱼无蓝光30M/20M档位，按蓝光8M(rate=8200)请求")

    flv_data: dict[str, object] = {}
    flv_data_inner: DouyuFlvData = {}
    order = ["0", *DOUYU_RATE_DESC]
    if rate in order:
        pos = order.index(rate)
        attempt_rates = order[pos : pos + 3]  # 请求档 + 最多 2 个更低档
    else:
        attempt_rates = [rate]
    for idx, attempt in enumerate(attempt_rates):
        flv_data = await get_douyu_stream_data(rid, attempt, cookies=cookies, proxy_addr=proxy_addr)
        err_code = flv_data.get("error", 0)
        flv_data_inner = cast(DouyuFlvData, flv_data.get("data") or {})
        if not err_code and flv_data_inner.get("rtmp_live"):
            if idx > 0:
                actual_code = DOUYU_RATE_TO_CODE.get(attempt, requested_code)
                logger.warning(
                    i18n.tr(
                        "[斗鱼直播] 请求档位 {requested_code}(rate={rate}) 失败，降级为 {actual_code}(rate={attempt}) 拉流成功",
                        requested_code=code_to_zh(requested_code),
                        rate=rate,
                        actual_code=code_to_zh(actual_code),
                        attempt=attempt,
                    )
                )
            break
        err_msg = flv_data.get("msg", "")
        err_detail = f", msg={err_msg}" if err_msg else ""
        if idx + 1 < len(attempt_rates):
            logger.warning(
                i18n.tr(
                    "[斗鱼直播] rate={attempt} 拉流失败(error={err_code}{err_detail})，尝试更低档位 rate={next_rate}",
                    attempt=attempt,
                    err_code=err_code,
                    err_detail=err_detail,
                    next_rate=attempt_rates[idx + 1],
                )
            )
        else:
            logger.warning(
                i18n.tr(
                    "[斗鱼直播] rate={attempt} 拉流失败(error={err_code}{err_detail})，已无更低档位",
                    attempt=attempt,
                    err_code=err_code,
                    err_detail=err_detail,
                )
            )
    else:
        logger.error(
            i18n.tr("[斗鱼直播] 全部档位拉流失败(尝试={attempt_rates})，本轮无可用流地址", attempt_rates=attempt_rates)
        )

    rtmp_url = flv_data_inner.get("rtmp_url", "")
    rtmp_live = flv_data_inner.get("rtmp_live", "")
    actual_rate = str(flv_data_inner.get("rate", ""))
    actual_quality = DOUYU_RATE_TO_CODE.get(actual_rate, requested_code)

    result: dict[str, object] = {
        "anchor_name": dy.get("anchor_name"),
        "is_live": True,
        "quality": requested_code,
        "actual_quality": actual_quality,
    }
    if rtmp_live:
        flv_url = f"{rtmp_url}/{rtmp_live}"
        result |= {"flv_url": flv_url, "record_url": flv_url}
        path, _, query = flv_url.partition("?")
        if path.endswith(".flv"):
            result["m3u8_url"] = f"{path[:-4]}.m3u8" + (f"?{query}" if query else "")
    return result


@trace_error_decorator
async def get_yy_stream_url(json_data: dict[str, object]) -> dict[str, object]:
    y = cast(YyStreamUrl, cast(object, json_data))
    anchor_name = y.get("anchor_name", "")
    result: dict[str, object] = {"anchor_name": anchor_name, "is_live": False}
    avp = y.get("avp_info_res")
    if avp:
        stream_line_addr = avp.get("stream_line_addr") or {}
        if not stream_line_addr:
            return result
        cdn_info = list(stream_line_addr.values())[0]
        cdn_detail = cdn_info.get("cdn_info") or {}
        flv_url = cdn_detail.get("url", "")
        result |= {
            "is_live": True,
            "title": y.get("title", ""),
            "quality": "OD",
            "flv_url": flv_url,
            "record_url": flv_url,
        }
    return result


@trace_error_decorator
async def get_bilibili_stream_url(
    json_data: dict[str, object], video_quality: str | None = None, proxy_addr: str | None = None, cookies: str = ""
) -> dict[str, object]:
    b = cast(BilibiliStreamUrl, cast(object, json_data))
    anchor_name = b.get("anchor_name", "")
    if not b.get("live_status"):
        return {"anchor_name": anchor_name, "is_live": False}

    room_url = b.get("room_url", "")
    video_quality_options = {"OD": "10000", "BD": "400", "UHD": "250", "HD": "150", "SD": "80", "LD": "80"}

    requested_code = (video_quality or "OD").upper()
    select_code, _ = get_quality_index(requested_code)
    if requested_code not in QUALITY_LEVEL and not requested_code.isdigit():
        logger.warning(
            i18n.tr(
                "[B站直播] 未知画质代码 {video_quality}，按 {select_code} 请求流地址",
                video_quality=video_quality,
                select_code=select_code,
            )
        )

    select_quality = video_quality_options.get(select_code, "10000")
    play_url_data = await get_bilibili_stream_data(
        room_url, qn=select_quality, platform="web", proxy_addr=proxy_addr, cookies=cookies
    )
    if not play_url_data:
        return {"anchor_name": anchor_name, "is_live": False}
    pd = cast(BilibiliPlayData, cast(object, play_url_data))
    qn_to_code: dict[str, str] = {}
    for code, qn_value in video_quality_options.items():
        known = qn_to_code.get(qn_value)
        if known is None or QUALITY_LEVEL[code] < QUALITY_LEVEL[known]:
            qn_to_code[qn_value] = code
    actual_quality = qn_to_code.get(str(pd.get("current_qn", "")), video_quality)
    accept_qn = pd.get("accept_qn") or []
    available_qualities = [qn_to_code[str(q)] for q in accept_qn if str(q) in qn_to_code] or None
    return {
        "anchor_name": anchor_name,
        "is_live": True,
        "title": b.get("title", ""),
        "quality": video_quality,
        "actual_quality": actual_quality,
        "available_qualities": available_qualities,
        "record_url": pd.get("url", ""),
    }


@trace_error_decorator
async def get_netease_stream_url(json_data: dict[str, object], video_quality: str | None = None) -> dict[str, object]:
    n = cast(NeteaseStreamUrl, cast(object, json_data))
    if not n.get("is_live"):
        return json_data

    m3u8_url = n.get("m3u8_url", "")
    flv_url: str | None = None
    actual_quality: str | None = None
    available_qualities: list[str] | None = None
    stream_list_data = n.get("stream_list")
    if stream_list_data:
        stream_list = stream_list_data.get("resolution") or {}
        order = ["blueray", "ultra", "high", "standard"]
        sorted_keys = [key for key in order if key in stream_list]
        if not sorted_keys:
            return json_data
        video_quality, quality_index = get_quality_index(video_quality)
        idx = min(quality_index, len(sorted_keys) - 1)
        selected_quality = sorted_keys[idx]
        actual_quality = NETEASE_QUALITY_MAP.get(selected_quality, video_quality)
        available_qualities = [NETEASE_QUALITY_MAP.get(k, k.upper()) for k in sorted_keys]
        flv_url_list = stream_list[selected_quality].get("cdn") or {}
        selected_cdn = next(iter(flv_url_list), None)
        flv_url = flv_url_list[selected_cdn] if selected_cdn is not None else ""

    return {
        "is_live": True,
        "anchor_name": n.get("anchor_name", ""),
        "title": n.get("title", ""),
        "quality": video_quality,
        "actual_quality": actual_quality,
        "available_qualities": available_qualities,
        "m3u8_url": m3u8_url,
        "flv_url": flv_url,
        "record_url": flv_url or m3u8_url,
    }


@trace_error_decorator
async def get_stream_url(
    json_data: dict[str, object],
    video_quality: str | None = None,
    url_type: str = "m3u8",
    spec: bool = False,
    hls_extra_key: str | int | None = None,
    flv_extra_key: str | int | None = None,
) -> dict[str, object]:
    g = cast(GenericStreamUrl, cast(object, json_data))
    if not g.get("is_live"):
        return json_data

    play_url_list: list[str | dict[str, str]] = g.get("play_url_list") or []
    if not play_url_list:
        return json_data
    play_url_list = cast(list[str | dict[str, str]], _pad_list(play_url_list))

    video_quality, selected_quality = get_quality_index(video_quality)
    data: dict[str, object] = {"anchor_name": g.get("anchor_name", ""), "is_live": True}

    def get_url(key: str | int | None) -> str:
        play_url = play_url_list[selected_quality]
        if isinstance(play_url, str):
            return play_url
        if key:
            return str(play_url.get(cast(str, key), "") or "")
        for probe_key in _PLAY_URL_KEY_ORDER:
            value = play_url.get(probe_key, "")
            if value:
                return str(value)
        return ""

    if url_type == "all":
        m3u8_url = get_url(hls_extra_key)
        flv_url = get_url(flv_extra_key)
        data |= {
            "m3u8_url": g.get("m3u8_url", "") if spec else m3u8_url,
            "flv_url": g.get("flv_url", "") if spec else flv_url,
            "record_url": m3u8_url,
        }
    elif url_type == "m3u8":
        m3u8_url = get_url(hls_extra_key)
        data |= {"m3u8_url": g.get("m3u8_url", "") if spec else m3u8_url, "record_url": m3u8_url}
    else:
        flv_url = get_url(flv_extra_key)
        data |= {"flv_url": flv_url, "record_url": flv_url}
    data["title"] = g.get("title", "")
    data["quality"] = video_quality
    return data
