from __future__ import annotations

import hashlib
import random

XBOGUS_ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe"

_EMPTY_MD5_BYTES = (0x45, 0x3F)


def _rc4_encrypt(key: int, data: bytearray) -> None:
    """用单字节密钥 key 对 data 做 RC4 加密（与 dart rc4Encrypt 一致；原地修改传入的 bytearray，无返回值）。"""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key) & 0xFF
        tmp = s[i]
        s[i] = s[j]
        s[j] = tmp

    ii = 0
    j = 0
    for k in range(len(data)):
        ii = (ii + 1) & 0xFF
        j = (j + s[ii]) & 0xFF
        tmp = s[ii]
        s[ii] = s[j]
        s[j] = tmp
        data[k] ^= s[(s[ii] + s[j]) & 0xFF]


def _encode_base64(data: bytes) -> str:
    """把 data 按每 3 字节切成 4 个 6bit 值，用 X-Bogus 自定义字符表映射为字符串并返回。"""
    out = []
    n = len(data)
    for i in range(0, n, 3):
        b0 = data[i]
        b1 = data[i + 1] if i + 1 < n else 0
        b2 = data[i + 2] if i + 2 < n else 0
        out.append(XBOGUS_ALPHABET[(b0 >> 2) & 0x3F])
        out.append(XBOGUS_ALPHABET[((b0 << 4) | (b1 >> 4)) & 0x3F])
        out.append(XBOGUS_ALPHABET[((b1 << 2) | (b2 >> 6)) & 0x3F] if i + 1 < n else "=")
        out.append(XBOGUS_ALPHABET[b2 & 0x3F] if i + 2 < n else "=")
    return "".join(out)


def _md5_last2(hex_str: str) -> list:
    """把 hex_str 还原为原始字节再做一次 md5，返回摘要的最后两个字节（列表，长度 2，与 dart md5Last2 一致）。"""
    raw = bytes.fromhex(hex_str)
    digest = hashlib.md5(raw).digest()
    return [digest[14], digest[15]]


def generate_xbogus(ms_stub: str, counter: int = 1) -> str:
    """生成 X-Bogus 字符串：ms_stub 必须为 32 字符的 md5 hex（与 dart 一致，否则抛 ValueError），。"""
    if len(ms_stub) != 32:
        raise ValueError("ms_stub must be 32-char md5 hex string")

    random1 = random.randint(0, 255)
    random2 = random.randint(0, 254)

    header = 0x40 | (random1 & 0x1F)
    md5_bytes = _md5_last2(ms_stub)

    payload = bytearray(
        [
            counter & 0x3F,
            0,
            1,
            0x0E,
            _EMPTY_MD5_BYTES[0],
            _EMPTY_MD5_BYTES[1],
            md5_bytes[0],
            md5_bytes[1],
            random2,
            0,
        ]
    )

    checksum = 0
    for i in range(9):
        checksum ^= payload[i]
    payload[9] = checksum

    _rc4_encrypt(random2, payload)

    final_data = bytearray(12)
    final_data[0] = header
    final_data[1] = random2
    for i in range(10):
        final_data[i + 2] = payload[i]

    return _encode_base64(bytes(final_data))


def danmaku_signature(room_id: str, unique_id: str) -> str:
    """由 room_id 与 unique_id 拼出固定顺序参数串（逗号拼接 -> md5 -> generateXbogus(md5, 1)，。"""
    params = [
        ("live_id", "1"),
        ("aid", "6383"),
        ("version_code", "180800"),
        ("webcast_sdk_version", "1.0.14-beta.0"),
        ("room_id", room_id),
        ("sub_room_id", ""),
        ("sub_channel_id", ""),
        ("did_rule", "3"),
        ("user_unique_id", unique_id),
        ("device_platform", "web"),
        ("device_type", ""),
        ("ac", ""),
        ("identity", "audience"),
    ]
    sig_param = ",".join(f"{k}={v}" for k, v in params)
    md5_sig = hashlib.md5(sig_param.encode("utf-8")).hexdigest()
    return generate_xbogus(md5_sig, 1)
