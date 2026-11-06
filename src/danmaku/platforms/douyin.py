from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any, cast

from src.danmaku.base import DanmakuBase, DanmakuMessage, DanmakuMessageType
from src.danmaku.compression import MAX_DECOMPRESSED_BYTES, decompress_zlib
from src.danmaku.platforms._xbogus import danmaku_signature
from src.network.websocket import WebSocketClient
from src.proto import douyin_pb2
from src.streams.ttwid import get_ttwid, invalidate_ttwid
from src.support import i18n
from src.support.logger import logger

SERVER_URL = "wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/"

_HTTP_200_REFUSAL_PATTERN = re.compile(r"(?:http|status(?:\s+code)?)[^\d]{0,4}\b200\b", re.IGNORECASE)


def _is_http200_handshake_refusal(detail: str) -> bool:
    """判定一条 WebSocketClient 回调文案是否为「握手被服务端以 HTTP 200 拒绝」。"""
    return bool(_HTTP_200_REFUSAL_PATTERN.search(detail or ""))


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
)

METHOD_CHAT = "WebcastChatMessage"
METHOD_ONLINE = "WebcastRoomUserSeqMessage"


def _make_hb_frame() -> bytes:
    """构造抖音心跳/进房帧 PushFrame{payloadType='hb'}，返回序列化 bytes。"""
    frame = douyin_pb2.PushFrame()
    frame.payloadType = "hb"
    return cast(bytes, frame.SerializeToString())


