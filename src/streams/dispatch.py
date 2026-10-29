"""直播间 URL 准入与平台解析分派。"""

import asyncio
import configparser
import os
import random
import re
import uuid
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit

from loguru import logger

from src.config import files as config_files
from src.monitors.registry import follow_monitor
from src.network.http import request
from src.recording import state
from src.streams import spider, stream
from src.streams.selection import _douyin_rate_limit
from src.support import i18n, utils

PLATFORM_HOST = [
    "live.douyin.com",
    "v.douyin.com",
    "www.douyin.com",
    "live.kuaishou.com",
    "www.huya.com",
    "hy.fan",
    "www.douyu.com",
    "m.douyu.com",
    "www.yy.com",
    "live.bilibili.com",
    "b23.tv",
    "www.xiaohongshu.com",
    "xhslink.com",
    "www.bigo.tv",
    "slink.bigovideo.tv",
    "app.blued.cn",
    "cc.163.com",
    "qiandurebo.com",
    "fm.missevan.com",
    "look.163.com",
    "twitcasting.tv",
    "live.baidu.com",
    "weibo.com",
    "fanxing.kugou.com",
    "fanxing2.kugou.com",
    "mfanxing.kugou.com",
    "www.huajiao.com",
    "www.7u66.com",
    "wap.7u66.com",
    "live.acfun.cn",
    "m.acfun.cn",
    "live.tlclw.com",
    "wap.tlclw.com",
    "live.ybw1666.com",
    "wap.ybw1666.com",
    "www.inke.cn",
    "www.zhihu.com",
    "www.haixiutv.com",
    "h5webcdnp.vvxqiu.com",
    "17.live",
    "www.lang.live",
    "m.pp.weimipopo.com",
    "v.6.cn",
    "m.6.cn",
    "www.lehaitv.com",
    "h.catshow168.com",
    "e.tb.cn",
    "m.tb.cn",
    "tbzb.taobao.com",
    "huodong.m.taobao.com",
    "3.cn",
    "eco.m.jd.com",
    "www.miguvideo.com",
    "m.miguvideo.com",
    "show.lailianjie.com",
    "www.imkktv.com",
    "www.picarto.tv",
    "www.tiktok.com",
    "play.sooplive.co.kr",
    "m.sooplive.co.kr",
    "www.sooplive.com",
    "m.sooplive.com",
    "www.pandalive.co.kr",
    "www.winktv.co.kr",
    "www.flextv.co.kr",
    "www.ttinglive.com",
    "www.popkontv.com",
    "www.twitch.tv",
    "www.liveme.com",
    "www.showroom-live.com",
    "chzzk.naver.com",
    "m.chzzk.naver.com",
    "live.shopee.",
    ".shp.ee",
    "www.youtube.com",
    "youtu.be",
    "www.faceit.com",
]

OVERSEAS_PLATFORM_HOST = [
    "www.tiktok.com",
    "play.sooplive.co.kr",
    "m.sooplive.co.kr",
    "www.sooplive.com",
    "m.sooplive.com",
    "www.pandalive.co.kr",
    "www.winktv.co.kr",
    "www.flextv.co.kr",
    "www.ttinglive.com",
    "www.popkontv.com",
    "www.twitch.tv",
    "www.liveme.com",
    "www.showroom-live.com",
    "chzzk.naver.com",
    "m.chzzk.naver.com",
    "live.shopee.",
    ".shp.ee",
    "www.youtube.com",
    "youtu.be",
    "www.faceit.com",
]

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


class _PlatformResolveContext:
    def __init__(self, record_url: str, proxy_address: str | None, record_quality: str) -> None:
        self.record_url = record_url
        self.proxy_address = proxy_address
        self.record_quality = record_quality
        self.platform = "未知平台"
        self.port_info: dict[str, Any] = {}
        self.record_danmaku_args: dict[str, Any] | None = None
        self.new_record_url = ""
        self.unrecognized = False


def _match_host(*fragments: str) -> Callable[[str], bool]:
    """平台匹配器工厂：多片段为 or。"""

    def _m(url: str) -> bool:
        """只匹配 urlsplit 解析出的 host，不匹配整串子串：否则。"""
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        if not host:
            return False
        normalized = f"{(parts.scheme or '').lower()}://{host}/"
        return any(frag.lower() in normalized for frag in fragments)

    return _m


