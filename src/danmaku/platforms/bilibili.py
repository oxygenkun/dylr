from __future__ import annotations

import asyncio
import json
import re
import struct
from typing import Any, Union

from loguru import logger

from src.danmaku.base import DanmakuBase, DanmakuMessage, DanmakuMessageType, spawn_danmaku_task
from src.danmaku.compression import MAX_DECOMPRESSED_BYTES, decompress_zlib
from src.network.websocket import WebSocketClient
from src.support import i18n

HEADER_LEN = 16

_BILI_DANMAKU_ALLOWED_HOST_SUFFIXES = (
    "bilibili.com",
    "bilivideo.com",
    "bilivideo.cn",
    "hdslb.com",
)


def _bili_danmaku_host_allowed(host: str) -> bool:
    name = (host or "").strip().lower()
    if not name or "/" in name or ":" in name:
        return False
    return any(name == suffix or name.endswith("." + suffix) for suffix in _BILI_DANMAKU_ALLOWED_HOST_SUFFIXES)


def _decompress_brotli(data: bytes, limit: int = MAX_DECOMPRESSED_BYTES, chunk: int = 4096) -> bytes:
    """分块处理 Brotli 输入，并限制累计输出。"""
    import brotli

    obj = brotli.Decompressor()
    out = bytearray()
    for i in range(0, len(data), chunk):
        out += obj.process(data[i : i + chunk])
        if len(out) > limit:
            raise ValueError(f"decompressed payload exceeds limit ({limit} bytes)")
    if not obj.is_finished():
        raise ValueError("brotli stream is incomplete")
    return bytes(out)


