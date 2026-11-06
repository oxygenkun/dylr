"""TOML 配置存储：迁移、类型校验、热加载与备份脱敏。"""

from pathlib import Path

import pytest

from src.config import io as config_io
from src.config.store import (
    ConfigError,
    ConfigStore,
    ConfigValueError,
    coerce_value,
    ensure_config_migrated,
    migrate_ini_to_toml,
)


def _write_ini(path: Path) -> None:
    path.write_text(
        "[录制设置]\n"
        "language = zh_CN\n"
        "是否录制弹幕(是/否) = 是\n"
        "循环时间(秒) = 60\n"
        "录制空间剩余阈值(gb) = 1.5\n"
        "代理地址 = \n"
        "原画|超清|高清|标清|流畅 = 原画\n"
        "[Cookie]\n"
        "抖音cookie = ttwid=abc\n",
        encoding="utf-8-sig",
    )


def _load(path: Path) -> ConfigStore:
    store = ConfigStore(path)
    store.read()
    return store


class TestMigration:
    def test_types_are_converted_and_legacy_file_renamed(self, tmp_path: Path) -> None:
        ini = tmp_path / "config.ini"
        toml_path = tmp_path / "config.toml"
        _write_ini(ini)

        assert ensure_config_migrated(toml_path, ini) is True
        assert toml_path.is_file()
        assert not ini.exists()
        assert (tmp_path / "config.ini.migrated").is_file()

        store = _load(toml_path)
        assert store.get("录制设置", "language") == "zh_CN"
        assert store.native("录制设置", "是否录制弹幕(是/否)") is True
        assert store.native("录制设置", "循环时间(秒)") == 60
        assert store.native("录制设置", "录制空间剩余阈值(gb)") == 1.5
        assert store.get("录制设置", "代理地址") == ""
        assert store.get("Cookie", "抖音cookie") == "ttwid=abc"

    def test_existing_toml_is_not_overwritten(self, tmp_path: Path) -> None:
        ini = tmp_path / "config.ini"
        toml_path = tmp_path / "config.toml"
        _write_ini(ini)
        toml_path.write_text('["录制设置"]\nlanguage = "en_US"\n', encoding="utf-8")

        assert ensure_config_migrated(toml_path, ini) is False
        assert _load(toml_path).get("录制设置", "language") == "en_US"
        assert ini.exists()

    def test_missing_legacy_creates_empty_document(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "config.toml"
        assert ensure_config_migrated(toml_path, tmp_path / "absent.ini") is False
        assert toml_path.is_file()
        assert _load(toml_path).sections() == []

    def test_returns_false_when_nothing_to_migrate(self, tmp_path: Path) -> None:
        assert migrate_ini_to_toml(tmp_path / "absent.ini", tmp_path / "out.toml") is False


class TestValidation:
    def test_known_types_are_coerced(self) -> None:
        assert coerce_value("录制设置", "循环时间(秒)", "60") == 60
        assert coerce_value("录制设置", "是否录制弹幕(是/否)", "否") is False
        assert coerce_value("录制设置", "录制空间剩余阈值(gb)", "1.5") == 1.5
        assert coerce_value("录制设置", "language", 5) == 5

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ConfigValueError):
            coerce_value("录制设置", "循环时间(秒)", "abc")
        with pytest.raises(ConfigValueError):
            coerce_value("Web", "web_port", True)

    def test_invalid_value_on_load_falls_back_to_raw_text(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('["录制设置"]\n"循环时间(秒)" = "abc"\n', encoding="utf-8")
        store = _load(path)
        assert store.get("录制设置", "循环时间(秒)") == "abc"
        assert config_io._safe_int(store.get("录制设置", "循环时间(秒)"), 60) == 60


class TestHotReload:
    def test_unchanged_file_skips_reread(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('["录制设置"]\nlanguage = "zh_CN"\n', encoding="utf-8")
        store = _load(path)
        assert store.reload_if_changed() is False

    def test_changed_file_is_reloaded(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('["录制设置"]\nlanguage = "zh_CN"\n', encoding="utf-8")
        store = _load(path)
        path.write_text('["录制设置"]\nlanguage = "en_US"\n', encoding="utf-8")
        assert store.reload_if_changed() is True
        assert store.get("录制设置", "language") == "en_US"

    def test_broken_file_keeps_previous_values(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('["录制设置"]\nlanguage = "zh_CN"\n', encoding="utf-8")
        store = _load(path)
        path.write_text('["录制设置"\n', encoding="utf-8")
        assert store.reload_if_changed() is False
        assert store.get("录制设置", "language") == "zh_CN"


class TestWrite:
    def test_set_persists_and_preserves_comments(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '# 顶部注释\n["录制设置"]\n# 行内注释\nlanguage = "zh_CN"\n',
            encoding="utf-8",
        )
        store = _load(path)
        store.set("录制设置", "循环时间(秒)", 90)
        assert store.write() is True

        text = path.read_text(encoding="utf-8")
        assert "# 顶部注释" in text
        assert "# 行内注释" in text
        assert _load(path).native("录制设置", "循环时间(秒)") == 90

    def test_invalid_set_does_not_touch_document(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('["Web"]\nweb_port = 8000\n', encoding="utf-8")
        store = _load(path)
        with pytest.raises(ConfigValueError):
            store.set("Web", "web_port", "not-a-port")
        assert store.get("Web", "web_port") == "8000"

    def test_case_insensitive_key_reuses_existing_spelling(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('["录制设置"]\n"禁用ssl证书验证的平台(逗号分隔)" = "斗鱼直播"\n', encoding="utf-8")
        store = _load(path)
        assert store.get("录制设置", "禁用SSL证书验证的平台(逗号分隔)") == "斗鱼直播"
        store.set("录制设置", "禁用SSL证书验证的平台(逗号分隔)", "虎牙直播")
        assert store.write() is True

        text = path.read_text(encoding="utf-8")
        assert text.count("禁用ssl证书验证的平台") == 1
        assert text.count("禁用SSL证书验证的平台") == 0

    def test_corrupt_file_reports_error_and_refuses_write(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('["录制设置"\n', encoding="utf-8")
        store = ConfigStore(path)
        with pytest.raises(ConfigError):
            store.read()
        assert store.write() is False
        assert path.read_text(encoding="utf-8") == '["录制设置"\n'


class TestBackupRedaction:
    def test_toml_backup_masks_secrets(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[Cookie]\n"抖音cookie" = "ttwid=abc"\n[Web]\nweb_password = "plain"\n',
            encoding="utf-8",
        )
        backup_dir = tmp_path / "backup"
        assert config_io.backup_file(str(path), str(backup_dir)) is True

        backup = next(backup_dir.iterdir())
        text = backup.read_text(encoding="utf-8")
        assert "ttwid=abc" not in text
        assert "plain" not in text
        assert text.count("***") == 2

    def test_url_config_backup_is_copied_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "URL_config.ini"
        raw = "\ufeffhttps://live.douyin.com/1\n"
        path.write_text(raw, encoding="utf-8")
        backup_dir = tmp_path / "backup"
        assert config_io.backup_file(str(path), str(backup_dir)) is True
        assert next(backup_dir.iterdir()).read_text(encoding="utf-8") == raw