def _stream_path_suffix(url: str) -> str:
    """取 URL 路径部分的小写扩展名（含点）；无扩展名或畸形地址返回空串。"""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    return os.path.splitext(path)[1].lower()


def _match_stream_suffix(*suffixes: str) -> Callable[[str], bool]:
    """自定义流地址匹配器：按小写**路径扩展名**精确比对（平台/用户手填地址可能是 .M3U8 / .FLV 大写形态）。"""
    lowered = tuple(s.lower() for s in suffixes)

    def _m(url: str) -> bool:
        return _stream_path_suffix(url) in lowered

    return _m


def _resolve_douyin_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "抖音直播"
    _, follow_room = follow_monitor.get_room(ctx.record_url)
    if follow_room is None:
        _douyin_rate_limit()
    with state.semaphore:
        if follow_room is not None:
            json_data = follow_room
        elif "v.douyin.com" not in ctx.record_url and "/user/" not in ctx.record_url:
            json_data = asyncio.run(
                spider.get_douyin_web_stream_data(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.dy_cookie
                )
            )
        else:
            json_data = asyncio.run(
                spider.get_douyin_app_stream_data(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.dy_cookie
                )
            )
        _douyin_room_id = ""
        if isinstance(json_data, dict):
            _douyin_room_id = str(json_data.get("id_str") or json_data.get("id") or "")
        if _douyin_room_id:
            ctx.record_danmaku_args = {
                "room_id": _douyin_room_id,
                "user_id": str(random.randint(10**11, 10**12 - 1)),
                "cookie": state.dy_cookie or "",
            }
        ctx.port_info = asyncio.run(stream.get_douyin_stream_url(json_data, ctx.record_quality, ctx.proxy_address))


def _resolve_tiktok_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "TikTok直播"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            tiktok_data = asyncio.run(
                spider.get_tiktok_stream_data(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.tiktok_cookie
                )
            )
            json_data = tiktok_data if tiktok_data is not None else cast(dict[str, object], {"is_live": False})
            ctx.port_info = asyncio.run(stream.get_tiktok_stream_url(json_data, ctx.record_quality, ctx.proxy_address))
        else:
            logger.error("错误信息: 网络异常，请检查网络是否能正常访问TikTok平台")


def _resolve_live_kuaishou_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "快手直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_kuaishou_stream_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.ks_cookie)
        )
        ctx.port_info = asyncio.run(stream.get_kuaishou_stream_url(json_data, ctx.record_quality))


