from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import types
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_build_exe() -> Any:
    spec = importlib.util.spec_from_file_location("_build_exe_runtime_probe", ROOT / "tool/build_exe.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_exe = _load_build_exe()


def test_verify_runtime_binaries_flags_every_missing_component(tmp_path: Path) -> None:
    problems = build_exe.verify_runtime_binaries(tmp_path)
    text = "\n".join(problems)
    assert len(problems) == 2, problems
    for name in ("ffmpeg", "node"):
        assert name in text, f"未报告缺失组件 {name}: {text}"


def test_verify_runtime_binaries_accepts_nested_bin_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / "ffmpeg").write_bytes(b"x" * 10)
    (tmp_path / "node" / "bin").mkdir(parents=True)
    (tmp_path / "node" / "bin" / "node").write_bytes(b"z" * 10)
    monkeypatch.setattr(build_exe, "_probe_runnable", lambda binary, component: None)
    assert build_exe.verify_runtime_binaries(tmp_path) == []


def test_zero_byte_runtime_binary_is_not_counted_as_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "ffmpeg").mkdir()
    (tmp_path / "ffmpeg" / "ffmpeg").write_bytes(b"")
    (tmp_path / "node").mkdir()
    (tmp_path / "node" / "node").write_bytes(b"ok")
    monkeypatch.setattr(build_exe, "_probe_runnable", lambda binary, component: None)
    problems = build_exe.verify_runtime_binaries(tmp_path)
    assert len(problems) == 1 and "ffmpeg" in problems[0], problems


def test_unrunnable_binary_is_reported_with_upstream_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for rel in ("ffmpeg/ffmpeg", "node/node"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"binary")
    fake = types.SimpleNamespace(**vars(subprocess))
    fake.run = lambda *args, **kwargs: types.SimpleNamespace(
        returncode=1, stdout=b"", stderr=b"dyld: Library not loaded: /opt/homebrew/lib/libx265.215.dylib\n"
    )
    monkeypatch.setattr(build_exe, "subprocess", fake)
    problems = build_exe.verify_runtime_binaries(tmp_path)
    assert len(problems) == 2, problems
    assert any("libx265" in p for p in problems), problems


