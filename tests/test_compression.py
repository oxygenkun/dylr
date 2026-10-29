"""弹幕解压的输出边界。"""

import gzip
import zlib
from collections.abc import Callable

import brotli
import pytest

from src.danmaku.compression import decompress_zlib
from src.danmaku.platforms.bilibili import _decompress_brotli


@pytest.mark.parametrize(("compress", "wbits"), [(gzip.compress, 31), (zlib.compress, 15)])
def test_zlib_output_is_bounded(compress: Callable[[bytes], bytes], wbits: int) -> None:
    assert decompress_zlib(compress(b"hello"), limit=5, wbits=wbits) == b"hello"
    with pytest.raises(ValueError, match="exceeds limit"):
        decompress_zlib(compress(b"a" * 4096), limit=64, wbits=wbits)


def test_brotli_output_is_bounded() -> None:
    assert _decompress_brotli(brotli.compress(b"hello"), limit=5) == b"hello"
    with pytest.raises(ValueError, match="exceeds limit"):
        _decompress_brotli(brotli.compress(b"a" * 4096), limit=64)
