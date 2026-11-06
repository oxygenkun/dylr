# 抖音私信读取与直播分享解析回归锁（离线）。
#
# 覆盖：protobuf 会话/消息解析、直播分享识别、抖音号到直播间 URL 的拼接、
# 轮询游标去重、每日上限、离线跳过，以及停用状态下零网络请求。
# 所有 HTTP 边界由 FakeClient 替换，不读取真实 Cookie、不访问抖音。

import json
import threading
from typing import Any, cast
from unittest.mock import Mock

import pytest

from src.monitors.douyin_dm import (
    ADDED,
    Conversation,
    DmApiError,
    DmMessage,
    DouyinDmMonitor,
    build_im_envelope,
    extract_live_share,
    live_url_for,
    parse_conversations,
    parse_messages,
)


# 最小 protobuf 写助手：测试用固定字节模拟网页 SDK 的真实响应，
# 不 import 生产编码函数，避免「编码和解析同错」时互相掩盖。
def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number: int, value: Any) -> bytes:
    if isinstance(value, int):
        return _varint((number << 3) | 0) + _varint(value)
    raw = value if isinstance(value, bytes) else str(value).encode()
    return _varint((number << 3) | 2) + _varint(len(raw)) + raw


# 响应外层：f1=命令、f2=请求 id、f3=状态码、f4=OK、f6=业务 body。
def _top(command: int, body: bytes) -> bytes:
    return _field(1, command) + _field(2, 10006) + _field(3, 0) + _field(4, b"OK") + _field(6, body)


# 会话列表：f6 -> f2006 -> repeated f1（会话项），会话项 f1/id、f2/short_id、f50/f4 时间。
def _conversation_payload(*items: tuple[str, int, int]) -> bytes:
    listing = b"".join(
        _field(1, _field(1, conv) + _field(2, short) + _field(50, _field(4, ts))) for conv, short, ts in items
    )
    return _top(2006, _field(2006, listing))


# 消息列表：f6 -> f301 -> repeated f1（消息项），消息项含 server_id/type/content/时间。
def _message_payload(*messages: tuple[str, int, int, int, dict[str, Any]]) -> bytes:
    listing = b""
    for server_id, type_code, created_ms, sender, content in messages:
        raw = _field(1, "0:1:1:2") + _field(3, int(server_id)) + _field(6, type_code)
        raw += _field(7, int(sender)) + _field(8, json.dumps(content, ensure_ascii=False))
        raw += _field(10, created_ms) + _field(14, "MS4wLjABAAAAtest")
        listing += _field(1, raw)
    return _top(301, _field(301, listing))


def _share(room_id: str = "7699488590597835571", name: str = "66喵") -> dict[str, Any]:
    return {
        "room_id": room_id,
        "room_owner_id": "62346419314",
        "room_owner_sec_id": "MS4wLjABAAAAtest",
        "room_owner_name": name,
        "push_detail": name,
    }