class BilibiliDanmaku(DanmakuBase):
    heartbeat_interval = 60.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._args: dict = {}
        self._ws: WebSocketClient | None = None
        self._session_ok = False
        self._auth_ok = False
        self._hosts_left = 0
        self._close_reported = False

    def _report_close(self, reason: str) -> None:
        """M-13：on_close 的唯一出口（轮换闸门 + 一次性保护），命名与 src/network/websocket.py::_report_close 对齐。"""
        if self._hosts_left > 0 and not self._stopped:
            self._on_reconnect(reason)
            return
        if self._close_reported:
            return
        self._close_reported = True
        if self._on_close:
            self._on_close(reason)

    async def start(self, args: Any) -> None:
        """启动：解析房间/服务器参数，逐个尝试 host 建立 WebSocket 连接，失败则回调 on_close。"""
        self._args = args if isinstance(args, dict) else {}
        self._close_reported = False
        server_host = self._args.get("server_host", "")
        room_id = self._args.get("room_id")
        if not server_host or not room_id:
            self._report_close("缺少 server_host/room_id")
            return

        hosts = [str(h) for h in (self._args.get("host_list") or []) if h]
        if server_host and server_host not in hosts:
            hosts.insert(0, server_host)
        _allowed: list[str] = []
        for _h in hosts:
            if _bili_danmaku_host_allowed(_h):
                _allowed.append(_h)
            else:
                logger.warning(
                    i18n.tr(
                        "[B站弹幕]跳过非官方域弹幕服务器（可能被劫持，不发送凭据）: {host}",
                        host=_h,
                    )
                )
        hosts = _allowed
        if not hosts:
            self._report_close(i18n.tr("无可用弹幕服务器（host 均不在 B站官方域白名单内）"))
            self._session_ok = False
            return

        cookie = self._args.get("cookie", "")
        for idx, host in enumerate(hosts):
            if self._stopped:
                return
            self._hosts_left = len(hosts) - idx - 1
            backup = hosts[idx + 1] if idx + 1 < len(hosts) else None
            self._ws = WebSocketClient(
                url=f"wss://{host}/sub",
                backup_url=f"wss://{backup}/sub" if backup else None,
                heartbeat_interval=self.heartbeat_interval,
                on_message=self.decode_message,
                on_ready=self._on_ws_ready,
                on_heartbeat=self.heartbeat,
                on_close=self._report_close,
                on_reconnect=self._on_reconnect,
                headers={"cookie": cookie} if cookie else None,
                max_reconnect=2,
                reconnect_interval=3.0,
                connect_timeout=8.0,
            )
            await self._ws.connect()
            if self._session_ok or self._stopped:
                break
        self._hosts_left = 0

    def _on_ws_ready(self) -> None:
        """WS 就绪回调：置进房成功标志、触发 on_ready 并异步发送进房包。"""
        self._session_ok = True
        self._auth_ok = False
        if self._on_ready:
            self._on_ready()
        spawn_danmaku_task(self._join_room())

    async def _join_room(self) -> None:
        """异步发送进房请求（uid/roomid/token 等），加入指定直播间。"""
        if self._ws is None:
            return
        _m = re.search(r"DedeUserID=(\d+)", str(self._args.get("cookie") or ""))
        viewer_uid = int(_m.group(1)) if _m else 0
        join_body = json.dumps(
            {
                "uid": viewer_uid,
                "roomid": int(self._args.get("room_id", 0)),
                "protover": 3,
                "buvid": self._args.get("buvid", ""),
                "platform": "web",
                "type": 2,
                "key": str(self._args.get("token", "")),
            },
            separators=(",", ":"),
        )
        await self._ws.send(self._encode(join_body, action=7))
        spawn_danmaku_task(self._auth_watchdog(self._ws))

    _AUTH_TIMEOUT = 8.0

    async def _auth_watchdog(self, ws: WebSocketClient) -> None:
        """认证看门狗：限时未收到 AUTH_REPLY(code=0) 即按被拒处理——静默拒绝与 code!=0 的软拒绝。"""
        await asyncio.sleep(self._AUTH_TIMEOUT)
        if self._stopped or self._auth_ok:
            return
        if self._ws is not ws:
            return  # 会话已切换 host，旧看门狗作废
        logger.warning(
            i18n.tr(
                "[B站弹幕]进房认证 {AUTH_TIMEOUT} 秒无回应，按被拒处理主动断开",
                AUTH_TIMEOUT=f"{self._AUTH_TIMEOUT:.0f}",
            )
        )
        self._reject_auth()

    def _reject_auth(self) -> None:
        """认证被拒统一处理：置停止标志、关闭连接，并使 spider 侧 buvid 缓存失效（被拒的 buvid 不可。"""
        self._stopped = True
        if self._ws is not None:
            spawn_danmaku_task(self._ws.fail(i18n.tr("进房认证被拒（AUTH_REPLY 非 0 或超时未回应）")))
        try:
            from src.streams import spider

            spider.invalidate_bili_buvid_cache()
        except Exception:
            pass

    async def heartbeat(self) -> None:
        """发送心跳包（action=2），维持 WebSocket 长连接。"""
        if self._ws is not None:
            await self._ws.send(self._encode("", action=2))

    async def stop(self) -> None:
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()

    @staticmethod
    def _encode(msg: str, action: int) -> bytes:
        """将文本按 B站 16 字节大头序帧头封装为发送字节串，返回 bytes。"""
        data = msg.encode("utf-8")
        return struct.pack(">IHHII", len(data) + HEADER_LEN, HEADER_LEN, 0, action, 1) + data

    def decode_message(self, data: Union[bytes, str]) -> None:
        """解析收到的字节流（处理粘包），逐帧解码并分发弹幕/在线消息。"""
        if isinstance(data, str):
            return
        offset = 0
        n = len(data)
        while offset + HEADER_LEN <= n:
            packet_len = struct.unpack_from(">I", data, offset)[0]
            if packet_len < HEADER_LEN or offset + packet_len > n:
                break
            try:
                self._decode_packet(data[offset : offset + packet_len])
            except Exception as e:
                logger.debug(
                    i18n.tr(
                        "[B站弹幕]帧解析异常: {type_name} head={head}",
                        type_name=type(e).__name__,
                        head=data[offset : offset + 16].hex(),
                    )
                )
            offset += packet_len

    def _decode_packet(self, frame: bytes) -> None:
        """解码单帧：按 protover 解压，解析心跳回应/弹幕消息并 emit。"""
        if len(frame) < HEADER_LEN:
            return
        proto_ver = struct.unpack_from(">H", frame, 6)[0]
        operation = struct.unpack_from(">I", frame, 8)[0]
        body = frame[HEADER_LEN:]

        if operation == 3:
            if len(body) >= 4:
                online = struct.unpack_from(">I", body, 0)[0]
                self._emit(
                    DanmakuMessage(
                        type=DanmakuMessageType.ONLINE,
                        user_name="",
                        message="",
                        data=online,
                        color="#FFFFFF",
                    )
                )
            return

        if operation == 8:
            try:
                reply = json.loads(body.decode("utf-8", errors="ignore") or "{}")
            except Exception:
                reply = {}
            code = reply.get("code", -1) if isinstance(reply, dict) else -1
            if code == 0:
                self._auth_ok = True  # 解除认证看门狗
                logger.debug("[B站弹幕]进房认证成功（AUTH_REPLY code=0）")
            else:
                logger.warning(
                    i18n.tr(
                        "[B站弹幕]进房认证失败（AUTH_REPLY code={code}），主动断开: {reply}", code=code, reply=reply
                    )
                )
                self._reject_auth()
            return
        if operation != 5:
            return  # 其他操作码忽略

        try:
            if proto_ver == 2:
                payload = decompress_zlib(body, MAX_DECOMPRESSED_BYTES, wbits=15)
            elif proto_ver == 3:
                payload = _decompress_brotli(body, MAX_DECOMPRESSED_BYTES)
            else:
                payload = body
        except Exception:
            return  # 解压失败或超限丢弃

        for item in re.split(r"[\x00-\x1f]+", payload.decode("utf-8", errors="ignore")):
            item = item.strip()
            if len(item) > 2 and item.startswith("{"):
                try:
                    self._parse_message(json.loads(item))
                except Exception as e:
                    logger.debug(
                        i18n.tr(
                            "[B站弹幕]消息解析异常: {type_name} item={item}",
                            type_name=type(e).__name__,
                            item=repr(item[:64]),
                        )
                    )

    def _parse_message(self, obj: dict) -> None:
        """解析单条 JSON 弹幕消息（弹幕/SC），提取内容、用户、颜色并 emit。"""
        cmd = str(obj.get("cmd", ""))
        if "DANMU_MSG" in cmd:
            info = obj.get("info")
            if isinstance(info, list) and len(info) > 2:
                message = str(info[1])
                color_int = 0
                try:
                    if isinstance(info[0], list) and len(info[0]) > 3:
                        color_int = int(info[0][3])
                except TypeError, ValueError:
                    color_int = 0
                user_info = info[2]
                if isinstance(user_info, list) and len(user_info) > 1:
                    self._emit(
                        DanmakuMessage(
                            type=DanmakuMessageType.CHAT,
                            user_name=str(user_info[1]),
                            message=message,
                            color=f"#{color_int:06X}" if color_int else "#FFFFFF",
                        )
                    )
        elif cmd == "SUPER_CHAT_MESSAGE":
            data = obj.get("data")
            if data:
                self._emit(
                    DanmakuMessage(
                        type=DanmakuMessageType.SUPER_CHAT,
                        user_name=str(data.get("user_info", {}).get("uname", "")),
                        message=str(data.get("message", "")),
                        data=data,
                        color="#FFFFFF",
                    )
                )
