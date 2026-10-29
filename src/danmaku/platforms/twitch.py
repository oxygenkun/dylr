from __future__ import annotations

import random
import re
import urllib.request
from typing import Any, Union

from src.danmaku.base import DanmakuBase, DanmakuMessage, DanmakuMessageType
from src.network.urls import normalize_proxy_url
from src.network.websocket import WebSocketClient

SERVER_URL = "wss://irc-ws.chat.twitch.tv"

_PRIVMSG_RE = re.compile(r"PRIVMSG [^:]+:(.+)")
_NAME_RE = re.compile(r"display-name=([^;]+);")
_COLOR_RE = re.compile(r"color=#([a-zA-Z0-9]{6});")


class TwitchDanmaku(DanmakuBase):
    heartbeat_interval = 40.0  # IRC 侧无服务端心跳，靠本端按时发 PONG 保活

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """记录频道名与 WS 客户端占位；真正建连在 start()。"""
        super().__init__(*args, **kwargs)
        self._channel: str = ""
        self._ws: WebSocketClient | None = None
        self._line_buf = ""  # MI-21：跨帧半行缓冲（见 decode_message）

    async def start(self, args: Any) -> None:
        """启动监听：解析频道名 → 确定代理 → 建立 WebSocket 长连接；无频道名时回调 on_close 并返回。"""
        proxy = None
        if isinstance(args, dict):
            self._channel = str(args.get("channel", ""))
            proxy = args.get("proxy")
        else:
            self._channel = str(args)
        self._channel = self._channel.lstrip("#").strip()
        if not self._channel:
            if self._on_close:
                self._on_close("缺少 channel")
            return

        proxy_addr = normalize_proxy_url(proxy)
        if not proxy_addr:
            for _scheme in ("https", "http"):
                _p = urllib.request.getproxies().get(_scheme)
                if _p:
                    proxy_addr = normalize_proxy_url(_p)
                    break

        self._ws = WebSocketClient(
            url=SERVER_URL,
            heartbeat_interval=self.heartbeat_interval,
            on_message=self.decode_message,
            on_ready=self._on_ws_ready,
            on_heartbeat=self.heartbeat,
            on_close=self._on_close,
            on_reconnect=self._on_reconnect,
            proxy=proxy_addr,
        )
        await self._ws.connect()

    def _on_ws_ready(self) -> None:
        """WebSocket 连接就绪回调：向上层抛 on_ready，随后立即发送 IRC 登录/进房指令。"""
        if self._on_ready:
            self._on_ready()
        self._join_room()

    def _join_room(self) -> None:
        """发送 IRC 匿名登录与进房序列（CAP/PASS/NICK/USER/JOIN），无返回值。"""
        if self._ws is None:
            return
        user = f"justinfan{1000 + random.randint(0, 99999 - 1000)}"
        for line in (
            "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership",
            "PASS SCHMOOPIIE",
            f"NICK {user}",
            f"USER {user} 8 * :{user}",
            f"JOIN #{self._channel}",
        ):
            self._ws.send_nowait(line)

    async def heartbeat(self) -> None:
        """心跳：主动发送 IRC PONG 保活（由 WebSocketClient 按 heartbeat_interval 定时调用）。"""
        if self._ws is not None:
            await self._ws.send("PONG :tmi.twitch.tv")

    async def stop(self) -> None:
        """停止监听：置停止标记并关闭 WebSocket 连接。"""
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()

    def decode_message(self, data: Union[bytes, str]) -> None:
        """解析服务端下行文本（bytes 或 str）：逐行处理，收到 PING 立刻回 PONG，。"""
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="ignore")
        else:
            text = data

        text = self._line_buf + text
        _parts = text.split("\n")
        self._line_buf = _parts.pop()  # 末段可能不完整，留到下帧
        for line in _parts:
            line = line.strip("\r")
            if line.startswith("PING") and self._ws is not None:
                self._ws.send_nowait(line.replace("PING", "PONG", 1))
            content_match = _PRIVMSG_RE.search(line)
            name_match = _NAME_RE.search(line)
            color_match = _COLOR_RE.search(line)
            if content_match and name_match:
                color = "#FFFFFF"
                color_int = 0
                if color_match:
                    try:
                        color_int = int(color_match.group(1), 16)
                        color = f"#{color_match.group(1)}"
                    except ValueError:
                        color = "#FFFFFF"
                        color_int = 0
                if color_int == 0:
                    color = "#FFFFFF"
                self._emit(
                    DanmakuMessage(
                        type=DanmakuMessageType.CHAT,
                        user_name=name_match.group(1),
                        message=content_match.group(1),
                        color=color,
                    )
                )