def test_version_probe_arg_is_per_component(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for rel in ("ffmpeg/ffmpeg", "node/node"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"binary")
    argvs: list[list[str]] = []
    fake = types.SimpleNamespace(**vars(subprocess))

    def record_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        argvs.append([str(part) for part in cmd])
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    fake.run = record_run
    monkeypatch.setattr(build_exe, "subprocess", fake)
    assert build_exe.verify_runtime_binaries(tmp_path) == [], "真实探针把可用产物判成了缺件"
    flags = {Path(argv[0]).name: argv[1] for argv in argvs}
    assert flags == {"ffmpeg": "-version", "node": "--version"}, flags
    assert len(argvs) == 2, "有组件没被探测"


def test_full_build_aborts_when_runtime_incomplete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(build_exe, "install_runtime", lambda name, target: None)
    with pytest.raises(SystemExit, match="full 包运行时不可用"):
        build_exe.download_runtime_binaries(tmp_path)


def _stub_build_steps(monkeypatch: pytest.MonkeyPatch, order: list[str], tmp_path: Path) -> None:
    def _make(name: str, ret: object) -> Any:
        def _stub(*args: object, **kwargs: object) -> object:
            order.append(name)
            return ret

        return _stub

    monkeypatch.setattr(build_exe, "DIST_DIR", tmp_path / "dist")
    for name in ("run_pyinstaller", "copy_external_binaries", "download_runtime_binaries", "smoke_test"):
        monkeypatch.setattr(build_exe, name, _make(name, None))
    monkeypatch.setattr(build_exe, "_prepare_url_config", _make("_prepare_url_config", None))

    def _fake_make_zip(*args: object, **kwargs: object) -> Path:
        order.append("make_zip")
        suffix = str(kwargs.get("suffix", args[1] if len(args) > 1 else "") or "")
        zip_path = cast(Path, build_exe.DIST_DIR) / f"stub{suffix}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"PK\x03\x04")
        return zip_path

    monkeypatch.setattr(build_exe, "make_zip", _fake_make_zip)


def _zip_positions(order: list[str]) -> list[int]:
    return [i for i, name in enumerate(order) if name == "make_zip"]


def test_dual_build_zips_both_variants_before_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--dual", "--smoke"])
    build_exe.main()
    zips = _zip_positions(order)
    assert len(zips) == 2, f"--dual 应产出 lite + full 两个 zip：{order}"
    assert "smoke_test" in order, order
    smoke_at = order.index("smoke_test")
    assert all(z < smoke_at for z in zips), f"冒烟必须排在两个 zip 之后（否则日志进包）：{order}"
    assert order.index("download_runtime_binaries") < zips[-1] < smoke_at, order
    produced = sorted(p.name for p in (tmp_path / "dist").glob("*.zip"))
    assert produced == ["stub-full.zip", "stub-lite.zip"], produced


def test_dual_build_prunes_stale_zips_before_first_zip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    stale_buggy = dist / "DouyinLiveRecorder-v4.3.zip"  # with_suffix 截断缺陷时期的产物名形态
    stale_old = dist / "DouyinLiveRecorder-v4.2.0-windows-amd64-full.zip"
    user_file = dist / "release-notes.txt.zip"  # 不归本构建管辖，绝不得碰
    for victim in (stale_buggy, stale_old, user_file):
        victim.write_bytes(b"PK\x03\x04")
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--dual"])
    build_exe.main()
    assert not stale_buggy.exists(), order
    assert not stale_old.exists(), order
    assert user_file.is_file(), "清理范围越界：非本产物名的 zip 也被删了"
    assert _zip_positions(order), order


def test_no_zip_build_keeps_existing_dist_zips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    keeper = dist / "DouyinLiveRecorder-v4.2.0-windows-amd64-lite.zip"
    keeper.write_bytes(b"PK\x03\x04")
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--smoke", "--no-zip", "--no-runtime"])
    build_exe.main()
    assert keeper.is_file(), order


def test_single_build_zips_before_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--smoke"])
    build_exe.main()
    zips = _zip_positions(order)
    assert len(zips) == 1, order
    assert zips[0] < order.index("smoke_test"), f"非 dual 路径同样必须先压缩后冒烟：{order}"


def test_no_zip_build_still_smokes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--smoke", "--no-zip", "--no-runtime"])
    build_exe.main()
    assert "make_zip" not in order, order
    assert "smoke_test" in order, order


def test_ensure_utf8_streams_keeps_replace_error_policy() -> None:
    before = (getattr(sys.stdout, "errors", None), getattr(sys.stderr, "errors", None))
    build_exe._ensure_utf8_streams()
    after = (getattr(sys.stdout, "errors", None), getattr(sys.stderr, "errors", None))
    assert before == ("replace", "replace"), f"本用例依赖 pytest fd 捕获的 replace 策略，实测：{before}"
    assert after == before, f"_ensure_utf8_streams 改动了标准流错误策略：{before} -> {after}"


def test_ensure_utf8_streams_passes_explicit_errors_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    class _FakeStream:
        encoding = "cp1252"
        errors = "strict"

        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", _FakeStream())
    monkeypatch.setattr(sys, "stderr", _FakeStream())
    build_exe._ensure_utf8_streams()
    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ], f"两个标准流都必须带 errors='replace' 重配置：{calls}"


