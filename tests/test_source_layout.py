"""源码目录迁移后的入口顺序和运行时资源定位。"""

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import src
import src.recording
from src.runtime import node as node_install
from src.support import logger as logger_module

ROOT = Path(__file__).resolve().parent.parent


def test_package_import_leaves_entry_initialization_to_entry(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import src; "
            "assert 'src.support.logger' not in sys.modules; assert 'src.runtime.node' not in sys.modules",
            str(ROOT),
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("frozen", [False, True])
def test_source_and_frozen_entries_keep_resources_at_app_root(monkeypatch: pytest.MonkeyPatch, frozen: bool) -> None:
    process = types.SimpleNamespace(**vars(sys))
    process.argv = [str(ROOT / "src" / "main.py")]
    process.executable = str(ROOT / "DouyinLiveRecorder")
    process.frozen = frozen
    monkeypatch.setattr(logger_module, "sys", process)
    assert Path(logger_module._app_root()) == ROOT


@pytest.mark.parametrize("skip, expected_calls", [("1", []), ("", [True])])
def test_runtime_initialization_respects_skip_and_bundled_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip: str, expected_calls: list[bool]
) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    monkeypatch.setattr(logger_module, "script_path", str(tmp_path))
    calls: list[bool] = []
    monkeypatch.setattr(node_install, "check_node", lambda: calls.append(True))
    monkeypatch.setenv("DOUYIN_SKIP_RUNTIME_CHECK", skip)
    monkeypatch.setenv("PATH", str(node_dir))
    src.initialize_runtime()
    assert calls == expected_calls
    assert os.environ["PATH"] == str(node_dir)


@pytest.mark.parametrize(
    "module",
    [
        "main",
        "config.io",
        "support.utils",
        "notifications.notify",
        "recording.status",
        "streams.dispatch",
        "recording.session",
        "recording.engine",
    ],
)
def test_modules_import_without_starting_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    monkeypatch.setenv("DOUYIN_LOG_NO_ENQUEUE", "1")
    monkeypatch.setenv("DOUYIN_SKIP_RUNTIME_CHECK", "1")
    monkeypatch.setenv("DOUYIN_DISABLE_LOG_ARCHIVE", "1")
    code = """
import builtins
import importlib
import signal
import sys
import threading
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
module = sys.argv[2]
sys.argv[0] = str(Path.cwd() / 'check.py')
if module != 'main':
    importlib.import_module('src.support.logger')
printing = builtins.print
handlers = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
threads = set(threading.enumerate())
importlib.import_module('src.' + module)
assert builtins.print is printing
assert [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)] == handlers
assert set(threading.enumerate()) == threads
assert not Path('backup_config').exists()
if module == 'main':
    assert 'src.recording.engine' not in sys.modules
else:
    assert 'src.main' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT), module],
        cwd=tmp_path,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.parametrize("as_module", [False, True])
def test_cli_initializes_encoding_and_engine_in_order(monkeypatch: pytest.MonkeyPatch, as_module: bool) -> None:
    import runpy

    from src import main
    from src.support import console

    calls: list[str] = []
    engine = types.ModuleType("src.recording.engine")
    engine.initialize_process = lambda *, cli: calls.append(f"initialize:{cli}")
    engine.main = lambda: calls.append("record")
    monkeypatch.setattr(console, "configure_encoding", lambda: calls.append("encoding"))
    monkeypatch.setattr(src.recording, "engine", engine, raising=False)
    monkeypatch.setitem(sys.modules, "src.recording.engine", engine)
    # 统一入口的两处外部依赖：配置文件迁移与面板开关，替身避免读写真实配置。
    monkeypatch.setattr("src.config.store.ensure_config_migrated", lambda *args, **kwargs: False)
    monkeypatch.setattr("src.api.config.read_web_config", lambda _path: {"web_enable": False})
    if as_module:
        monkeypatch.delitem(sys.modules, "src.main", raising=False)
        runpy.run_module("src.main", run_name="__main__")
    else:
        main.main()
    assert calls == ["encoding", "initialize:True", "record"]


def test_source_module_dependencies_have_no_cycles() -> None:
    import ast

    modules = {
        ".".join(path.relative_to(ROOT).with_suffix("").parts): path
        for path in (ROOT / "src").rglob("*.py")
        if path.name != "__init__.py"
    }
    dependencies: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                dependencies[name].update(alias.name for alias in node.names if alias.name in modules)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    package = ".".join(name.split(".")[: -node.level])
                    prefix = package + ("." + prefix if prefix else "")
                if prefix in modules:
                    dependencies[name].add(prefix)
                else:
                    dependencies[name].update(
                        prefix + "." + alias.name for alias in node.names if prefix + "." + alias.name in modules
                    )

    visited: set[str] = set()
    pending: list[str] = []

    def visit(name: str) -> None:
        assert name not in pending, "循环依赖: " + " → ".join([*pending, name])
        if name in visited:
            return
        pending.append(name)
        for dependency in sorted(dependencies[name]):
            visit(dependency)
        pending.pop()
        visited.add(name)

    for name in sorted(modules):
        visit(name)
