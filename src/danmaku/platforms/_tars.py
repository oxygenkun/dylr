from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

INT1, INT2, INT4, INT8, FLOAT, DOUBLE, STRING1, STRING4 = 0, 1, 2, 3, 4, 5, 6, 7
MAP, LIST, STRUCT_BEGIN, STRUCT_END, ZERO_TAG, SIMPLE_LIST = 8, 9, 10, 11, 12, 13


class TarsInputStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _need(self, n: int) -> int:
        """统一边界校验入口（CR-03）：协议读出的每个长度都必须经此消费。负长度/越界在此拒成。"""
        if n < 0 or self._pos + n > len(self._data):
            raise ValueError(f"bad tars length: pos={self._pos} len={n} total={len(self._data)}")
        return n

    def _advance(self, n: int) -> None:
        """游标推进入口：一切协议长度都经此消费，保证边界校验不可绕过。"""
        self._pos += self._need(n)

    def _assert_progress(self, before: int) -> None:
        """断言本轮确实推进了游标：某类型 skip 原地不动即为死循环，必须抛错止损。"""
        if self._pos <= before:
            raise ValueError(f"tars parse stuck: pos={self._pos} (no progress from {before})")

    def _peek_field(self) -> tuple[int, int] | None:
        """预读下一个字段头但不移动游标，返回 (type, tag)；缓冲读完返回 None。"""
        if self._pos >= len(self._data):
            return None
        b = self._data[self._pos]
        typ = b & 0x0F
        tag = (b & 0xF0) >> 4
        if tag == 15 and self._pos + 1 < len(self._data):
            tag = self._data[self._pos + 1]
        return typ, tag

    def _take_head(self) -> tuple[int, int]:
        """消费并返回下一个字段头 (type, tag)；tag==15 时再吃一个字节作为扩展 tag。"""
        b = self._data[self._pos]
        self._pos += 1
        typ = b & 0x0F
        tag = (b & 0xF0) >> 4
        if tag == 15:
            tag = self._data[self._pos]
            self._pos += 1
        return typ, tag

    def _skip(self, typ: int) -> None:
        """按类型 typ 跳过已消费头之后的字段体（容器类型递归跳过其元素）；未知类型抛 ValueError。"""
        if typ in (INT1,):
            self._advance(1)
        elif typ == INT2:
            self._advance(2)
        elif typ in (INT4, FLOAT):
            self._advance(4)
        elif typ in (INT8, DOUBLE):
            self._advance(8)
        elif typ == STRING1:
            n = self._data[self._pos]
            self._advance(1 + n)
        elif typ == STRING4:
            n = struct.unpack_from(">i", self._data, self._pos)[0]
            self._advance(4 + n)
        elif typ == SIMPLE_LIST:
            self._take_head()
            n = self.read_int(0)
            self._advance(n)
        elif typ == LIST:
            n = self.read_int(0)
            if n > len(self._data):
                raise ValueError(f"bad tars list size: {n}")
            for _ in range(n):
                t2, _ = self._take_head()
                self._skip(t2)
        elif typ == MAP:
            n = self.read_int(0)
            if n > len(self._data):
                raise ValueError(f"bad tars map size: {n}")
            for _ in range(n):
                t2, _ = self._take_head()
                self._skip(t2)
                t2, _ = self._take_head()
                self._skip(t2)
        elif typ == STRUCT_BEGIN:
            self.finish_struct()
        elif typ in (STRUCT_END, ZERO_TAG):
            pass
        else:
            raise ValueError(f"Unknown tars type: {typ}")

    def _goto(self, tag: int) -> tuple[int, int] | None:
        """定位到目标 tag 字段并消费其头字节，返回 (type, tag)；。"""
        while True:
            f = self._peek_field()
            if f is None:
                return None
            typ, cur_tag = f
            if typ == STRUCT_END or cur_tag > tag:
                return None
            _before = self._pos
            self._take_head()
            if cur_tag == tag:
                return (typ, tag)
            self._skip(typ)
            self._assert_progress(_before)

    def read_int(self, tag: int, default: Any = 0) -> Any:
        """读取 tag 位置的整数（兼容 INT1/2/4/8 与 ZERO_TAG）；字段缺失或类型不符返回 default。"""
        found = self._goto(tag)
        if found is None:
            return default
        typ, _ = found
        if typ == ZERO_TAG:
            return 0
        if typ == INT1:
            v = struct.unpack_from(">b", self._data, self._pos)[0]
            self._pos += 1
            return v
        if typ == INT2:
            v = struct.unpack_from(">h", self._data, self._pos)[0]
            self._pos += 2
            return v
        if typ == INT4:
            v = struct.unpack_from(">i", self._data, self._pos)[0]
            self._pos += 4
            return v
        if typ == INT8:
            v = struct.unpack_from(">q", self._data, self._pos)[0]
            self._pos += 8
            return v
        self._skip(typ)
        return default

    def read_string(self, tag: int, default: str = "") -> str:
        """读取 tag 位置的字符串（STRING1 单字节长度 / STRING4 四字节长度），按 UTF-8 忽略错误解码；。"""
        found = self._goto(tag)
        if found is None:
            return default
        typ, _ = found
        if typ == ZERO_TAG:
            return ""
        if typ in (STRING1, STRING4):
            if typ == STRING1:
                self._need(1)
                n = self._data[self._pos]
                self._advance(1)
            else:
                self._need(4)
                n = struct.unpack_from(">i", self._data, self._pos)[0]
                self._advance(4)
            self._need(n)
            s = self._data[self._pos : self._pos + n]
            self._advance(n)
            return s.decode("utf-8", errors="ignore")
        self._skip(typ)
        return default

    def read_bytes(self, tag: int, default: bytes = b"") -> bytes:
        """读取 tag 位置的二进制内容（SIMPLE_LIST 或 STRING1/STRING4 均视作字节串）；。"""
        found = self._goto(tag)
        if found is None:
            return default
        typ, _ = found
        if typ == ZERO_TAG:
            return b""
        if typ == SIMPLE_LIST:
            self._take_head()
            n = self.read_int(0)
            self._need(n)
            b = self._data[self._pos : self._pos + n]
            self._advance(n)
            return b
        if typ in (STRING1, STRING4):
            if typ == STRING1:
                self._need(1)
                n = self._data[self._pos]
                self._advance(1)
            else:
                self._need(4)
                n = struct.unpack_from(">i", self._data, self._pos)[0]
                self._advance(4)
            self._need(n)
            b = self._data[self._pos : self._pos + n]
            self._advance(n)
            return b
        self._skip(typ)
        return default

    def read_struct(self, tag: int, parser: Callable[[TarsInputStream], Any]) -> Any:
        """读取 tag 位置的结构体：交由 parser 回调解析字段并消费 STRUCT_END，返回其结果；。"""
        found = self._goto(tag)
        if found is None:
            return None
        typ, _ = found
        if typ == ZERO_TAG:
            return None
        if typ != STRUCT_BEGIN:
            self._skip(typ)
            return None
        return parser(self)

    def finish_struct(self) -> None:
        """供 parser 收尾调用：吃掉剩余未读字段（parser 未读到的更高 tag）直至 STRUCT_END，。"""
        while True:
            f = self._peek_field()
            if f is None:
                return
            typ, _ = f
            if typ == STRUCT_END:
                self._take_head()
                return
            _before = self._pos
            self._take_head()
            self._skip(typ)
            self._assert_progress(_before)