def test_release_cleaner_removes_runtime_artifacts_and_keeps_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = tmp_path / build_exe.APP_NAME
    for name in build_exe.RUNTIME_ARTIFACT_DIR_NAMES:
        (release / name).mkdir(parents=True)
        (release / name / "junk.txt").write_text("leak", encoding="utf-8")
    (release / "_internal" / "src" / "__pycache__").mkdir(parents=True)
    keep = release / "_internal" / "src" / "utils.py"
    keep.write_text("#\n", encoding="utf-8")
    cfg = release / "config"
    cfg.mkdir()
    (cfg / "config.ini").write_text("[录制设置]\n", encoding="utf-8")
    monkeypatch.setattr(build_exe, "RELEASE_DIR", release)

    removed = build_exe._clean_runtime_artifacts_from_release()

    for name in build_exe.RUNTIME_ARTIFACT_DIR_NAMES:
        assert not (release / name).exists(), f"{name}/ 未被剔除，会随 zip 外发"
    assert not (release / "_internal" / "src" / "__pycache__").exists(), "嵌套 __pycache__ 未剔除"
    assert keep.is_file() and (cfg / "config.ini").is_file(), "清理误伤发布载荷（源码/配置模板）"
    assert {p.replace("\\", "/") for p in removed} == {
        "logs/",
        "backup_config/",
        "downloads/",
        "_internal/src/__pycache__/",
    }, removed


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        (f"{build_exe.APP_NAME}/logs/streamget.log", True),
        (f"{build_exe.APP_NAME}\\logs\\PlayURL.log", True),
        (f"{build_exe.APP_NAME}/downloads/x/1.ts", True),
        (f"{build_exe.APP_NAME}/_internal/src/__pycache__/utils.cpython-314.pyc", True),
        ("logs/web_console.log", True),
        (f"{build_exe.APP_NAME}/config/config.ini", False),
        (f"{build_exe.APP_NAME}/_internal/src/support/utils.py", False),
        (f"{build_exe.APP_NAME}/_internal/web/logs_note.js", False),
        (f"{build_exe.APP_NAME}/", False),
    ],
)
def test_zip_member_runtime_artifact_predicate(member: str, expected: bool) -> None:
    assert build_exe._zip_member_is_runtime_artifact(member) is expected


def _fake_archive_with(members: list[str]) -> Any:
    def _fake(zip_path: Path) -> None:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for member in members:
                zf.writestr(member, "x")

    return _fake


def _stub_make_zip_inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, members: list[str]) -> None:
    release = tmp_path / build_exe.APP_NAME
    release.mkdir(parents=True)
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setattr(build_exe, "RELEASE_DIR", release)
    monkeypatch.setattr(build_exe, "DIST_DIR", dist)
    monkeypatch.setattr(build_exe, "assert_no_credentials_in_release", lambda: None)
    monkeypatch.setattr(build_exe, "_clean_runtime_artifacts_from_release", lambda: [])
    monkeypatch.setattr(build_exe, "_zip_release_dir", _fake_archive_with(members))


def test_make_zip_aborts_when_archive_still_contains_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_make_zip_inputs(monkeypatch, tmp_path, [f"{build_exe.APP_NAME}/logs/streamget.log"])
    with pytest.raises(SystemExit) as caught:
        build_exe.make_zip("9.9.9")
    assert "logs/streamget.log" in str(caught.value)


def test_make_zip_accepts_clean_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_make_zip_inputs(
        monkeypatch,
        tmp_path,
        [f"{build_exe.APP_NAME}/config/config.ini", f"{build_exe.APP_NAME}/_internal/a.py"],
    )
    zip_path = build_exe.make_zip("9.9.9")
    assert zip_path.is_file()


@pytest.mark.parametrize("suffix,tail", [("-lite", "lite"), ("-full", "full")])
def test_make_zip_name_keeps_dotted_version_and_variant_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str, tail: str
) -> None:
    _stub_make_zip_inputs(monkeypatch, tmp_path, [f"{build_exe.APP_NAME}/_internal/a.py"])
    zip_path = build_exe.make_zip("4.3.0", suffix=suffix)
    pattern = rf"DouyinLiveRecorder-v4\.3\.0-(windows|macos|linux)-[a-z0-9_]+-{tail}\.zip"
    assert re.fullmatch(pattern, zip_path.name), zip_path.name