def _resolve_huya_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "虎牙直播"
    with state.semaphore:
        if ctx.record_quality not in ["OD", "BD", "UHD"]:
            json_data = asyncio.run(
                spider.get_huya_stream_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.hy_cookie)
            )
            ctx.port_info = asyncio.run(stream.get_huya_stream_url(json_data, ctx.record_quality))
            try:
                _huya_data0 = cast(
                    dict[str, object],
                    (cast(list[object], (json_data or {}).get("data") or [{}]))[0],
                )
                _gstream = cast(
                    dict[str, object],
                    (cast(list[object], _huya_data0.get("gameStreamInfoList") or [{}]))[0],
                )
                _glive = cast(dict[str, object], _huya_data0.get("gameLiveInfo") or {})
                _ayyuid = cast(Any, _glive.get("yyid"))
                _topSid = cast(Any, _gstream.get("lChannelId"))
                _subSid = cast(Any, _gstream.get("lSubChannelId"))
                if _ayyuid is not None and _topSid is not None and _subSid is not None:
                    ctx.record_danmaku_args = {
                        "ayyuid": int(_ayyuid),
                        "topSid": int(_topSid),
                        "subSid": int(_subSid),
                    }
            except Exception as e:
                logger.warning(i18n.tr("[虎牙直播]弹幕参数提取失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        else:
            ctx.port_info = asyncio.run(
                spider.get_huya_app_stream_url(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.hy_cookie
                )
            )
            try:
                _ayyuid = cast(Any, (ctx.port_info or {}).get("yyid"))
                _topSid = cast(Any, (ctx.port_info or {}).get("lChannelId"))
                _subSid = cast(Any, (ctx.port_info or {}).get("lSubChannelId"))
                if _ayyuid is not None and _topSid is not None and _subSid is not None:
                    ctx.record_danmaku_args = {
                        "ayyuid": int(_ayyuid),
                        "topSid": int(_topSid),
                        "subSid": int(_subSid),
                    }
                else:
                    logger.debug(
                        i18n.tr(
                            "[虎牙直播]OD/BD/UHD app路径弹幕参数缺失，跳过弹幕: yyid={_ayyuid}, lChannelId={_topSid}, lSubChannelId={_subSid}",
                            _ayyuid=_ayyuid,
                            _topSid=_topSid,
                            _subSid=_subSid,
                        )
                    )
            except Exception as e:
                logger.warning(
                    i18n.tr(
                        "[虎牙直播]OD/BD/UHD app路径弹幕参数提取失败: {type_name}: {e}",
                        type_name=type(e).__name__,
                        e=e,
                    )
                )


_HY_FAN_ROOM_CACHE: dict[str, str] = {}


def _short_link_path_segment(record_url: str) -> str:
    try:
        path = urlsplit(record_url).path
    except ValueError:
        raise ValueError(i18n.tr("URL 无法解析"))
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        raise ValueError(i18n.tr("路径为空，缺少房间号"))
    if len(segments) > 1:
        raise ValueError(i18n.tr("路径含多级目录，不是标准房间链接"))
    segment = segments[0]
    if not (segment.isascii() and segment.isalnum()):
        raise ValueError(i18n.tr("房间号含非法字符"))
    return segment


def _huya_room_segment_from_landing(final_url: str) -> str:
    try:
        parts = urlsplit(final_url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host != "huya.com" and not host.endswith(".huya.com"):
        return ""
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) != 1:
        return ""
    segment = segments[0]
    if not (segment.isascii() and segment.isalnum()):
        return ""
    return segment


async def _resolve_hy_fan_room_segment(record_url: str, proxy_addr: str | None) -> str:
    final_url = await request(
        url=record_url,
        proxy_addr=proxy_addr,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        },
        redirect_url=True,
    )
    if not isinstance(final_url, str) or not final_url:
        raise ValueError(i18n.tr("未获取到跳转地址"))
    segment = _huya_room_segment_from_landing(final_url)
    if not segment:
        raise ValueError(i18n.tr("跳转地址不是虎牙直播间"))
    return segment


def _resolve_hy_fan(ctx: _PlatformResolveContext) -> None:
    try:
        segment = _short_link_path_segment(ctx.record_url)
    except ValueError as err:
        logger.error(
            i18n.tr(
                "虎牙移动端链接缺少有效房间号，本轮跳过: {record_url} - {reason}",
                record_url=utils.mask_credentials(ctx.record_url),
                reason=str(err),
            )
        )
        ctx.unrecognized = True
        return
    if segment.isascii() and segment.isdigit():
        room_segment = segment
    else:
        room_segment = _HY_FAN_ROOM_CACHE.get(segment, "")
        if not room_segment:
            try:
                room_segment = asyncio.run(_resolve_hy_fan_room_segment(ctx.record_url, ctx.proxy_address))
            except Exception as err:
                logger.error(
                    i18n.tr(
                        "虎牙移动端短链解析失败，本轮跳过: {record_url} - {reason}",
                        record_url=utils.mask_credentials(ctx.record_url),
                        reason=f"{type(err).__name__}: {err}",
                    )
                )
                ctx.unrecognized = True
                return
            _HY_FAN_ROOM_CACHE[segment] = room_segment
    ctx.record_url = f"https://www.huya.com/{room_segment}"
    _resolve_huya_com(ctx)


def _resolve_douyu_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "斗鱼直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_douyu_info_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.douyu_cookie)
        )
        _douyu_rid = str(json_data.get("room_id") or "") if isinstance(json_data, dict) else ""
        if _douyu_rid:
            ctx.record_danmaku_args = {"room_id": _douyu_rid}
        ctx.port_info = asyncio.run(
            stream.get_douyu_stream_url(
                json_data,
                video_quality=ctx.record_quality,
                cookies=state.douyu_cookie,
                proxy_addr=ctx.proxy_address,
            )
        )


def _resolve_yy_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "YY直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_yy_stream_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.yy_cookie)
        )
        ctx.port_info = asyncio.run(stream.get_yy_stream_url(json_data))