class DouyinDanmaku(DanmakuBase):
    heartbeat_interval = 10.0  # 与 dart heartbeatTime 一致

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._args: dict = {}
        self._ws: WebSocketClient | None = None
        self._ttwid_in_use = False
        self._ttwid_revoked = False

    async def start(self, args: Any) -> None:
        """启动：构造带 XBogus 签名与 cookie 的 WS URL，建立连接；缺 room_id 回调 on_close。"""
        self._args = args if isinstance(args, dict) else {}
        room_id = str(self._args.get("room_id") or "")
        user_id = str(self._args.get("user_id") or "")
        cookie = str(self._args.get("cookie") or "").strip()
        self._ttwid_in_use = False
        self._ttwid_revoked = False
        if not cookie:
            try:
                cookie = await get_ttwid()
            except Exception as e:
                logger.warning(
                    i18n.tr("[抖音弹幕]动态获取 ttwid 失败: {type_name}: {e}", type_name=type(e).__name__, e=e)
                )
                cookie = ""
            if not cookie:
                logger.warning("[抖音弹幕]无可用 cookie/ttwid，WS 握手可能被服务端拒绝")
            else:
                self._ttwid_in_use = True
        if not room_id:
            if self._on_close:
                self._on_close("缺少 room_id")
            return

        ts = int(time.time() * 1000)
        params = [
            ("app_name", "douyin_web"),
            ("version_code", "180800"),
            ("webcast_sdk_version", "1.0.14-beta.0"),
            ("update_version_code", "1.0.14-beta.0"),
            ("compress", "gzip"),
            ("cursor", f"h-1_t-{ts}_r-1_d-1_u-1"),
            ("host", "https://live.douyin.com"),
            ("aid", "6383"),
            ("live_id", "1"),
            ("did_rule", "3"),
            ("debug", "false"),
            ("maxCacheMessageNumber", "20"),
            ("endpoint", "live_pc"),
            ("support_wrds", "1"),
            ("im_path", "/webcast/im/fetch/"),
            ("user_unique_id", user_id),
            ("device_platform", "web"),
            ("cookie_enabled", "true"),
            ("screen_width", "1920"),
            ("screen_height", "1080"),
            ("browser_language", "zh-CN"),
            ("browser_platform", "Win32"),
            ("browser_name", "Mozilla"),
            ("browser_version", DEFAULT_USER_AGENT.replace("Mozilla/", "")),
            ("browser_online", "true"),
            ("tz_name", "Asia/Shanghai"),
            ("identity", "audience"),
            ("room_id", room_id),
            ("heartbeatDuration", "0"),
        ]
        query = urllib.parse.urlencode(params)
        sign = danmaku_signature(room_id, user_id)
        url = f"{SERVER_URL}?{query}&signature={sign}"
        backup_url = url.replace("webcast100-ws-web-lq", "webcast100-ws-web-lf")

        self._ws = WebSocketClient(
            url=url,
            backup_url=backup_url,
            heartbeat_interval=self.heartbeat_interval,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Cookie": cookie,
                "Origin": "https://live.douyin.com",
            },
            on_message=self.decode_message,
            on_ready=self._on_ws_ready,
            on_heartbeat=self.heartbeat,
            on_close=self._handle_close,
            on_reconnect=self._on_reconnect,
        )
        await self._ws.connect()

    def _handle_close(self, reason: str) -> None:
        """连接关闭上报：先做 ttwid 作废判定，再把「房间已关闭」事件原样上抛给采集器。"""
        self._note_ttwid_refusal(reason)
        if self._on_close:
            self._on_close(reason)

    def _on_reconnect(self, reason: str) -> None:
        """断线重连（基类回调，只记 debug、不上报关闭）：重连次数与退避节奏不变，只在每次失败时。"""
        self._note_ttwid_refusal(reason)
        super()._on_reconnect(reason)

    def _note_ttwid_refusal(self, reason: str) -> None:
        """invalidate_ttwid 一并作废，下一轮重新获取，不等 cookie_cache 的 30 分钟 TTL——那是。"""
        if not self._ttwid_in_use or self._ttwid_revoked:
            return
        if _is_http200_handshake_refusal(reason):
            self._ttwid_revoked = True
            invalidate_ttwid()

    def _on_ws_ready(self) -> None:
        """WS 就绪回调：触发 on_ready 并按 dart joinRoom 的做法发送进房 hb 帧。"""
        if self._on_ready:
            self._on_ready()
        self._send_nowait(_make_hb_frame())

    def _send_nowait(self, data: bytes) -> None:
        """非阻塞发送字节数据（仅在 WS 已建立时发送）。"""
        if self._ws is not None:
            self._ws.send_nowait(data)

    async def heartbeat(self) -> None:
        self._send_nowait(_make_hb_frame())

    async def stop(self) -> None:
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()

    def decode_message(self, data: bytes | str) -> None:
        """解析 PushFrame：gzip 解压 payload 为 Response，分发弹幕/在线消息并回 ack。"""
        if isinstance(data, str):
            return
        try:
            frame = douyin_pb2.PushFrame()
            frame.ParseFromString(data)
            payload = decompress_zlib(frame.payload, MAX_DECOMPRESSED_BYTES)
            resp = douyin_pb2.Response()
            resp.ParseFromString(payload)

            if resp.needAck:
                self._send_ack(frame.logId)

            for msg in resp.messagesList:
                method = msg.method
                if method == METHOD_CHAT:
                    self._decode_chat(msg.payload)
                elif method == METHOD_ONLINE:
                    pass  # 在线人数不进 SRT
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[抖音弹幕]帧解析异常: {type_name} head={head}", type_name=type(e).__name__, head=data[:16].hex()
                )
            )

    def _decode_chat(self, payload: bytes) -> None:
        """解析 ChatMessage 的 payload，提取昵称与内容并 emit 弹幕。"""
        try:
            chat = douyin_pb2.ChatMessage()
            chat.ParseFromString(payload)
            content = chat.content or ""
            nick = (chat.user.nickName or "") if chat.HasField("user") else ""
            if not content or not nick:
                return
            self._emit(
                DanmakuMessage(
                    type=DanmakuMessageType.CHAT,
                    user_name=nick,
                    message=content,
                    color="#FFFFFF",
                )
            )
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[抖音弹幕]弹幕解析异常: {type_name} payload={payload}",
                    type_name=type(e).__name__,
                    payload=payload[:16].hex(),
                )
            )

    def _send_ack(self, log_id: int) -> None:
        """对需要确认的帧发送 ack（payloadType='ack' + logId）。"""
        frame = douyin_pb2.PushFrame()
        frame.payloadType = "ack"
        frame.logId = log_id
        self._send_nowait(frame.SerializeToString())
