#!/usr/bin/env python3
# 读取本次 pytest-cov 数据，对 src/ 每个模块检查覆盖率阈值。
# 退出码：0 达标；1 未达标；2 数据缺失、报告不可解析或模块登记无效。

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import cast

# 项目根目录（tool/ 的上一级）：模块枚举以它为基准
ROOT = Path(__file__).resolve().parent.parent

# 兜底数据文件名：优先级（显式 --data-file > $COVERAGE_FILE > 本常量）见 _resolve_data_file，
# 与 coverage 自身取数据文件的顺序一致——本检查才评得上刚被 pytest --cov 写下的那份数据。
DEFAULT_DATA_FILE = ".coverage"

# 无数据时的统一提示：告诉调用者**下一步命令**，而不是只说「失败了」。
NO_DATA_HINT = (
    "先执行一次带覆盖率的测试再跑本检查：\n"
    "     .venv/Scripts/python.exe -m pytest -q --cov=src --cov-report=json\n"
    "   （Linux / CI 上为 python -m pytest -q --cov=src --cov-report=json）"
)

# —— 逐模块覆盖率阈值：本表是唯一事实源，AGENTS.md 只链接到此处、不复制数值。
# 新模块不必登记（未登记的按 GLOBAL_FLOOR 判），生成物豁免由 GATE_EXEMPT_MODULES 明确列出。
MODULE_THRESHOLDS: dict[str, float] = {
    "src/streams/spider.py": 50,
    "src/streams/stream.py": 70,
    "src/support/utils.py": 80,
    "src/streams/ttwid.py": 85,
    "src/network/proxy.py": 50,
}

# 未显式登记阈值的模块所用下限（MID-2259）。取 50.0 与 pyproject
# [tool.coverage.report] fail_under = 50 同口径——本检查不得比全库总闸更宽，
# 否则「新模块零测试」反而能通过逐模块闸。
GLOBAL_FLOOR = 50.0

# 显式豁免名单（MID-2259 要求的「第三条路」）：键 = 相对路径，值 = 豁免理由。
# 每条理由都必须说明「为什么它不该被本检查管」，且**不得**为了变绿往里塞普通模块——
# 被豁免的模块仍会打印读数（不参与判定），这样将来取消豁免时缺口立刻可见。
GATE_EXEMPT_MODULES: dict[str, str] = {
    # protoc 生成的桩代码：标注 DO NOT EDIT、随 protobuf 版本重新生成，手改无意义；
    "src/proto/douyin_pb2.py": "protoc 生成物（DO NOT EDIT，不参与人工补测）"
}


# 枚举 src/ 时跳过的目录名（编译缓存；它不是可测源码，却会混进 *.py 的通配结果）
_SRC_SCAN_SKIP_DIRS = frozenset({"__pycache__"})

# coverage JSON 报告为嵌套异构 dict，统一以 dict[str, object] 建模，读取处用 cast 收敛
CoverageData = dict[str, object]


def _resolve_data_file(data_file: str | None) -> Path:
    # 定位本次判定实际依赖的 coverage 数据文件（显式参数 > $COVERAGE_FILE > 默认 .coverage）。
    if data_file:
        return Path(data_file)
    env_file = os.environ.get("COVERAGE_FILE", "").strip()
    if env_file:
        return Path(env_file)
    return Path(DEFAULT_DATA_FILE)


def _src_modules(root: Path = ROOT) -> list[str]:
    # src/ 下全部待判定模块（posix 相对路径，排序稳定）。
    # 为什么按文件系统枚举而不是按 coverage 报告的键：报告的键集合本身可能是残缺的
    # （--cov 没生效 / 模块根本没被 import），拿它当判定面就等于「没测到 = 不存在」。
    base = root / "src"
    if not base.is_dir():
        return []
    modules: list[str] = []
    for path in sorted(base.rglob("*.py")):
        if _SRC_SCAN_SKIP_DIRS & set(path.parts):
            continue
        modules.append(path.relative_to(root).as_posix())
    return modules