class TarsOutputStream:
    def __init__(self) -> None:
        self._buf = bytearray()

    def _write_head(self, typ: int, tag: int) -> None:
        """写入一个字段头字节：高4位放 tag、低4位放 type（此处 tag 均 <15，无需扩展 tag）。"""
        self._buf.append((tag << 4) | typ)

    def write_int(self, value: int, tag: int) -> None:
        """写入整数字段：0 用 ZERO_TAG 压缩，能放进 int32 用 INT4，否则用 INT8。"""
        if value == 0:
            self._write_head(ZERO_TAG, tag)
            return
        if -(2**31) <= value <= 2**31 - 1:
            self._write_head(INT4, tag)
            self._buf.extend(struct.pack(">i", value))
        else:
            self._write_head(INT8, tag)
            self._buf.extend(struct.pack(">q", value))

    def write_bool(self, value: bool, tag: int) -> None:
        """写入布尔字段：False 用 ZERO_TAG，True 用 INT1 值 1。"""
        if not value:
            self._write_head(ZERO_TAG, tag)
            return
        self._write_head(INT1, tag)
        self._buf.append(1)

    def write_string(self, value: str, tag: int) -> None:
        """写入字符串字段：UTF-8 编码后按长度选 STRING1（<=255）或 STRING4，再追加内容。"""
        data = value.encode("utf-8")
        if len(data) > 255:
            self._write_head(STRING4, tag)
            self._buf.extend(struct.pack(">i", len(data)))
        else:
            self._write_head(STRING1, tag)
            self._buf.append(len(data))
        self._buf.extend(data)

    def write_bytes(self, value: bytes, tag: int) -> None:
        """写入字节串字段：以 SIMPLE_LIST 落盘——元素类型头(BYTE tag0) + 长度(Tars 自描述整数域，非固定4字节) + 内容。"""
        self._write_head(SIMPLE_LIST, tag)
        self._write_head(INT1, 0)
        self.write_int(len(value), 0)
        self._buf.extend(value)

    def to_bytes(self) -> bytes:
        """导出编码结果：返回当前缓冲的不可变 bytes 副本。"""
        return bytes(self._buf)
