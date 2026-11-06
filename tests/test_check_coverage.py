# tool/check_coverage.py 的数据、模块阈值与命令行退出码回归用例。

import contextlib
import importlib.util
import subprocess
import sys
import tomllib
import types
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_check_coverage() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_check_coverage_probe", ROOT / "tool" / "check_coverage.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_coverage: Any = _load_check_coverage()  # 动态加载的检查脚本：属性由 mypy 视角不可见，按 Any 处理

# 登记表 + 白名单的全部模块：构造临时 root 时以它为「已登记面」，用例再各自追加新模块。
_REGISTERED = tuple(check_coverage.MODULE_THRESHOLDS) + tuple(check_coverage.GATE_EXEMPT_MODULES)


def _make_root(tmp_path: Path, extra_modules: tuple[str, ...] = ()) -> Path:
    # 造一棵最小「项目根」：src/ 下含全部登记表模块 + 本用例追加的模块。
    # 只建判定所需的东西（check_coverage 按文件系统枚举模块），
    # 不复制真实源码——复制就会让「登记表指向磁盘模块」这类断言变成自证。
    for module in _REGISTERED + tuple(extra_modules):
        path = tmp_path / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 占位模块（仅供检查枚举，不执行）\n", encoding="utf-8")
    return tmp_path


def _report(percent_by_module: dict[str, float], *, backslash_keys: bool = False) -> dict[str, Any]:
    # 造 coverage JSON 报告。backslash_keys=True 时按 Windows 形态出题
    # （coverage json 在 Windows 上确实以反斜杠为键，尾部匹配规则必须吃得住）。
    files: dict[str, Any] = {}
    for module, pct in percent_by_module.items():
        key = module.replace("/", "\\") if backslash_keys else module
        files[key] = {"summary": {"percent_covered": pct}}
    return {"files": files, "totals": {"percent_covered": 0.0}}


def _full_pass_report() -> dict[str, Any]:
    return _report(dict.fromkeys(_REGISTERED, 100.0))


# ---------------------------------------------------------------------------
# E) 登记表自身的质量（表空 / 键腐烂都会让「判定面」悄悄缩小）
# ---------------------------------------------------------------------------


def test_registered_threshold_table_is_nonempty_and_points_at_real_modules() -> None:
    # 表为空 → evaluate_report 只会按下限评全部模块，登记语义静默消失；
    # 键指向不存在的模块 → 检查配置腐烂（模块改名后表没跟着改）。
    assert check_coverage.MODULE_THRESHOLDS, "MODULE_THRESHOLDS 不得为空（AGENTS.md 认定它是阈值唯一事实源）"
    for module, threshold in check_coverage.MODULE_THRESHOLDS.items():
        assert (ROOT / module).is_file(), f"登记表阈值指向不存在的模块: {module}"
        assert 0 < threshold <= 100, f"{module} 的阈值不合理: {threshold}"


def test_exempt_whitelist_entries_are_justified_and_real() -> None:
    # 白名单是 MID-2259 之后的「第三条路」，条目必须写明理由且指向真实模块，
    # 否则它会重新变成「自动豁免」的旁门（正是本检查要消灭的形态）。
    assert isinstance(check_coverage.GATE_EXEMPT_MODULES, dict)
    for module, reason in check_coverage.GATE_EXEMPT_MODULES.items():
        assert (ROOT / module).is_file(), f"豁免名单指向不存在的模块: {module}"
        assert isinstance(reason, str) and len(reason.strip()) >= 8, f"{module} 的豁免理由缺失或过短: {reason!r}"
        assert module not in check_coverage.MODULE_THRESHOLDS, f"{module} 同时被登记阈值又豁免，语义冲突"


def test_global_floor_is_not_looser_than_project_fail_under() -> None:
    # 逐模块闸不得比 pyproject [tool.coverage.report].fail_under（全库总闸）更宽，
    # 否则「新模块零测试」能过逐模块闸、只被总闸拦下——判定面就漏了一半。
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    fail_under = float(pyproject["tool"]["coverage"]["report"]["fail_under"])
    assert float(check_coverage.GLOBAL_FLOOR) >= fail_under, (
        f"GLOBAL_FLOOR={check_coverage.GLOBAL_FLOOR} 低于全库 fail_under={fail_under}：新模块可自动豁免"
    )


