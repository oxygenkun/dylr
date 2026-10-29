"""控制台编码设置在各平台和无控制台环境下均可调用。"""

import ctypes
import sys
from types import SimpleNamespace

import pytest

from src.support import console


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("stderr_kind", ["stream", "missing", "closed"])
def test_configure_encoding(monkeypatch: pytest.MonkeyPatch, platform: str, stderr_kind: str) -> None:
    configured: list[str] = []
    codepages: list[int] = []

    class Stream:
        def __init__(self, name: str) -> None:
            self.name = name

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            if self.name == "closed":
                raise ValueError("closed stream")
            assert (encoding, errors) == ("utf-8", "replace")
            configured.append(self.name)

    process = SimpleNamespace(**vars(sys))
    process.platform = platform
    process.stdout = Stream("stdout")
    process.stderr = None if stderr_kind == "missing" else Stream(stderr_kind)
    monkeypatch.setattr(console, "sys", process)
    kernel = SimpleNamespace(SetConsoleOutputCP=codepages.append, SetConsoleCP=codepages.append)
    dlls = SimpleNamespace(**vars(ctypes))
    dlls.windll = SimpleNamespace(kernel32=kernel)
    monkeypatch.setattr(console, "ctypes", dlls)

    console.configure_encoding()

    assert configured == (["stdout", "stream"] if stderr_kind == "stream" else ["stdout"])
    assert codepages == ([65001, 65001] if platform == "win32" else [])