def _threshold_for(module_path: str) -> tuple[float, str]:
    # 返回 (阈值, 阈值来源)：登记阈值 > 全局下限。豁免名单在调用侧先处理。
    if module_path in MODULE_THRESHOLDS:
        return MODULE_THRESHOLDS[module_path], "登记"
    return GLOBAL_FLOOR, f"下限 {GLOBAL_FLOOR:.0f}%"


def _get_coverage_json(data_file: str | None) -> CoverageData:
    # 生成并解析 coverage JSON 报告（判定所需的一切读数都来自这里）。
    #
    # MIN-19：数据文件不存在时**立刻**以 rc=2 失败并给出下一步命令。只打一条 WARN 再继续，
    # 随后读到的是空报告，最终表现成「6 个模块查不到」——把「忘了跑测试」误报成「覆盖率不达标」。
    resolved = _resolve_data_file(data_file)
    if not resolved.exists():
        print(f"ERROR: 未找到 coverage 数据文件 {resolved.as_posix()}", file=sys.stderr)
        print("       检查没有可判定的数据 —— 这是「没跑测试」，不是「覆盖率不达标」。", file=sys.stderr)
        print(f"   {NO_DATA_HINT}", file=sys.stderr)
        sys.exit(2)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        cmd = [sys.executable, "-m", "coverage", "json", "-o", tmp_path]
        if data_file:
            cmd.extend(["--data-file", data_file])

        # 加超时与显式编码：防止 coverage 挂起卡死 CI，并兼容 Windows 非 UTF-8 locale
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            print(f"ERROR: coverage json timed out: {' '.join(cmd)}")
            sys.exit(2)

        if result.returncode != 0:
            # coverage json 可能因全局 fail_under 未达标而返回非零退出码（报告仍已生成），
            # 真正的失败以「JSON 能否生成并解析」为准，此处仅提示不中断。
            # 例外：源码与数据行号错位时的 "No source for code" —— 它产不出报告，
            # 等价于数据陈旧，下面的「JSON 读不出来」分支会以 rc=2 + 重跑命令收口。
            print(f"WARN: coverage json exited with code {result.returncode}:\n{result.stderr}", file=sys.stderr)

        try:
            with open(tmp_path, encoding="utf-8") as f:
                report = cast(CoverageData, json.load(f))
        except OSError, ValueError:
            # 数据文件在、JSON 却读不出来：等价于「无可用数据」，同样硬失败（rc=2）。
            print(f"ERROR: failed to read coverage json report: {tmp_path}", file=sys.stderr)
            print(f"       数据文件 {resolved.as_posix()} 存在但报告不可解析。{NO_DATA_HINT}", file=sys.stderr)
            sys.exit(2)

        return report
    finally:
        # 无论成功、失败还是提前退出，都清理临时文件
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def evaluate_report(coverage_data: CoverageData, root: Path = ROOT) -> int:
    # 判定核心：吃一份 coverage JSON 报告 dict，对 **src/ 下全部模块**（按文件系统枚举）
    # 逐个评阈值，返回 0 达标 / 1 不达标 / 2 检查自身失效（含 sys.exit(2) 提前退出）。
    #
    # 为什么单独拆成这个纯判定函数：判定与「跑 coverage 子进程」耦在一起时，单元测试只能靠
    # 真数据文件驱动，于是「谓词拿不到输入就返回通过」的形态（files 为空、模块压根没登记）从来
    # 没有回归锁。拆开后用例可直接喂构造好的报告 dict，验证「删掉某条 fail-closed 判据就变红」。
    files = cast(dict[str, CoverageData], coverage_data.get("files", {}))
    if not files:
        # 「有数据文件但里面一个被测文件都没有」＝ 上一轮 --cov 未生效 / 数据被截断，
        # 与「忘了跑」同属检查失效：必须硬失败，否则下面只会报「模块查不到」而指错方向。
        print("ERROR: coverage 报告里没有任何被测文件条目（files 为空）", file=sys.stderr)
        print(f"       通常是 --cov=src 未生效或数据文件已损坏。{NO_DATA_HINT}", file=sys.stderr)
        sys.exit(2)

    modules = _src_modules(root)
    if not modules:
        # 枚举不到任何 src/ 模块 = 判定面为空。MIN-19 的口径：检查「没东西可查」一律非 0，
        # 绝不输出「PASSED: All 0 module(s)」——那正是本文件要消灭的假绿形态。
        print("ERROR: 在 src/ 下没枚举到任何待判定模块（root 解析错位？）", file=sys.stderr)
        print("       判定面为空不等于全部达标。", file=sys.stderr)
        sys.exit(2)

    # 登记表/白名单指向磁盘上不存在的模块 = 检查配置本身腐烂（模块改名后表没跟着改）。
    # 这与「某模块覆盖率不达标」是两类问题，按 rc=2 处理，逼人去改表而不是加测试。
    module_set = set(modules)
    stale_entries = sorted(
        [m for m in MODULE_THRESHOLDS if m not in module_set] + [m for m in GATE_EXEMPT_MODULES if m not in module_set]
    )
    if stale_entries:
        print("ERROR: 覆盖率登记表指向 src/ 下不存在的模块（检查配置已腐烂）:", file=sys.stderr)
        for entry in stale_entries:
            print(f"   - {entry}", file=sys.stderr)
        print(
            "       模块改名/删除时必须同步 MODULE_THRESHOLDS / GATE_EXEMPT_MODULES。",
            file=sys.stderr,
        )
        sys.exit(2)

    failures: list[tuple[str, float, float, str]] = []
    passes: list[tuple[str, float, float, str]] = []
    missing_modules: list[str] = []
    exempt: list[tuple[str, float]] = []

    for module_path in modules:
        if module_path in GATE_EXEMPT_MODULES:
            file_data = _find_module_coverage(coverage_data, module_path)
            pct = _percent(file_data) if file_data is not None else 0.0
            exempt.append((module_path, pct))
            continue
        threshold, source = _threshold_for(module_path)
        file_data = _find_module_coverage(coverage_data, module_path)

        if file_data is None:
            missing_modules.append(module_path)
            continue

        coverage_pct = _percent(file_data)

        if coverage_pct >= threshold:
            passes.append((module_path, coverage_pct, threshold, source))
        else:
            failures.append((module_path, coverage_pct, threshold, source))

    # 读数一律打印（含豁免项），这样将来取消豁免时缺口立刻可见
    print("=" * 60)
    print(f"Per-module coverage gate（判定面 = src/ 全部 {len(modules)} 个模块，未登记阈值按下限 {GLOBAL_FLOOR:.0f}%）")
    print("=" * 60)

    if passes:
        print(f"\n[PASS] {len(passes)} module(s) meet threshold:")
        for module, pct, threshold, source in passes:
            print(f"   {module:38s} {pct:5.1f}%  (>= {threshold:.0f}%, {source})")

    if exempt:
        print(f"\n[EXEMPT] {len(exempt)} module(s) 显式豁免（不参与判定，读数仅供复核）:")
        for module, pct in exempt:
            print(f"   {module:38s} {pct:5.1f}%  —— {GATE_EXEMPT_MODULES[module]}")

    if missing_modules:
        print(f"\n[FAIL] {len(missing_modules)} module(s) missing from coverage data:")
        for module in missing_modules:
            print(f"   {module}")
        # 模块在覆盖率数据里查不到，通常意味着 .coverage 未采集到它、或 --cov=src 未生效。
        # 这是「检查失效」而非「达标」，必须判失败——否则会输出「全部 0 个模块达标」的假绿。
        print("       （查不到即视为检查失效，按失败处理；确属不可测的生成物请写进 GATE_EXEMPT_MODULES 并附理由）")

    if failures or missing_modules:
        if failures:
            print(f"\n[FAIL] {len(failures)} module(s) below threshold:")
            for module, pct, threshold, source in failures:
                deficit = threshold - pct
                print(f"   {module:38s} {pct:5.1f}%  (>= {threshold:.0f}%, {source})  <- {deficit:.1f}% short")
        print(f"\n{'=' * 60}")
        print(f"FAILED: {len(failures)} module(s) below threshold, {len(missing_modules)} module(s) missing from data")
        print(f"{'=' * 60}")
        return 1

    print(f"\n{'=' * 60}")
    print(f"PASSED: All {len(passes)} module(s) meet coverage threshold")
    print(f"{'=' * 60}")
    return 0


