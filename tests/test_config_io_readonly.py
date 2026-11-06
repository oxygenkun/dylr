# 回归测试：配置缺键自愈在写回失败时 best-effort（与 backup_file 一致），
# 以及 main.py 兼容旧键「虎牙是否禁用SSL证书验证(是/否)」仅读取、绝不写回。
# TOML 迁移后：缺键补写、旧键迁移与写回失败降级均针对 config.toml。
from pathlib import Path

import pytest
from loguru import logger
from pytest import MonkeyPatch

from src.config import io as config_io
from src.config.store import ConfigStore
from src.recording import engine as recorder
from src.recording import state


def _store(tmp_path: Path, text: str) -> ConfigStore:
    cfg = tmp_path / "config.toml"
    cfg.write_text(text, encoding="utf-8")
    store = ConfigStore(cfg)
    store.read()
    return store


def _reload(path: Path) -> ConfigStore:
    store = ConfigStore(path)
    store.read()
    return store


def test_read_config_value_missing_key_write_failure_ok(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # 缺键时 read_config_value 会尝试写回；写回失败（只读 / 被占用）必须只记 warning
    # 并返回默认值，而不是抛异常让整个 app 在启动阶段崩溃，且不得留下半写文件。
    store = _store(tmp_path, '["录制设置"]\nlanguage = "zh_cn"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))

    import src.support.utils as utils_module

    real_write = utils_module.atomic_write_text

    def _deny_write(path: str, *args: object, **kwargs: object) -> bool:
        if Path(path) == store.path:
            return False
        return real_write(str(path), *args, **kwargs)

    monkeypatch.setattr(config_io, "atomic_write_text", _deny_write, raising=False)
    monkeypatch.setattr("src.support.utils.atomic_write_text", _deny_write)

    captured: list[str] = []
    handler_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        result = config_io.read_config_value(store, "录制设置", "_缺失的键_xyz", "默认")
    finally:
        logger.remove(handler_id)

    assert result == "默认"
    assert "_缺失的键_xyz" not in store.path.read_text(encoding="utf-8")
    assert any("缺省值写回失败" in c for c in captured)


def test_read_config_value_key_with_delimiter_roundtrip(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # 历史键「最大同时录制数(0=不限制)」含 =，INI 时代会截断/写回失败；TOML 必须原样往返。
    store = _store(tmp_path, '["录制设置"]\nlanguage = "zh_cn"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))

    assert config_io.read_config_value(store, "录制设置", "最大同时录制数(0=不限制)", 0) == "0"
    assert _reload(store.path).get("录制设置", "最大同时录制数(0=不限制)") == "0"


def test_main_huya_old_key_compat_missing_only_reads(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # main.py 兼容旧键在缺键时仅读取、绝不写回。
    store = _store(tmp_path, '["录制设置"]\n"禁用SSL证书验证的平台(逗号分隔)" = "抖音直播"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    platforms = {p.strip() for p in store.get("录制设置", "禁用SSL证书验证的平台(逗号分隔)").split(",") if p.strip()}

    assert not store.has_option("录制设置", "虎牙是否禁用SSL证书验证(是/否)")
    if store.has_option("录制设置", "虎牙是否禁用SSL证书验证(是/否)"):
        if config_io.read_config_bool(store, "录制设置", "虎牙是否禁用SSL证书验证(是/否)", False):
            platforms.add("虎牙直播")

    assert platforms == {"抖音直播"}
    assert not store.has_option("录制设置", "虎牙是否禁用SSL证书验证(是/否)")


@pytest.mark.parametrize("raw", ["是", "true", "TRUE", "1", "yes", "on"])
def test_main_huya_old_key_compat_true_encodings_add(tmp_path: Path, raw: str) -> None:
    # 旧键的任何真值写法（是/true/1/yes/on）都应等价于「加入禁用列表」。
    value = raw if raw == "true" else f'"{raw}"'
    store = _store(tmp_path, f'["录制设置"]\n"虎牙是否禁用SSL证书验证(是/否)" = {value}\n')
    platforms: set[str] = set()
    if config_io.read_config_bool(store, "录制设置", "虎牙是否禁用SSL证书验证(是/否)", False):
        platforms.add("虎牙直播")
    assert platforms == {"虎牙直播"}


# ---------------- 「是否启用https录制」整合读取（旧键迁移） ----------------


def test_https_config_new_key_present(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(tmp_path, '["录制设置"]\n"是否启用https录制" = "是"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert recorder._read_https_recording_config(store) is True
    assert not store.has_option("录制设置", "是否强制启用https录制")


def test_https_config_legacy_key_migrates_yes(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(tmp_path, '["录制设置"]\n"是否强制启用https录制" = "是"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert recorder._read_https_recording_config(store) is True
    migrated = _reload(store.path)
    assert migrated.native("录制设置", "是否启用https录制") is True
    assert migrated.has_option("录制设置", "是否强制启用https录制")


def test_https_config_legacy_key_migrates_no(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(tmp_path, '["录制设置"]\n"是否强制启用https录制" = "否"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert recorder._read_https_recording_config(store) is False
    assert _reload(store.path).native("录制设置", "是否启用https录制") is False


def test_https_config_both_missing_writes_default(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(tmp_path, '["录制设置"]\nlanguage = "zh_cn"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert recorder._read_https_recording_config(store) is False
    assert _reload(store.path).native("录制设置", "是否启用https录制") is False


def test_https_config_new_key_overrides_legacy(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(
        tmp_path,
        '["录制设置"]\n"是否启用https录制" = "否"\n"是否强制启用https录制" = "是"\n',
    )
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert recorder._read_https_recording_config(store) is False


# ---------------- 「禁用SSL证书验证的平台」自动追加 ----------------


def test_sync_ssl_disable_platforms_appends_missing(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(tmp_path, '["录制设置"]\n"禁用ssl证书验证的平台(逗号分隔)" = "斗鱼直播"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    result = recorder._sync_ssl_disable_platforms(store)
    assert result == {"斗鱼直播", "虎牙直播", "B站直播"}
    assert _reload(store.path).get("录制设置", "禁用ssl证书验证的平台(逗号分隔)") == "斗鱼直播,虎牙直播,B站直播"
    assert store.get("录制设置", "禁用ssl证书验证的平台(逗号分隔)") == "斗鱼直播,虎牙直播,B站直播"


def test_sync_ssl_disable_platforms_idempotent(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    before = '["录制设置"]\n"禁用ssl证书验证的平台(逗号分隔)" = "虎牙直播,B站直播"\n'
    store = _store(tmp_path, before)
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert recorder._sync_ssl_disable_platforms(store) == {"虎牙直播", "B站直播"}
    assert store.path.read_text(encoding="utf-8") == before


def test_sync_ssl_disable_platforms_missing_key_writes_default(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(tmp_path, '["录制设置"]\nlanguage = "zh_cn"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert recorder._sync_ssl_disable_platforms(store) == set(recorder.SSL_DISABLE_REQUIRED_PLATFORMS)


# ---------------- 「language」语言键迁移读取 ----------------


def test_language_new_key_present(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    before = '["录制设置"]\nlanguage = "en_US"\n'
    store = _store(tmp_path, before)
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert config_io.read_config_value(store, "录制设置", "language", "") == "en_US"
    assert store.path.read_text(encoding="utf-8") == before


def test_language_both_missing_writes_empty_default(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = _store(tmp_path, '["录制设置"]\n"是否启用https录制" = "否"\n')
    monkeypatch.setattr(state, "config_file", str(store.path))
    assert config_io.read_config_value(store, "录制设置", "language", "") == ""
    assert _reload(store.path).get("录制设置", "language") == ""