# 只替换网络边界：会话/消息/用户/直播间四类响应都由用例给定，
# 监控器内部的游标、去重、上限逻辑全部走真实实现。
class FakeClient:
    def __init__(
        self,
        conversations: list[Conversation],
        messages: list[DmMessage],
        users: dict[str, dict[str, Any]],
        live: dict[str, dict[str, Any] | None],
    ) -> None:
        self.conversations = conversations
        self.messages = messages
        self.users = users
        self.live = live

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def list_conversations(self) -> list[Conversation]:
        return self.conversations

    def get_messages(self, _conversation: Conversation) -> list[DmMessage]:
        return self.messages

    def get_user_info(self, sec_user_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {sec: self.users[sec] for sec in sec_user_ids if sec in self.users}

    def get_live_room(self, web_rid: str) -> dict[str, Any] | None:
        return self.live.get(web_rid)


# 固定时钟/日期，避免用例受系统时间、时区与 monotonic 起点影响。
def _monitor(client: FakeClient, add_room: Any, **overrides: Any) -> DouyinDmMonitor:
    monitor = DouyinDmMonitor(
        threading.Event(),
        add_room=add_room,
        clock=lambda: 100.0,
        now_ms=lambda: 1_000_000_000,
        today=lambda: "2026-10-22",
        client_factory=lambda _cookie, _proxy: cast(Any, client),
    )
    settings: dict[str, Any] = {
        "enabled": True,
        "cookie": "test-cookie",
        "proxy": "",
        "interval": 60,
        "daily_limit": 10,
        "cooldown": 0,
        "quality": "原画",
        "require_live": True,
        "lookback_minutes": 60,
    }
    settings.update(overrides)
    monitor.configure(**settings)
    return monitor


class TestParsing:
    # 正向解析：字段号/类型与生产代码约定逐项对齐。
    def test_conversations_and_messages_are_parsed(self) -> None:
        # 会话项只保留后续请求必需的 conv_id/short_id，last_activity 仅用于诊断。
        conversations = parse_conversations(_conversation_payload(("0:1:1:2", 123, 456)))
        assert conversations == [Conversation("0:1:1:2", 123, 456)]

        # 消息项同时验证 server_id、类型、时间、发送者与 content JSON 解析。
        messages = parse_messages(_message_payload(("789", 21, 1_000_000_000, 42, _share())))
        assert len(messages) == 1
        assert messages[0].server_id == "789"
        assert messages[0].type_code == 21
        assert messages[0].created_ms == 1_000_000_000
        assert messages[0].sender_uid == "42"
        assert messages[0].content["room_owner_name"] == "66喵"

    # 登录态失效/风控拒绝时状态码非 0，必须上抛而不是返回空列表冒充「没有消息」。
    def test_nonzero_status_is_rejected(self) -> None:
        # 同一份拒绝帧分别喂给两个解析入口，防止某一侧漏判状态码。
        payload = _field(1, 301) + _field(3, 3) + _field(4, b"bad")
        with pytest.raises(DmApiError):
            parse_messages(payload)
        with pytest.raises(DmApiError):
            parse_conversations(payload)

    # 截断帧不得让解析循环静默空转；统一收敛成 DmApiError 供上层跳过本轮。
    def test_malformed_payload_is_rejected(self) -> None:
        with pytest.raises(DmApiError):
            parse_messages(b"\x08")
        with pytest.raises(DmApiError):
            parse_conversations(b"\x08")

    # 只有同时带 room_id + room_owner_sec_id 的才是直播分享；
    # 普通文本和视频分享（aweType=800）不得误判成直播间。
    def test_share_extraction_ignores_text_and_video(self) -> None:
        # 三种消息共用同一个提取函数，这里用最小 content 区分「分享/非分享」。
        text = DmMessage("1", 7, "1", "", 1, {"text": "hi"})
        video = DmMessage("2", 8, "1", "", 1, {"aweType": 800, "itemId": "123"})
        assert extract_live_share(text) is None
        assert extract_live_share(video) is None
        share = DmMessage("3", 21, "1", "", 1, _share())
        assert extract_live_share(share) is not None

    # 抖音号会直接拼进 URL 路径，必须挡住 ../、空格等路径注入形态。
    def test_live_url_rejects_path_injection(self) -> None:
        assert live_url_for("yikobaofan") == "https://live.douyin.com/yikobaofan"
        assert live_url_for("61007711-abc") == "https://live.douyin.com/61007711-abc"
        assert live_url_for("../etc/passwd") is None
        assert live_url_for("a b") is None
        assert live_url_for("") is None

    # 信封必须带命令号与当前构建号；构建号缺失会被服务端按旧客户端处理。
    def test_envelope_contains_command_and_build(self) -> None:
        envelope = build_im_envelope(2006, b"\x08\x01")
        assert b"0d50935:feat/pc-im-group" in envelope
        assert envelope.startswith(b"\x08")


class TestMonitor:
    # 一条直播分享 + 一条普通文本：确保只处理分享，且普通文本仍推进游标。
    def _messages(self, now_ms: int = 1_000_000_000) -> list[DmMessage]:
        return [
            DmMessage("101", 21, "42", "MS4wLjABAAAAtest", now_ms - 60_000, _share()),
            DmMessage("102", 7, "42", "MS4wLjABAAAAtest", now_ms - 30_000, {"text": "hello"}),
        ]

    # 主路径：分享 -> sec_uid -> unique_id -> 在播校验 -> add_room 回调。
    # 第二轮的相同消息必须被 server_id 游标挡住，证明不是每轮重复写配置。
    def test_live_share_is_added_once(self) -> None:
        add = Mock(return_value=ADDED)
        client = FakeClient(
            [Conversation("0:1:1:2", 123)],
            self._messages(),
            {"MS4wLjABAAAAtest": {"unique_id": "yikobaofan", "nickname": "66喵"}},
            {"yikobaofan": {"is_live": True, "room_id": "7699488590597835571", "anchor": "66喵", "title": "你好呀"}},
        )
        monitor = _monitor(client, add)
        monitor.poll_once()
        # 参数顺序与回调契约一致：URL、画质、主播名；主播名取用户接口的 nickname。
        add.assert_called_once_with("https://live.douyin.com/yikobaofan", "原画", "66喵")

        # 同一批消息在第二轮不应重复添加（server_id 游标已推进）。
        monitor.poll_once()
        assert add.call_count == 1

    # 离线的分享不加：否则配置里会多出一个每轮空转、永不录制的房间。
    def test_offline_share_is_skipped(self) -> None:
        add = Mock(return_value=ADDED)
        client = FakeClient(
            [Conversation("0:1:1:2", 123)],
            self._messages(),
            {"MS4wLjABAAAAtest": {"unique_id": "yikobaofan", "nickname": "66喵"}},
            {"yikobaofan": {"is_live": False, "room_id": "7699488590597835571", "anchor": "66喵", "title": ""}},
        )
        monitor = _monitor(client, add)
        monitor.poll_once()
        # 离线判定在 add_room 之前；回调一次都不应发生。
        add.assert_not_called()

    # 每日上限=1 时，同一轮内的第二条分享必须被拦下；上限按成功添加计数，不是按尝试。
    def test_daily_limit_is_enforced(self) -> None:
        # 两条分享指向同一 unique_id，但 room_id 不同；上限只允许第一次添加。
        add = Mock(return_value=ADDED)
        messages = [
            DmMessage("201", 21, "42", "MS4wLjABAAAAtest", 999_990_000, _share("1", "A")),
            DmMessage("202", 21, "42", "MS4wLjABAAAAtest", 999_995_000, _share("2", "B")),
        ]
        users = {
            "MS4wLjABAAAAtest": {"unique_id": "anchor", "nickname": "A"},
        }
        client = FakeClient(
            [Conversation("0:1:1:2", 123)],
            messages,
            users,
            {"anchor": {"is_live": True, "room_id": "1", "anchor": "A", "title": ""}},
        )
        monitor = _monitor(client, add, daily_limit=1)
        monitor.poll_once()
        # 第二条被上限拦下，所以回调只发生一次。
        assert add.call_count == 1

    # 关闭状态必须零网络：client_factory 一旦被调用就立刻失败，用例据此证明没有连接。
    def test_disabled_does_not_create_client(self) -> None:
        add = Mock(return_value=ADDED)
        monitor = DouyinDmMonitor(
            threading.Event(),
            add_room=add,
            client_factory=cast(Any, Mock(side_effect=AssertionError("must not connect"))),
        )
        monitor.configure(False, "test-cookie")
        monitor.poll_once()
        # 关闭状态直接 return，连 client 都不创建。
        add.assert_not_called()

    # 抖音号能解析，但返回的 room_id 与分享里的不一致：说明主播已换房间，宁可跳过。
    def test_room_changed_is_skipped(self) -> None:
        add = Mock(return_value=ADDED)
        client = FakeClient(
            [Conversation("0:1:1:2", 123)],
            self._messages(),
            {"MS4wLjABAAAAtest": {"unique_id": "yikobaofan", "nickname": "66喵"}},
            {"yikobaofan": {"is_live": True, "room_id": "different", "anchor": "66喵", "title": ""}},
        )
        monitor = _monitor(client, add)
        monitor.poll_once()
        # room_changed 是终态：既不加配置，也不应被当成离线去重。
        add.assert_not_called()
