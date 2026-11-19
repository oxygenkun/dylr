"""平台列表通过真实配置加载器统一清洗。"""

from pathlib import Path

import pytest
import tomlkit

from src.config import settings
from src.config.store import ConfigStore


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in settings.__annotations__:
        monkeypatch.setattr(settings, name, getattr(settings, name))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("斗鱼直播, B站直播", ["斗鱼直播", "B站直播"]),
        ("斗鱼直播， B站直播 , 虎牙直播 ,", ["斗鱼直播", "B站直播", "虎牙直播"]),
        ("B站直播,,   ,虎牙直播", ["B站直播", "虎牙直播"]),
        ("\tB站直播\n, 抖音直播 ", ["B站直播", "抖音直播"]),
        ("", []),
        (" , , ", []),
        ("B站直播", ["B站直播"]),
        ("斗鱼直播,B站直播,虎牙直播,抖音直播,TwitchTV", ["斗鱼直播", "B站直播", "虎牙直播", "抖音直播", "TwitchTV"]),
    ],
)
def test_platform_lists_strip_whitespace_and_empty_items(tmp_path: Path, raw: str, expected: list[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        tomlkit.dumps({"录制设置": {"弹幕录制平台(逗号分隔)": raw, "HLS采集排除平台(逗号分隔)": raw}}),
        encoding="utf-8",
    )
    store = ConfigStore(config)
    store.read()
    settings.load(store)
    assert settings.danmaku_platforms == expected
    assert settings.hls_collection_exclude_platforms == expected


def test_template_platforms_load_without_empty_items(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    template = Path(__file__).resolve().parents[1] / "config/config.toml.example"
    config.write_bytes(template.read_bytes())
    store = ConfigStore(config)
    store.read()
    settings.load(store)
    assert settings.danmaku_platforms
    assert all(item == item.strip() and item for item in settings.danmaku_platforms)
