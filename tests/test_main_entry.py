# 单入口回归锁：`python -m src.main` 按 config.toml 的 [Web] web_enable 决定是否同时启动面板。

import sys
import types
from pathlib import Path

import pytest

import src.recording
import src.web
from src import main as entry
from src.api import config as api_config
from src.support import console


def _write_toml(tmp_path: Path, web_enable: bool | None) -> Path:
    path = tmp_path / "config.toml"
    body = "[Web]\n"
    if web_enable is not None:
        body += f"web_enable = {str(web_enable).lower()}\n"
    path.write_text(body, encoding="utf-8")
    return path


def test_panel_disabled_when_key_missing(tmp_path: Path) -> None:
    assert api_config.read_web_config(_write_toml(tmp_path, None))["web_enable"] is False


def test_panel_enabled_by_toml(tmp_path: Path) -> None:
    assert api_config.read_web_config(_write_toml(tmp_path, True))["web_enable"] is True


def _stub_entry_dependencies(monkeypatch: pytest.MonkeyPatch, calls: list[str], web_enable: bool) -> None:
    monkeypatch.setattr(console, "configure_encoding", lambda: calls.append("encoding"))
    monkeypatch.setattr("src.config.store.ensure_config_migrated", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "src.api.config.read_web_config",
        lambda _path: {"web_enable": web_enable},
    )


def _stub_engine(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    engine = types.ModuleType("src.recording.engine")
    engine.initialize_process = lambda *, cli: calls.append(f"initialize:{cli}")
    engine.main = lambda: calls.append("record")
    monkeypatch.setattr(src.recording, "engine", engine, raising=False)
    monkeypatch.setitem(sys.modules, "src.recording.engine", engine)


def test_main_runs_panel_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _stub_entry_dependencies(monkeypatch, calls, web_enable=True)
    monkeypatch.setattr(src.web, "run", lambda: calls.append("web"))
    _stub_engine(monkeypatch, calls)

    entry.main()

    assert calls == ["encoding", "web"]


def test_main_runs_cli_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _stub_entry_dependencies(monkeypatch, calls, web_enable=False)
    monkeypatch.setattr(src.web, "run", lambda: calls.append("web"))
    _stub_engine(monkeypatch, calls)

    entry.main()

    assert calls == ["encoding", "initialize:True", "record"]