# ---------------------------------------------------------------------------
# B) MID-2259：未登记模块不再自动豁免
# ---------------------------------------------------------------------------


def test_new_src_module_is_gated_even_without_registered_threshold(tmp_path: Path) -> None:
    # 核心回归锁：把 evaluate_report 改回「只遍历 MODULE_THRESHOLDS」，本条立刻变红
    # （旧形态下 34 个 src/*.py 里只有 6 个被评，其余自动免检）。
    root = _make_root(tmp_path, ("src/brand_new.py",))
    rc = check_coverage.evaluate_report(_full_pass_report(), root)
    assert rc == 1, "src/brand_new.py 未出现在报告里却被放行 = 新增模块自动豁免（MID-2259 回归）"


def test_unregistered_module_below_global_floor_exits_1(tmp_path: Path) -> None:
    root = _make_root(tmp_path, ("src/brand_new.py",))
    floor = float(check_coverage.GLOBAL_FLOOR)
    percents = dict.fromkeys(_REGISTERED, 100.0)
    percents["src/brand_new.py"] = floor - 1.0
    assert check_coverage.evaluate_report(_report(percents), root) == 1, "低于下限却放行：GLOBAL_FLOOR 未生效"


def test_unregistered_module_at_global_floor_passes(tmp_path: Path) -> None:
    # 与上一条分开成例：同一用例里两次判定会共用被 monkeypatch 改过的模块状态（见文件头）。
    # 恰好等于下限必须判达标（>= 语义），否则「按下限放水」这条规则本身就不可用。
    root = _make_root(tmp_path, ("src/brand_new.py",))
    percents = dict.fromkeys(_REGISTERED, 100.0)
    percents["src/brand_new.py"] = float(check_coverage.GLOBAL_FLOOR)
    assert check_coverage.evaluate_report(_report(percents), root) == 0, "恰好等于下限应判达标（>= 语义）"


def test_registered_module_still_uses_its_own_higher_threshold(tmp_path: Path) -> None:
    # 反向锁：登记了 85% 的模块不得被 50% 的下限放水（否则「登记」这层毫无意义）。
    root = _make_root(tmp_path)
    ttwid_threshold = check_coverage.MODULE_THRESHOLDS["src/streams/ttwid.py"]
    assert ttwid_threshold > check_coverage.GLOBAL_FLOOR, "用例前提失效：ttwid 阈值须高于全局下限"
    percents = dict.fromkeys(_REGISTERED, 100.0)
    percents["src/streams/ttwid.py"] = ttwid_threshold - 1.0
    assert check_coverage.evaluate_report(_report(percents), root) == 1


def test_exempt_module_does_not_block_the_gate(tmp_path: Path) -> None:
    # 豁免只作用于该模块自身，不得顺手放行别的缺口（否则白名单就成了总开关）。
    root = _make_root(tmp_path)
    percents = {module: 100.0 for module in _REGISTERED if module not in check_coverage.GATE_EXEMPT_MODULES}
    rc = check_coverage.evaluate_report(_report(percents), root)
    assert rc == 0, "被豁免模块缺席报告即判失败：豁免名单没起作用"


# ---------------------------------------------------------------------------
# A) MIN-19 / fail-closed：拿不到输入一律 rc=2 + 下一步命令
# ---------------------------------------------------------------------------


def test_empty_files_report_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        check_coverage.evaluate_report({"files": {}})
    assert excinfo.value.code == 2, "files 为空必须按「检查没跑」处理，不得继续判定"
    err = capsys.readouterr().err
    assert "--cov=src" in err, f"rc=2 必须打印下一步命令: {err}"


def test_missing_data_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        check_coverage.check_coverage(data_file=str(tmp_path / "no-such-.coverage"), root=tmp_path)
    assert excinfo.value.code == 2
    assert "--cov=src" in capsys.readouterr().err