def _resolve_live_bilibili_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "B站直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_bilibili_room_info(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.bili_cookie)
        )
        ctx.port_info = asyncio.run(
            stream.get_bilibili_stream_url(
                json_data,
                video_quality=ctx.record_quality,
                cookies=state.bili_cookie,
                proxy_addr=ctx.proxy_address,
            )
        )
        if ctx.port_info.get("is_live", False):
            try:
                ctx.record_danmaku_args = asyncio.run(
                    spider.get_bilibili_danmaku_info(
                        url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.bili_cookie
                    )
                )
            except Exception as e:
                logger.warning(i18n.tr("[B站直播]弹幕信息获取失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
                ctx.record_danmaku_args = None


_B23_TV_ROOM_CACHE: dict[str, str] = {}


def _bilibili_room_id_from_landing(final_url: str) -> str:
    try:
        parts = urlsplit(final_url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host != "live.bilibili.com" and not host.endswith(".live.bilibili.com"):
        return ""
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) == 1:
        room_id = segments[0]
    elif len(segments) == 2 and segments[0] == "h5":
        room_id = segments[1]
    else:
        return ""
    if not (room_id.isascii() and room_id.isdigit()):
        return ""
    return room_id


async def _resolve_b23_tv_room_segment(record_url: str, proxy_addr: str | None) -> str:
    final_url = await request(
        url=record_url,
        proxy_addr=proxy_addr,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
        },
        redirect_url=True,
    )
    if not isinstance(final_url, str) or not final_url:
        raise ValueError(i18n.tr("未获取到跳转地址"))
    room_id = _bilibili_room_id_from_landing(final_url)
    if not room_id:
        raise ValueError(i18n.tr("跳转地址不是B站直播间"))
    return room_id


def _resolve_b23_tv(ctx: _PlatformResolveContext) -> None:
    try:
        segment = _short_link_path_segment(ctx.record_url)
    except ValueError as err:
        logger.error(
            i18n.tr(
                "B站移动端链接缺少有效房间号，本轮跳过: {record_url} - {reason}",
                record_url=utils.mask_credentials(ctx.record_url),
                reason=str(err),
            )
        )
        ctx.unrecognized = True
        return
    if segment.isascii() and segment.isdigit():
        room_id = segment
    else:
        room_id = _B23_TV_ROOM_CACHE.get(segment, "")
        if not room_id:
            try:
                room_id = asyncio.run(_resolve_b23_tv_room_segment(ctx.record_url, ctx.proxy_address))
            except Exception as err:
                logger.error(
                    i18n.tr(
                        "B站移动端短链解析失败，本轮跳过: {record_url} - {reason}",
                        record_url=utils.mask_credentials(ctx.record_url),
                        reason=f"{type(err).__name__}: {err}",
                    )
                )
                ctx.unrecognized = True
                return
            _B23_TV_ROOM_CACHE[segment] = room_id
    ctx.record_url = f"https://live.bilibili.com/{room_id}"
    _resolve_live_bilibili_com(ctx)


def _resolve_xhslink_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "小红书直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_xhs_stream_url(ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.xhs_cookie)
        )
    _error = str(ctx.port_info.get("error") or "")
    if _error:
        logger.warning(
            i18n.tr(
                "小红书链接解析失败，本轮按未识别处理: {record_url} - {code}",
                record_url=utils.mask_credentials(ctx.record_url),
                code=_error,
            )
        )
        ctx.unrecognized = True
    _normalized = str(ctx.port_info.get("normalized_url") or "")
    if _normalized and _normalized != ctx.record_url:
        ctx.new_record_url = _normalized


def _resolve_bigo_tv(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "bigo"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_bigo_stream_url(ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.bigo_cookie)
        )


def _resolve_app_blued_cn(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "blued"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_blued_stream_url(ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.blued_cookie)
        )


def _resolve_sooplive_co_kr(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "SOOP(原AfreecaTV)"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            json_data = asyncio.run(
                spider.get_sooplive_stream_data(
                    url=ctx.record_url,
                    proxy_addr=ctx.proxy_address,
                    cookies=state.sooplive_cookie,
                    username=state.sooplive_username,
                    password=state.sooplive_password,
                )
            )
            if json_data and json_data.get("new_cookies"):
                try:
                    with state.file_update_lock:  # 与主循环 config.read/其他写入方互斥，防止半写
                        config_files.update_config(
                            state.config_file,
                            "Cookie",
                            "sooplive_cookie",
                            cast(str, json_data["new_cookies"]),
                        )
                except (configparser.Error, OSError) as e:
                    logger.warning(
                        i18n.tr(
                            "凭据回写失败（不影响本轮录制）: {type_name}: {e}",
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )
            ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))
        else:
            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问SOOP(原AfreecaTV)平台")