def test_dual_suffixes_do_not_collide_on_one_zip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_make_zip_inputs(monkeypatch, tmp_path, [f"{build_exe.APP_NAME}/_internal/a.py"])
    lite = build_exe.make_zip("4.3.0", suffix="-lite")
    full = build_exe.make_zip("4.3.0", suffix="-full")
    assert lite != full
    produced = sorted(p.name for p in (tmp_path / "dist").glob("*.zip"))
    assert len(produced) == 2, produced
    assert lite.is_file() and full.is_file()


def test_make_zip_refuses_name_containing_path_separator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_make_zip_inputs(monkeypatch, tmp_path, [f"{build_exe.APP_NAME}/_internal/a.py"])
    for version, suffix in (("4.3.0/escape", ""), ("4.3.0", "-lite/escape")):
        with pytest.raises(SystemExit) as caught:
            build_exe.make_zip(version, suffix=suffix)
        assert "路径分隔符" in str(caught.value), (version, suffix)
        assert not (tmp_path / "dist" / "DouyinLiveRecorder-v4.3.0").exists(), (version, suffix)
        assert list((tmp_path / "dist").glob("*.zip")) == [], (version, suffix)
        assert list(tmp_path.glob("*.zip")) == [], (version, suffix)


def test_dual_and_no_zip_are_mutually_exclusive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--dual", "--no-zip", "--smoke"])
    with pytest.raises(SystemExit) as caught:
        build_exe.main()
    assert "--dual 与 --no-zip 互斥" in str(caught.value)
    assert order == [], order
    assert list((tmp_path / "dist").glob("*.zip")) == [], "互斥判定必须早于清理，否则报错前已经删过文件"


def _stub_two_colliding_zips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order: list[str]) -> Path:
    victim = tmp_path / "dist" / "DouyinLiveRecorder-v4.3.zip"
    victim.parent.mkdir(parents=True, exist_ok=True)

    def _fake(*args: object, **kwargs: object) -> Path:
        order.append("make_zip")
        victim.write_bytes(b"PK\x03\x04")
        return victim

    monkeypatch.setattr(build_exe, "make_zip", _fake)
    return victim


def test_dual_build_aborts_when_both_variants_land_on_one_zip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    victim = _stub_two_colliding_zips(monkeypatch, tmp_path, order)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--dual"])
    with pytest.raises(SystemExit) as caught:
        build_exe.main()
    assert "同一个文件" in str(caught.value), str(caught.value)
    assert len(_zip_positions(order)) == 2, order


def test_dual_build_aborts_when_a_variant_zip_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    _stub_build_steps(monkeypatch, order, tmp_path)
    made: list[Path] = []

    def _fake(*args: object, **kwargs: object) -> Path:
        order.append("make_zip")
        suffix = str(kwargs.get("suffix", "") or "")
        zip_path = cast(Path, build_exe.DIST_DIR) / f"stub{suffix}.zip"
        if suffix != "-full":  # full 只报成功不落件
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            zip_path.write_bytes(b"PK\x03\x04")
        made.append(zip_path)
        return zip_path

    monkeypatch.setattr(build_exe, "make_zip", _fake)
    monkeypatch.setattr(sys, "argv", ["tool/build_exe.py", "--dual"])
    with pytest.raises(SystemExit) as caught:
        build_exe.main()
    assert "不存在" in str(caught.value), str(caught.value)
    assert [p.name for p in made] == ["stub-lite.zip", "stub-full.zip"], made