def _find_module_coverage(coverage_data: CoverageData, module_path: str) -> CoverageData | None:
    # 在 JSON 报告里定位某模块的读数。报告键的形态不固定（相对 / 绝对 / Windows 反斜杠），
    # 所以匹配规则必须紧到「只可能命中这一个模块」——宽松一档就是拿别的文件的读数评它。
    files = cast(dict[str, CoverageData], coverage_data.get("files", {}))

    # 第一级：报告键与登记表里的相对路径完全相同
    if module_path in files:
        return files[module_path]

    # 第二级（现规则）：**整段模块路径（含 src/）必须出现在报告键的尾部**，分隔符按 POSIX 归一
    # 后再比。这条被收紧过两次，两次都是「同名模块顶掉读数」：① 按纯文件名命中，于是
    # tests/utils.py 能顶掉 src/support/utils.py；② 去 src/ 前缀 + endswith('/文件名')，tests/utils.py
    # 照样命中 src/support/utils.py（本仓两处同名模块真实存在）。故现规则要求整段路径出现在键尾。
    module_norm = Path(module_path).as_posix()

    def _posix(key: str) -> str:
        return key.replace("\\", "/")

    if "/" not in module_norm:
        # 第三级（唯一允许按文件名匹配的情形）：登记表写的就是纯文件名时，仍限定父目录为
        # src 或项目根——不再无门槛接受任意同名文件
        for file_path, file_data in files.items():
            file_norm_path = PurePosixPath(_posix(file_path))
            if file_norm_path.name == module_norm and file_norm_path.parent.name in ("src", ""):
                return file_data
        return None

    for file_path, file_data in files.items():
        key = _posix(file_path)
        # 相等 = 相对键（"src/x.py"）；尾部匹配 = 绝对键（"D:/repo/src/x.py"、"/__w/repo/src/x.py"），
        # 且必须以 "/" 边界起始，避免 "mysrc/support/utils.py" 这类前缀粘连命中
        if key == module_norm or key.endswith("/" + module_norm):
            return file_data

    return None


def _percent(file_data: CoverageData) -> float:
    # 取单文件覆盖率；缺 summary / 类型异常一律按 0 处理（保守侧：拿不到读数不得当成满分）
    summary = cast(CoverageData, file_data.get("summary", {}))
    raw = cast(object, summary.get("percent_covered", 0.0))
    return float(cast(float, raw))


def check_coverage(data_file: str | None = None, root: Path = ROOT) -> int:
    # 检查入口：解析数据文件 → 生成/读取 coverage JSON → 交给 evaluate_report 判定。
    # 返回 0 达标 / 1 不达标；rc=2 的「检查没跑」形态在 _get_coverage_json 与
    # evaluate_report 内部直接 sys.exit(2)（数据缺失 / 报告为空 / 登记表腐烂）。
    coverage_data = _get_coverage_json(data_file)
    return evaluate_report(coverage_data, root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-module coverage gate check")
    _ = parser.add_argument(
        "--data-file",
        type=str,
        default=None,
        help="Path to .coverage data file (auto-detected by default)",
    )
    args = parser.parse_args()
    sys.exit(check_coverage(data_file=cast(str | None, args.data_file)))


if __name__ == "__main__":
    main()
