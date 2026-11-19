"""声明式设置加载：类型、缺省值、派生值与热加载。"""

from pathlib import Path

import pytest
import tomlkit

from src.config import settings
from src.config.store import ConfigStore


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in settings.__annotations__:
        monkeypatch.setattr(settings, name, getattr(settings, name))


def test_load_reads_recording_notifications_accounts_and_cookies(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        tomlkit.dumps(
            {
                "录制设置": {
                    "同一时间访问网络的线程数": "7",
                    "最大同时录制数(0为不限制)": 2,
                    "是否使用代理ip(是/否)": "是",
                    "代理地址": "http://proxy.example:8080",
                    "使用代理录制的平台(逗号分隔)": "douyin，tiktok",
                    "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频": "mp4",
                    "录制空间剩余阈值(gb)": "2.5",
                    "是否录制完成后执行自定义脚本": True,
                    "自定义脚本执行命令": "echo completed",
                    "抖音关注轮询间隔(秒)": 90,
                    "抖音私信自动录制每日上限(个)": 8,
                },
                "推送配置": {"开播推送开启(是/否)": "否", "自定义推送标题": "测试标题"},
                "账号密码": {"sooplive账号": "test-account", "partner_code": "P-12345"},
                "Authorization": {"popkontv_token": "test-token"},
                "Cookie": {"抖音cookie": "test-cookie", "B站cookie": "bili-test", "17live_cookie": "live-test"},
                "Bark主播中断级别": {"https://live.douyin.com/123": "timeSensitive"},
            }
        ),
        encoding="utf-8",
    )
    store = ConfigStore(path)
    store.read()
    settings.load(store)
    assert settings.max_request == 7 and settings.recording_limit == 2
    assert settings.video_save_type == "MP4" and settings.disk_space_limit == 2.5
    assert settings.proxy_addr == "http://proxy.example:8080"
    assert settings.enable_proxy_platform_list == ["douyin", "tiktok"]
    assert settings.custom_script == "echo completed"
    assert settings.douyin_follow_interval == 90 and settings.douyin_dm_daily_limit == 8
    assert settings.begin_show_push is False and settings.push_message_title == "测试标题"
    assert settings.sooplive_username == "test-account" and settings.popkontv_partner_code == "P-12345"
    assert settings.popkontv_access_token == "test-token"
    assert (settings.dy_cookie, settings.bili_cookie, settings.yiqilive_cookie) == (
        "test-cookie",
        "bili-test",
        "live-test",
    )
    assert settings.bark_level_overrides == {"https://live.douyin.com/123": "timeSensitive"}


def test_load_keeps_numeric_fallbacks_bounds_and_original_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        tomlkit.dumps(
            {
                "录制设置": {
                    "同一时间访问网络的线程数": "bad",
                    "循环时间(秒)": -1,
                    "排队读取网址时间(秒)": -1,
                    "视频分段时间(秒)": -1,
                    "单次录制时长上限(秒,0为不限制)": -1,
                    "录制空间剩余阈值(gb)": "nan",
                    "弹幕分片时长(秒)": "bad",
                    "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频": "unknown",
                    "是否使用代理ip(是/否)": "maybe",
                    "抖音关注轮询间隔(秒)": "bad",
                },
                "推送配置": {"直播推送检测频率(秒)": -1},
            }
        ),
        encoding="utf-8",
    )
    store = ConfigStore(path)
    store.read()
    settings.load(store)
    assert settings.max_request == 3
    assert (settings.delay_default, settings.local_delay_default, settings.push_check_seconds) == (30, 0, 30)
    assert settings.split_time == "1800" and settings.max_record_seconds == 0
    assert settings.disk_space_limit == 1.0 and settings.danmaku_split_time == 1800.0
    assert settings.video_save_type == "TS" and settings.use_proxy is True
    assert settings.douyin_follow_interval == 60
    store.read()
    assert store.native("录制设置", "同一时间访问网络的线程数") == "bad"
    assert store.native("录制设置", "是否使用代理ip(是/否)") == "maybe"


def test_reload_restores_declared_defaults_and_clears_derived_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        tomlkit.dumps(
            {
                "录制设置": {
                    "同一时间访问网络的线程数": 9,
                    "保存文件夹是否以作者区分": False,
                    "代理地址": "http://proxy.example:8080",
                    "是否录制完成后执行自定义脚本": True,
                    "自定义脚本执行命令": "echo completed",
                    "弹幕录制平台(逗号分隔)": "B站直播",
                },
                "Cookie": {"抖音cookie": "test-cookie"},
                "Bark主播中断级别": {"room": "active"},
            }
        ),
        encoding="utf-8",
    )
    store = ConfigStore(path)
    store.read()
    settings.load(store)
    assert settings.max_request == 9 and settings.folder_by_author is False
    assert settings.proxy_addr and settings.custom_script and settings.bark_level_overrides
    path.write_text('["录制设置"]\n"是否使用代理ip(是/否)" = false\n', encoding="utf-8")
    assert store.reload_if_changed()
    settings.load(store)
    assert settings.max_request == 3 and settings.folder_by_author is True
    assert settings.proxy_addr is None and settings.custom_script is None
    assert settings.dy_cookie == "" and settings.bark_level_overrides == {}
    assert settings.max_record_seconds == 6 * 60 * 60
    assert settings.danmaku_platforms == ["斗鱼直播", "B站直播", "虎牙直播", "抖音直播", "TwitchTV"]
    store.read()
    assert store.native("录制设置", "同一时间访问网络的线程数") == 3
    assert store.native("录制设置", "保存文件夹是否以作者区分") is True
    assert not store.has_option("录制设置", "自定义脚本执行命令")