def test_clean_stale_release_zips_only_touches_own_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    mine = [dist / "DouyinLiveRecorder-v4.3.zip", dist / "DouyinLiveRecorder-v4.2.0-linux-x86_64-full.zip"]
    keepers = [dist / "notes.zip", dist / "DouyinLiveRecorder.txt"]
    for victim in mine + keepers:
        victim.write_bytes(b"x")
    monkeypatch.setattr(build_exe, "DIST_DIR", dist)
    removed = build_exe._clean_stale_release_zips()
    assert sorted(removed) == ["DouyinLiveRecorder-v4.2.0-linux-x86_64-full.zip", "DouyinLiveRecorder-v4.3.zip"]
    assert not any(victim.exists() for victim in mine)
    assert all(victim.exists() for victim in keepers)
    assert build_exe._clean_stale_release_zips() == []
    missing = tmp_path / "no-such-dist"
    monkeypatch.setattr(build_exe, "DIST_DIR", missing)
    assert build_exe._clean_stale_release_zips() == []


def _ignore_patterns(name: str) -> list[str]:
    text = (ROOT / name).read_text(encoding="utf-8-sig")
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


@pytest.mark.parametrize("ignore_file", [".dockerignore", ".gitignore"])
def test_ignore_files_have_no_inline_comments(ignore_file: str) -> None:
    patterns = _ignore_patterns(ignore_file)
    offenders = [
        f"{ignore_file}:{no} -> {line!r}" for no, line in enumerate(patterns, 1) if " #" in line or "\t#" in line
    ]
    assert not offenders, "以下忽略规则把注释写在了行内，模式永不匹配（须移到独立行）：\n" + "\n".join(offenders)


def test_dockerignore_excludes_dev_only_artifacts_as_bare_patterns() -> None:
    patterns = _ignore_patterns(".dockerignore")
    for must in ("tool/build_exe.py", "*.jsonl"):
        assert must in patterns, f".dockerignore 缺少裸模式 {must!r}（带行内注释的规则永不匹配）"


def test_gitignore_runtime_dirs_are_bare_patterns() -> None:
    patterns = _ignore_patterns(".gitignore")
    for must in ("node/", "node-v*.zip", "backup_config/", "logs/", "*.jsonl"):
        assert must in patterns, f".gitignore 缺少裸模式 {must!r}"
    assert "!node/" not in patterns and "!logs/" not in patterns and "!backup_config/" not in patterns


_SMOKE_SCRIPT = ROOT / "tool" / "smoke_test.py"


def _run_smoke_script(tmp_path: Path, content: str) -> int:
    cfg = tmp_path / "cfg.json"
    cfg.write_text(content, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SMOKE_SCRIPT), "-c", str(cfg)],
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
        cwd=str(ROOT),
        check=False,
    )
    rc: int = proc.returncode
    return rc


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"base_url": "http://127.0.0.1:9", "checks": []}', 2),
        ('{"base_url": "http://127.0.0.1:9"}', 2),
        ("[]", 2),
        ('[{"url": "http://127.0.0.1:9/none", "expected_status": 418, "name": "closed port"}]', 1),
    ],
    ids=["empty-checks", "missing-checks-key", "empty-top-level", "one-real-check-fails"],
)
def test_smoke_script_exit_codes_on_empty_config(tmp_path: Path, content: str, expected: int) -> None:
    assert _run_smoke_script(tmp_path, content) == expected