def _resolve_cc_163_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "网易CC直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_netease_stream_data(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.netease_cookie
            )
        )
        ctx.port_info = asyncio.run(stream.get_netease_stream_url(json_data, ctx.record_quality))


def _resolve_qiandurebo_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "千度热播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_qiandurebo_stream_data(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.qiandurebo_cookie
            )
        )


def _resolve_pandalive_co_kr(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "PandaTV"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            json_data = asyncio.run(
                spider.get_pandatv_stream_data(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.pandatv_cookie
                )
            )
            ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))
        else:
            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问PandaTV直播平台")


def _resolve_fm_missevan_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "猫耳FM直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_maoerfm_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.maoerfm_cookie
            )
        )


def _resolve_winktv_co_kr(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "WinkTV"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            json_data = asyncio.run(
                spider.get_winktv_stream_data(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.winktv_cookie
                )
            )
            ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))
        else:
            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问WinkTV直播平台")


def _resolve_flextv_co_kr(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "TTingLive(原Flextv)"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            json_data = asyncio.run(
                spider.get_flextv_stream_data(
                    url=ctx.record_url,
                    proxy_addr=ctx.proxy_address,
                    cookies=state.flextv_cookie,
                    username=state.flextv_username,
                    password=state.flextv_password,
                )
            )
            if json_data and json_data.get("new_cookies"):
                try:
                    with state.file_update_lock:  # 与主循环 config.read/其他写入方互斥，防止半写
                        config_files.update_config(
                            state.config_file, "Cookie", "flextv_cookie", cast(str, json_data["new_cookies"])
                        )
                except (configparser.Error, OSError) as e:
                    logger.warning(
                        i18n.tr(
                            "凭据回写失败（不影响本轮录制）: {type_name}: {e}",
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )
            if "play_url_list" in json_data:
                ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))
            else:
                ctx.port_info = json_data
        else:
            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问TTingLive(原Flextv)直播平台")


def _resolve_look_163_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "Look直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_looklive_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.look_cookie)
        )


def _resolve_popkontv_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "PopkonTV"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            ctx.port_info = asyncio.run(
                spider.get_popkontv_stream_url(
                    url=ctx.record_url,
                    proxy_addr=ctx.proxy_address,
                    access_token=state.popkontv_access_token,
                    username=state.popkontv_username,
                    password=state.popkontv_password,
                    partner_code=state.popkontv_partner_code,
                )
            )
            if ctx.port_info and ctx.port_info.get("new_token"):
                try:
                    with state.file_update_lock:  # 与主循环 config.read/其他写入方互斥，防止半写
                        config_files.update_config(
                            file_path=state.config_file,
                            section="Authorization",
                            key="popkontv_token",
                            new_value=cast(str, ctx.port_info["new_token"]),
                        )
                except (configparser.Error, OSError) as e:
                    logger.warning(
                        i18n.tr(
                            "凭据回写失败（不影响本轮录制）: {type_name}: {e}",
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )

        else:
            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问PopkonTV直播平台")


def _resolve_twitcasting_tv(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "TwitCasting"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_twitcasting_stream_url(
                url=ctx.record_url,
                proxy_addr=ctx.proxy_address,
                cookies=state.twitcasting_cookie,
                account_type=state.twitcasting_account_type,
                username=state.twitcasting_username,
                password=state.twitcasting_password,
            )
        )
        ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=False))

        if json_data and json_data.get("new_cookies"):
            try:
                with state.file_update_lock:  # 与主循环 config.read/其他写入方互斥，防止半写
                    config_files.update_config(
                        file_path=state.config_file,
                        section="Cookie",
                        key="twitcasting_cookie",
                        new_value=cast(str, json_data["new_cookies"]),
                    )
            except (configparser.Error, OSError) as e:
                logger.warning(
                    i18n.tr(
                        "凭据回写失败（不影响本轮录制）: {type_name}: {e}",
                        type_name=type(e).__name__,
                        e=e,
                    )
                )


