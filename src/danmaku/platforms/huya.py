from __future__ import annotations

import base64
from typing import Any

from src.danmaku.base import DanmakuBase, DanmakuMessage, DanmakuMessageType, spawn_danmaku_task
from src.danmaku.platforms._tars import TarsInputStream, TarsOutputStream
from src.network.websocket import WebSocketClient
from src.support import i18n
from src.support.logger import logger

SERVER_URL = "wss://cdnws.api.huya.com"

HEARTBEAT_DATA = base64.b64decode("ABQdAAwsNgBM")

URI_CHAT = 1400
URI_ONLINE = 8006


class HuyaDanmaku(DanmakuBase):
    heartbeat_interval = 60.0  # 60s

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化：调用父类并重置内部状态（参数、WS 连接）。"""
        super().__init__(*args, **kwargs)
        self._args: dict = {}
        self._ws: WebSocketClient | None = None

    async def start(self, args: Any) -> None:
        """启动：解析 ayyuid/topSid 等参数，建立虎牙 WS 连接；缺失则回调 on_close。"""
        self._args = args if isinstance(args, dict) else {}
        if not self._args.get("ayyuid") or not self._args.get("topSid"):
            if self._on_close:
                self._on_close("缺少 ayyuid/topSid")
            return

        self._ws = WebSocketClient(
            url=SERVER_URL,
            heartbeat_interval=self.heartbeat_interval,
            on_message=self.decode_message,
            on_ready=self._on_ws_ready,
            on_heartbeat=self.heartbeat,
            on_close=self._on_close,
            on_reconnect=self._on_reconnect,
        )
        await self._ws.connect()

    def _on_ws_ready(self) -> None:
        """WS 就绪回调：触发 on_ready 并异步发送进房数据。"""
        if self._on_ready:
            self._on_ready()
        spawn_danmaku_task(self._join_room())

    async def _join_room(self) -> None:
        """异步发送进房数据（WSRegisterReq）加入直播间。"""
        if self._ws is None:
            return
        await self._ws.send(self._make_join_data())

    def _make_join_data(self) -> bytes:
        """构造 Tars 编码的进房数据字节串（WSCmd iCmdType=1），返回 bytes。"""
        oos = TarsOutputStream()
        oos.write_int(int(self._args.get("ayyuid") or 0), 0)
        oos.write_bool(True, 1)
        oos.write_string("", 2)
        oos.write_string("", 3)
        oos.write_int(int(self._args.get("topSid") or 0), 4)
        oos.write_int(int(self._args.get("topSid") or 0), 5)
        oos.write_int(0, 6)
        oos.write_int(0, 7)

        wscmd = TarsOutputStream()
        wscmd.write_int(1, 0)
        wscmd.write_bytes(oos.to_bytes(), 1)
        return wscmd.to_bytes()

    async def heartbeat(self) -> None:
        """发送硬编码心跳字节包维持连接。"""
        if self._ws is not None:
            await self._ws.send(HEARTBEAT_DATA)

    async def stop(self) -> None:
        """停止：置停止标志并关闭 WebSocket 连接。"""
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()

    def decode_message(self, data: bytes | str) -> None:
        """解析 Tars 帧：识别 cmdType==7 的消息推送并解码 HYPushMessage。"""
        if isinstance(data, str):
            return
        try:
            stream = TarsInputStream(data)
            cmd_type = stream.read_int(0)
            if cmd_type != 7:
                return  # 非消息推送（如注册回应），忽略
            push_data = stream.read_bytes(1)
            self._decode_push_message(push_data)
        except Exception as e:
            logger.debug(
                i18n.tr(
                    "[虎牙弹幕]帧解析异常: {type_name} head={head}", type_name=type(e).__name__, head=data[:16].hex()
                )
            )

    def _decode_push_message(self, data: bytes) -> None:
        """解析推送消息：按 uri 分发弹幕(1400)/在线人数(8006)。"""
        stream = TarsInputStream(data)
        uri = stream.read_int(1)
        msg = stream.read_bytes(2)

        if uri == URI_CHAT:
            self._decode_chat(msg)
        elif uri == URI_ONLINE:
            s = TarsInputStream(msg)
            online = s.read_int(0)
            self._emit(
                DanmakuMessage(
                    type=DanmakuMessageType.ONLINE,
                    user_name="",
                    message="",
                    data=online,
                    color="#FFFFFF",
                )
            )

    def _decode_chat(self, msg: bytes) -> None:
        """解析 HYMessage 弹幕：提取发送者昵称、内容与颜色并 emit。"""
        stream = TarsInputStream(msg)

        def parse_sender(inner: TarsInputStream) -> dict:
            """从 userInfo 结构体解析出发送者昵称，返回 {nick_name}。"""
            nick = inner.read_string(2)
            inner.finish_struct()
            return {"nick_name": nick}

        sender = stream.read_struct(0, parse_sender) or {"nick_name": ""}
        content = stream.read_string(3)
        font_color = 0

        def parse_format(f: TarsInputStream) -> None:
            """从 bulletFormat 结构体解析出弹幕字体颜色(int)。"""
            nonlocal font_color
            font_color = f.read_int(0)
            f.finish_struct()

        stream.read_struct(6, parse_format)

        color = "#FFFFFF" if font_color <= 0 else f"#{font_color:06X}"
        self._emit(
            DanmakuMessage(
                type=DanmakuMessageType.CHAT,
                user_name=sender["nick_name"],
                message=content,
                color=color,
            )
        )