def test_root_without_src_dir_exits_2(tmp_path: Path) -> None:
    # 判定面为空（root 解析错位、src/ 被误删）→ 旧形态会打印「PASSED: All 0 module(s)」。
    with pytest.raises(SystemExit) as excinfo:
        check_coverage.evaluate_report(_report({"src/streams/spider.py": 100.0}), tmp_path)
    assert excinfo.value.code == 2, "枚举不到任何模块却继续判定 = 空检查自证通过"


def test_stale_table_entry_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 登记表指向不存在的模块（改名后的残留）→ 检查配置腐烂，按 rc=2 而不是 rc=1：
    # 让人去改表，而不是误以为「补测试就能变绿」。
    root = _make_root(tmp_path)
    broken = dict(check_coverage.MODULE_THRESHOLDS)
    broken["src/renamed_away.py"] = 60.0
    monkeypatch.setattr(check_coverage, "MODULE_THRESHOLDS", broken)
    with pytest.raises(SystemExit) as excinfo:
        check_coverage.evaluate_report(_full_pass_report(), root)
    assert excinfo.value.code == 2
    assert "src/renamed_away.py" in capsys.readouterr().err


def test_stale_table_entry_exits_2_then_clean_table_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 「rc=2 是因为那条坏键」而不是「这份报告本来就该 rc=2」——必须两次判定才能证明。
    # 还原用 ExitStack 当场完成：留给用例 teardown 会让第二次判定仍读到被改坏的表，
    # 于是本断言与上一条互相串味、失败原因指向错的地方。
    root = _make_root(tmp_path)
    report = _full_pass_report()
    broken = dict(check_coverage.MODULE_THRESHOLDS)
    broken["src/renamed_away.py"] = 60.0
    with contextlib.ExitStack() as stack:
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        patch.setattr(check_coverage, "MODULE_THRESHOLDS", broken)
        with pytest.raises(SystemExit) as excinfo:
            check_coverage.evaluate_report(report, root)
        assert excinfo.value.code == 2
        assert "src/renamed_away.py" in capsys.readouterr().err
    assert check_coverage.evaluate_report(report, root) == 0, "还原真表后仍 rc!=0：rc=2 的归因不成立（坏键以外的原因）"


def test_unparseable_report_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 「数据文件在、coverage json 生成失败」这一类必须走 rc=2（MIN-19 明列的第三形态）。
    # 只桩 coverage 子进程这一层（返回非 0、且不落 JSON），判定逻辑仍走真实实现。
    root = _make_root(tmp_path)
    data = root / ".coverage"
    data.write_bytes(b"not-really-coverage-data")

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr="no data")

    # 只换**检查模块自己的** subprocess 全局引用，不动 stdlib 模块本体（patch subprocess
    # 会波及同进程 harness 的守护线程与其它用例，AGENTS.md「patch main 的 subprocess」同族约定）。
    shim = types.SimpleNamespace(**vars(subprocess))
    shim.run = fake_run
    monkeypatch.setattr(check_coverage, "subprocess", cast(types.SimpleNamespace, shim))
    with pytest.raises(SystemExit) as excinfo:
        check_coverage.check_coverage(data_file=str(data), root=root)
    assert excinfo.value.code == 2, "报告不可解析必须按「检查没数据」处理，不得继续判定"
    assert "--cov=src" in capsys.readouterr().err


def test_missing_summary_counts_as_zero(tmp_path: Path) -> None:
    # 报告里有文件名条目却没有 summary（数据被截断）→ 按 0% 处理，绝不当满分。
    root = _make_root(tmp_path)
    report: dict[str, Any] = {"files": {module: {} for module in _REGISTERED}, "totals": {}}
    assert check_coverage.evaluate_report(report, root) == 1


# ---------------------------------------------------------------------------
# D) 审查 6.7 的尾部匹配收紧（防同名顶替）
# ---------------------------------------------------------------------------


def test_find_module_coverage_rejects_tests_only_key(tmp_path: Path) -> None:
    _make_root(tmp_path)
    report = _report({"tests/utils.py": 99.5})
    assert check_coverage._find_module_coverage(report, "src/support/utils.py") is None, (
        "tests/utils.py 顶替了 src/support/utils.py 的读数（6.7 的形态回来了）"
    )