def _resolve_live_baidu_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "百度直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_baidu_stream_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.baidu_cookie)
        )
        ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality))


def _resolve_weibo_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "微博直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_weibo_stream_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.weibo_cookie)
        )
        ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, hls_extra_key="m3u8_url"))


def _resolve_kugou_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "酷狗直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_kugou_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.kugou_cookie)
        )


def _resolve_twitch_tv(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "TwitchTV"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            json_data = asyncio.run(
                spider.get_twitchtv_stream_data(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.twitch_cookie
                )
            )
            ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))
            try:
                _twitch_channel = ctx.record_url.split("?")[0].rstrip("/").split("/")[-1].lower()
                if _twitch_channel:
                    _danmaku_extra = {}
                    if ctx.proxy_address:
                        _danmaku_extra["proxy"] = ctx.proxy_address
                    ctx.record_danmaku_args = {"channel": _twitch_channel, **_danmaku_extra}
            except Exception as e:
                logger.warning(
                    i18n.tr(
                        "[TwitchTV]弹幕 channel 提取失败: {type_name}: {e}",
                        type_name=type(e).__name__,
                        e=e,
                    )
                )
        else:
            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问TwitchTV直播平台")


def _resolve_liveme_com(ctx: _PlatformResolveContext) -> None:
    if state.global_proxy or ctx.proxy_address:
        ctx.platform = "LiveMe"
        with state.semaphore:
            ctx.port_info = asyncio.run(
                spider.get_liveme_stream_url(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.liveme_cookie
                )
            )
    else:
        logger.error("错误信息: 网络异常，请检查本网络是否能正常访问LiveMe直播平台")


def _resolve_huajiao_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "花椒直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_huajiao_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.huajiao_cookie
            )
        )


def _resolve_n7u66_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "流星直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_liuxing_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.liuxing_cookie
            )
        )


def _resolve_showroom_live_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "ShowRoom"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_showroom_stream_data(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.showroom_cookie
            )
        )
        ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))


def _resolve_live_acfun_cn(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "Acfun"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_acfun_stream_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.acfun_cookie)
        )
        ctx.port_info = asyncio.run(
            stream.get_stream_url(json_data, ctx.record_quality, url_type="flv", flv_extra_key="url")
        )


def _resolve_live_tlclw_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "畅聊直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_changliao_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.changliao_cookie
            )
        )


def _resolve_ybw1666_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "音播直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_yinbo_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.yinbo_cookie)
        )


def _resolve_inke_cn(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "映客直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_yingke_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.yingke_cookie)
        )


def _resolve_zhihu_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "知乎直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_zhihu_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.zhihu_cookie)
        )


def _resolve_chzzk_naver_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "CHZZK"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_chzzk_stream_data(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.chzzk_cookie)
        )
        ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))


def _resolve_haixiutv_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "嗨秀直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_haixiu_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.haixiu_cookie)
        )


def _resolve_vvxqiu_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "VV星球"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_vvxqiu_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.vvxqiu_cookie)
        )


def _resolve_n17_live(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "17Live"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_17live_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.yiqilive_cookie
            )
        )


def _resolve_lang_live(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "浪Live"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_langlive_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.langlive_cookie
            )
        )


def _resolve_m_pp_weimipopo_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "飘飘直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_pplive_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.pplive_cookie)
        )


def _resolve_n6_cn(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "六间房直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_6room_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.six_room_cookie)
        )


def _resolve_lehaitv_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "乐嗨直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_haixiu_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.lehaitv_cookie)
        )


def _resolve_h_catshow168_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "花猫直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_pplive_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.huamao_cookie)
        )


_SHOPEE_UID_SAFE_RE = re.compile(r"^[A-Za-z0-9_\-&=%./:]+$")


def _build_shopee_record_url(record_url: str, uid: str) -> str:
    base = record_url.split("?")[0]
    if not uid or not _SHOPEE_UID_SAFE_RE.match(uid) or "\n" in uid or "\r" in uid:
        logger.warning(
            i18n.tr(
                "Shopee 返回的 uid 含不合法字符，已跳过配置回写: {record_url}",
                record_url=utils.mask_credentials(record_url),
            )
        )
        return ""
    candidate = base + "?" + uid
    try:
        if (urlsplit(candidate).hostname or "") != (urlsplit(base).hostname or ""):
            logger.warning(
                i18n.tr(
                    "Shopee 返回的 uid 会改变目标主机，已跳过配置回写: {record_url}",
                    record_url=utils.mask_credentials(record_url),
                )
            )
            return ""
    except ValueError:
        return ""
    return candidate


