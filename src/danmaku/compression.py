"""弹幕 gzip/zlib 载荷的有界解压。"""

MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024


def decompress_zlib(data: bytes, limit: int = MAX_DECOMPRESSED_BYTES, wbits: int = 31) -> bytes:
    """限制 gzip/zlib 解压后的输出大小。"""
    import zlib

    obj = zlib.decompressobj(wbits)
    out = obj.decompress(data, limit + 1)
    if len(out) > limit:
        raise ValueError(f"decompressed payload exceeds limit ({limit} bytes)")
    tail = obj.flush()
    if len(out) + len(tail) > limit:
        raise ValueError(f"decompressed payload exceeds limit ({limit} bytes)")
    return out + tail