def test_smoke_load_config_still_reads_valid_checks(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("_smoke_cfg_probe", _SMOKE_SCRIPT)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    cfg = tmp_path / "ok.json"
    cfg.write_text('{"base_url": "http://127.0.0.1:9", "checks": [{"url": "/health"}]}', encoding="utf-8")
    checks, base_url = smoke.load_config(str(cfg))
    assert base_url == "http://127.0.0.1:9"
    assert checks == [{"url": "/health"}]
    bad = tmp_path / "bad.json"
    bad.write_text('{"base_url": "http://127.0.0.1:9"}', encoding="utf-8")
    with pytest.raises(ValueError):
        smoke.load_config(str(bad))


def _production_import_names() -> set[str]:
    files = [
        ROOT / name for name in ("src/main.py", "src/web.py", "src/support/i18n.py", "src/notifications/msg_push.py")
    ]
    files += sorted((ROOT / "src").rglob("*.py"))
    names: set[str] = set()
    for path in files:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
    return names


def test_bloat_excludes_never_hide_a_module_that_production_imports() -> None:
    imported = _production_import_names()
    collisions = sorted(
        name
        for excluded in build_exe.BLOAT_EXCLUDES
        for name in imported
        if name == excluded or name.startswith(excluded + ".")
    )
    assert not collisions, f"排除项与生产导入冲突（冻结后会 ModuleNotFoundError）：{collisions}"


def test_bloat_excludes_still_cover_the_measured_bloat() -> None:
    for required in (
        "pydantic.v1.mypy",  # 拖入整个 mypy 包（0.71MB / 69 文件）
        "PIL._avif",  # 7.52MB，产物内最大的单个非解释器文件
        "PIL._imagingft",  # 2.07MB
        "httptools",  # 0.17MB
        "watchfiles",  # 0.61MB
        "nodejs_wheel",  # 114MB：venv 里有、requirements.txt 没有
    ):
        assert required in build_exe.BLOAT_EXCLUDES, f"体积排除清单缺 {required!r}，产物体积将回弹"


def test_every_analysis_applies_the_bloat_excludes() -> None:
    rendered = build_exe.SPEC_TEMPLATE.format(
        app=build_exe.APP_NAME,
        bloat_excludes=list(build_exe.BLOAT_EXCLUDES),
        i18n_datas=build_exe.i18n_datas_entries(),
    )
    assert rendered.count("excludes=excludes_bloat +") == 2, "两个 Analysis 必须都挂上排除清单"
    spec = ast.parse(rendered)
    datas = next(node for node in spec.body if isinstance(node, ast.Assign) and node.targets[0].id == "datas")
    entries = [ast.literal_eval(item) for item in datas.value.elts if isinstance(item, ast.Tuple)]
    assert ("pyproject.toml", ".") in entries
    assert ("src/external_resources.json", "src") in entries


def test_i18n_datas_include_all_json_catalogs() -> None:
    entries = build_exe.i18n_datas_entries()
    assert set(entries) == {(f"i18n/{lang}.json", "i18n") for lang in ("en_US",)}


def test_zip_release_dir_prefixes_members_with_app_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    release = tmp_path / build_exe.APP_NAME
    (release / "config").mkdir(parents=True)
    (release / "config" / "config.ini").write_text("[录制设置]\n", encoding="utf-8")
    monkeypatch.setattr(build_exe, "RELEASE_DIR", release)
    zip_path = tmp_path / "out.zip"
    build_exe._zip_release_dir(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert f"{build_exe.APP_NAME}/config/config.ini" in names
        assert all(n.startswith(f"{build_exe.APP_NAME}/") for n in names), names
        info = zf.getinfo(f"{build_exe.APP_NAME}/config/config.ini")
        assert info.compress_type == zipfile.ZIP_DEFLATED


@pytest.mark.parametrize("exit_code", [None, 0, 1])
def test_smoke_requires_process_to_remain_alive(monkeypatch: pytest.MonkeyPatch, exit_code: int | None) -> None:
    class Process:
        returncode = exit_code

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self, timeout: int) -> tuple[str, None]:
            return "ready", None

    proc = Process()
    killed = []
    monkeypatch.setattr(build_exe, "_kill_tree", killed.append)
    if exit_code is None:
        build_exe._finish(proc, "cli")
        assert killed == [proc]
    else:
        with pytest.raises(RuntimeError, match="进程提前退出"):
            build_exe._finish(proc, "cli")
        assert not killed


def test_smoke_rejects_output_collection_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode = None

        def poll(self) -> None:
            return None

        def communicate(self, timeout: int) -> tuple[str, None]:
            raise subprocess.TimeoutExpired("smoke", timeout)

    proc = Process()
    killed = []
    monkeypatch.setattr(build_exe, "_kill_tree", killed.append)
    with pytest.raises(RuntimeError, match="进程收尾超时"):
        build_exe._finish(proc, "cli")
    assert killed == [proc]