def test_tests_only_report_exits_1(tmp_path: Path) -> None:
    # 顶替不成立时整份报告必须整体判失败，而不是「查不到就算过」。
    root = _make_root(tmp_path)
    assert check_coverage.evaluate_report(_report({"tests/utils.py": 99.5}), root) == 1


def test_windows_backslash_report_keys_match(tmp_path: Path) -> None:
    # 正向锁：收紧匹配不得把合法形态一起杀掉——coverage json 在 Windows 上就是反斜杠键。
    root = _make_root(tmp_path)
    assert check_coverage.evaluate_report(_report(dict.fromkeys(_REGISTERED, 100.0), backslash_keys=True), root) == 0


def test_absolute_ci_report_keys_match(tmp_path: Path) -> None:
    # CI（Linux runner）上 coverage 写绝对路径键，尾部匹配规则同样必须命中。
    root = _make_root(tmp_path)
    absolute = _report({f"/__w/repo/{module}": 100.0 for module in _REGISTERED})
    assert check_coverage.evaluate_report(absolute, root) == 0


def test_real_src_tree_enumeration_is_populated_and_classified() -> None:
    # 判定面见证：真仓 src/ 下确实被枚举到（不是 6 个、也不是 0 个），
    # 且每个模块都能拿到阈值或落进白名单——否则「全部模块」这句话说不上。
    modules = check_coverage._src_modules(ROOT)
    assert modules, "src/ 模块枚举为空"
    assert all(m.startswith("src/") and m.endswith(".py") for m in modules)
    for module in modules:
        threshold, source = check_coverage._threshold_for(module)
        assert 0 < threshold <= 100, f"{module} 的阈值异常: {threshold} ({source})"


# ---------------------------------------------------------------------------
# F) CLI 出口：三种 rc 都必须原样透出去
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate_rc", [0, 1, 2])
def test_cli_main_exits_with_the_gate_rc(gate_rc: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # 上面几条直调判定函数，这组锁 **CLI 出口**：main() 必须把 rc 原样 sys.exit 出去。
    # 若有人把 `sys.exit(check_coverage(...))` 改成 `return`，脚本会恒以 0 结束——
    # 检查在 CI 里就变成一行装饰性输出（MIN-19 想消灭的正是这一族）。
    # 这里只桩「取数据 + 判定」这一层（真实判定逻辑由上面各条分别锁），每个参数化实例只调一次
    # main()，因此不触发文件头的单次判定口径。
    seen: dict[str, Any] = {}

    def fake_check_coverage(data_file: str | None = None) -> int:
        seen["data_file"] = data_file
        return gate_rc

    monkeypatch.setattr(check_coverage, "check_coverage", fake_check_coverage)
    monkeypatch.setattr(sys, "argv", ["check_coverage.py", "--data-file", "given/path"])
    with pytest.raises(SystemExit) as excinfo:
        check_coverage.main()
    assert excinfo.value.code == gate_rc, f"rc={gate_rc} 未原样透出（退出码被改写 = 检查被旁路）"
    assert seen["data_file"] == "given/path", "--data-file 必须透传到判定层"


def test_cli_entrypoint_propagates_rc2_for_unusable_data(tmp_path: Path) -> None:
    # 真实子进程跑一遍脚本，锁「rc=2 真的成了进程退出码」——in-process 那组只能证 sys.exit
    # 被调用，证不了 SystemExit 未被某处 except 吞掉。
    # 输出**按字节**判定（不用 text=True/encoding=）：中文 Windows 下子进程 stderr 是码页
    # 编码的本地化文本，让解码进入判定路径就会在 UTF-8 检查下炸 reader 线程。
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tool" / "check_coverage.py"), "--data-file", str(tmp_path / "absent")],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode == 2, f"CLI 未透出检查失效码：rc={proc.returncode}\n{proc.stdout!r}\n{proc.stderr!r}"
    assert b"--cov=src" in proc.stderr, proc.stderr
