# URL_config.ini 追加写入口回归锁（私信自动录制使用）。
#
# 覆盖：BOM/CRLF 保留、末行无换行、重复启用/禁用状态、非法地址、原子写失败。
# 不打桩真实 DNS；所有路径都在 tmp_path 内，绝不碰仓库里的运行期配置。
from pathlib import Path

import pytest

from src.api import config as web_config
from src.config import io as config_io
from src.network import targets
from src.recording import state


@pytest.fixture(autouse=True)
def _stub_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    # web_config 的房间地址校验会解析 host；固定成公网地址，离线可复跑。
    monkeypatch.setattr(targets, "resolve_host_ips", lambda _host: ["114.114.114.114"])


# 统一按 utf-8-sig + newline="" 读：既验证 BOM 只有一个，也验证 CRLF 没被翻译。
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", newline="")


# 新文件：只写一个 BOM、一行「URL,主播: 名字」，并同步内存快照。
def test_add_to_new_file_writes_bom_and_line(tmp_path: Path) -> None:
    path = tmp_path / "URL_config.ini"
    result = config_io.append_room_line(str(path), "https://live.douyin.com/123", "原画", "测试主播")
    assert result == config_io.ROOM_ADDED
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert _read(path) == "https://live.douyin.com/123,主播: 测试主播\n"
    assert state.ini_URL_content == _read(path)


# 已启用的重复地址：返回 exists 且文件字节完全不变（不刷新 mtime）。
def test_duplicate_enabled_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / "URL_config.ini"
    path.write_text("\ufeffhttps://live.douyin.com/123,主播: 已有\n", encoding="utf-8")
    before = path.read_bytes()
    result = config_io.append_room_line(str(path), "https://live.douyin.com/123", "", "新名字")
    assert result == config_io.ROOM_EXISTS
    assert path.read_bytes() == before


# 被用户手工注释的房间：返回 disabled，绝不自动取消注释。
def test_disabled_room_is_not_reenabled(tmp_path: Path) -> None:
    path = tmp_path / "URL_config.ini"
    path.write_text("\ufeff# https://live.douyin.com/123,主播: 已禁用\n", encoding="utf-8")
    before = path.read_bytes()
    result = config_io.append_room_line(str(path), "https://live.douyin.com/123", "", "")
    assert result == config_io.ROOM_DISABLED
    assert path.read_bytes() == before


# CRLF 文件与「末行没有换行」两种真实形态：追加后仍保持 CRLF，且新行独立成行。
def test_crlf_and_missing_trailing_newline_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "URL_config.ini"
    path.write_bytes(("\ufeffhttps://live.douyin.com/1,主播: A\r\nhttps://live.douyin.com/2").encode("utf-8"))
    result = config_io.append_room_line(str(path), "https://live.douyin.com/3", "", "C")
    assert result == config_io.ROOM_ADDED
    raw = path.read_bytes()
    assert raw.endswith(b"https://live.douyin.com/3,\xe4\xb8\xbb\xe6\x92\xad: C\r\n")
    assert raw.count(b"\r\n") == 3
    assert raw.count(b"\n") == 3


# 非法 scheme 必须在写盘前被 format_url_line 拦下，文件保持原样。
def test_invalid_url_is_rejected_without_touching_file(tmp_path: Path) -> None:
    path = tmp_path / "URL_config.ini"
    path.write_text("\ufeffhttps://live.douyin.com/1\n", encoding="utf-8")
    before = path.read_bytes()
    assert config_io.append_room_line(str(path), "file:///etc/passwd", "", "") == config_io.ROOM_INVALID
    assert path.read_bytes() == before


# 原子写失败必须回写 write_failed，不能像旧接口那样无条件报成功。
def test_write_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "URL_config.ini"
    path.write_text("\ufeffhttps://live.douyin.com/1\n", encoding="utf-8")
    monkeypatch.setattr(config_io, "_atomic_write_text", lambda *_args, **_kwargs: False)
    assert config_io.append_room_line(str(path), "https://live.douyin.com/2", "", "") == config_io.ROOM_WRITE_FAILED


def test_name_commas_are_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "URL_config.ini"
    assert config_io.append_room_line(str(path), "https://live.douyin.com/123", "", "A,B，C") == config_io.ROOM_ADDED
    assert "主播: A B C" in _read(path)