def _resolve_live_shopee(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "shopee"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_shopee_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.shopee_cookie)
        )
        if ctx.port_info.get("uid"):
            ctx.new_record_url = _build_shopee_record_url(ctx.record_url, str(ctx.port_info["uid"]))


def _resolve_youtube_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "YouTube"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_youtube_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.youtube_cookie
            )
        )
        ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))


def _resolve_tb_cn(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "淘宝直播"
    with state.semaphore:
        json_data = asyncio.run(
            spider.get_taobao_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.taobao_cookie)
        )
        ctx.port_info = asyncio.run(
            stream.get_stream_url(
                json_data,
                ctx.record_quality,
                url_type="all",
                hls_extra_key="hlsUrl",
                flv_extra_key="flvUrl",
            )
        )


def _resolve_n3_cn(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "京东直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_jd_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.jd_cookie)
        )


def _resolve_faceit_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "faceit"
    with state.semaphore:
        if state.global_proxy or ctx.proxy_address:
            json_data = asyncio.run(
                spider.get_faceit_stream_data(
                    url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.faceit_cookie
                )
            )
            ctx.port_info = asyncio.run(stream.get_stream_url(json_data, ctx.record_quality, spec=True))
        else:
            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问faceit直播平台")


def _resolve_miguvideo_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "咪咕直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_migu_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.migu_cookie)
        )


def _resolve_show_lailianjie_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "连接直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_lianjie_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.lianjie_cookie
            )
        )


def _resolve_imkktv_com(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "来秀直播"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_laixiu_stream_url(url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.laixiu_cookie)
        )


def _resolve_picarto_tv(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "Picarto"
    with state.semaphore:
        ctx.port_info = asyncio.run(
            spider.get_picarto_stream_url(
                url=ctx.record_url, proxy_addr=ctx.proxy_address, cookies=state.picarto_cookie
            )
        )


def _resolve_custom_stream(ctx: _PlatformResolveContext) -> None:
    ctx.platform = "自定义录制直播"
    ctx.port_info = {
        "anchor_name": ctx.platform + "_" + str(uuid.uuid4())[:8],
        "is_live": True,
        "record_url": ctx.record_url,
    }
    if _stream_path_suffix(ctx.record_url) == ".flv":
        ctx.port_info["flv_url"] = ctx.record_url
    else:
        ctx.port_info["m3u8_url"] = ctx.record_url


def _resolve_unrecognized(ctx: _PlatformResolveContext) -> None:
    record_url = ctx.record_url
    logger.error(
        i18n.tr(
            "无法识别的直播地址，本轮跳过: {record_url}",
            record_url=utils.mask_credentials(record_url),
        )
    )
    ctx.unrecognized = True


_PLATFORM_RESOLVERS: tuple[tuple[Callable[[str], bool], Callable[[_PlatformResolveContext], None]], ...] = (
    (_match_host("douyin.com/"), _resolve_douyin_com),
    (_match_host("www.tiktok.com/"), _resolve_tiktok_com),
    (_match_host("live.kuaishou.com/"), _resolve_live_kuaishou_com),
    (_match_host("www.huya.com/"), _resolve_huya_com),
    (_match_host("hy.fan/"), _resolve_hy_fan),
    (_match_host("www.douyu.com/", "m.douyu.com/"), _resolve_douyu_com),
    (_match_host("www.yy.com/"), _resolve_yy_com),
    (_match_host("live.bilibili.com/"), _resolve_live_bilibili_com),
    (_match_host("b23.tv/"), _resolve_b23_tv),
    (_match_host("xhslink.com/", "www.xiaohongshu.com/"), _resolve_xhslink_com),
    (_match_host("www.bigo.tv/", "slink.bigovideo.tv/"), _resolve_bigo_tv),
    (_match_host("app.blued.cn/"), _resolve_app_blued_cn),
    (_match_host("sooplive.co.kr/", "sooplive.com/"), _resolve_sooplive_co_kr),
    (_match_host("cc.163.com/"), _resolve_cc_163_com),
    (_match_host("qiandurebo.com/"), _resolve_qiandurebo_com),
    (_match_host("www.pandalive.co.kr/", "www.plive.kr/"), _resolve_pandalive_co_kr),
    (_match_host("fm.missevan.com/"), _resolve_fm_missevan_com),
    (_match_host("www.winktv.co.kr/"), _resolve_winktv_co_kr),
    (_match_host("www.flextv.co.kr/", "www.ttinglive.com/"), _resolve_flextv_co_kr),
    (_match_host("look.163.com/"), _resolve_look_163_com),
    (_match_host("www.popkontv.com/"), _resolve_popkontv_com),
    (_match_host("twitcasting.tv/"), _resolve_twitcasting_tv),
    (_match_host("live.baidu.com/"), _resolve_live_baidu_com),
    (_match_host("weibo.com/"), _resolve_weibo_com),
    (_match_host("kugou.com/"), _resolve_kugou_com),
    (_match_host("www.twitch.tv/"), _resolve_twitch_tv),
    (_match_host("www.liveme.com/"), _resolve_liveme_com),
    (_match_host("www.huajiao.com/"), _resolve_huajiao_com),
    (_match_host("7u66.com/"), _resolve_n7u66_com),
    (_match_host("showroom-live.com/"), _resolve_showroom_live_com),
    (_match_host("live.acfun.cn/", "m.acfun.cn/"), _resolve_live_acfun_cn),
    (_match_host("tlclw.com/"), _resolve_live_tlclw_com),
    (_match_host("ybw1666.com/"), _resolve_ybw1666_com),
    (_match_host("www.inke.cn/"), _resolve_inke_cn),
    (_match_host("www.zhihu.com/"), _resolve_zhihu_com),
    (_match_host("chzzk.naver.com/"), _resolve_chzzk_naver_com),
    (_match_host("www.haixiutv.com/"), _resolve_haixiutv_com),
    (_match_host("vvxqiu.com/"), _resolve_vvxqiu_com),
    (_match_host("17.live/"), _resolve_n17_live),
    (_match_host("www.lang.live/"), _resolve_lang_live),
    (_match_host("m.pp.weimipopo.com/"), _resolve_m_pp_weimipopo_com),
    (_match_host(".6.cn/"), _resolve_n6_cn),
    (_match_host("lehaitv.com/"), _resolve_lehaitv_com),
    (_match_host("h.catshow168.com/"), _resolve_h_catshow168_com),
    (_match_host("live.shopee", "shp.ee/"), _resolve_live_shopee),
    (_match_host("www.youtube.com/", "youtu.be/"), _resolve_youtube_com),
    (_match_host("tb.cn", "tbzb.taobao.com", "huodong.m.taobao.com"), _resolve_tb_cn),
    (_match_host("3.cn", "m.jd.com"), _resolve_n3_cn),
    (_match_host("faceit.com/"), _resolve_faceit_com),
    (_match_host("www.miguvideo.com", "m.miguvideo.com"), _resolve_miguvideo_com),
    (_match_host("show.lailianjie.com"), _resolve_show_lailianjie_com),
    (_match_host("www.imkktv.com"), _resolve_imkktv_com),
    (_match_host("www.picarto.tv"), _resolve_picarto_tv),
    (_match_stream_suffix(".m3u8", ".flv"), _resolve_custom_stream),
)


def _resolve_platform_stream(
    record_url: str, proxy_address: str | None, record_quality: str
) -> tuple[str, dict[str, Any], dict[str, Any] | None, str] | None:
    """按地址分派到平台解析；无法识别返回 None（调用方延迟重试）。"""
    ctx = _PlatformResolveContext(record_url, proxy_address, record_quality)
    for matcher, handler in _PLATFORM_RESOLVERS:
        if matcher(record_url):
            try:
                handler(ctx)
            except Exception as e:
                logger.warning(
                    i18n.tr(
                        "平台解析或凭据回写失败（本轮按未识别处理）: {record_url} - {type_name}: {e}",
                        record_url=utils.mask_credentials(record_url),
                        type_name=type(e).__name__,
                        e=e,
                    )
                )
                ctx.unrecognized = True
            break
    else:
        _resolve_unrecognized(ctx)
    if ctx.unrecognized:
        return None
    return ctx.platform, ctx.port_info, ctx.record_danmaku_args, ctx.new_record_url
