# 并发与发布安全回归：直接驱动生产实现。
import ast
import re
from pathlib import Path
from typing import Any

import pytest

from src.recording import state

ROOT = Path(__file__).resolve().parent.parent


# ────────────────────────────── SEV-2222 ──────────────────────────────
# 形态：tests/test_concurrency.py 用自己重写的一份 rate_limit 闭包做断言，从不调用 src 侧真正的
# _douyin_rate_limit —— 「限流被删掉」这类回归完全不会被发现。本锁直接驱动 src 实现。


def test_douyin_rate_limit_actually_sleeps_and_advances_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    # 直接驱动 src/stream_select._douyin_rate_limit：断言它真的 (a) 读 main.douyin_min_interval
    # (b) 间隔不足时 sleep 该差值 (c) 把 douyin_last_request_time 推进到当前时刻。
    # 换掉的是 **stream_select 模块自己的 time 引用**（浅拷贝 shim），不是 stdlib time 本体：
    # 改本体会全进程生效，波及 loguru enqueue 线程 / harness 守护线程 / coverage（R1 会红）。
    import types

    import src.streams.selection as stream_select

    sleeps: list[float] = []
    fake_now = [1000.0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        fake_now[0] += seconds  # 让时间随 sleep 前进，模拟真实等待

    time_shim = types.SimpleNamespace(**vars(stream_select.time))
    time_shim.sleep = fake_sleep
    time_shim.time = lambda: fake_now[0]
    monkeypatch.setattr(stream_select, "time", time_shim)
    # 上一条请求发生在 1.0s 前、最小间隔 3.0s → 应补睡 2.0s；实现是「sleep 后重新 time.time()」，
    # 故时间戳推进到 1002.0（fake_sleep 已让 fake_now 随 sleep 前进，模拟真实时钟在等待期间流逝）。
    monkeypatch.setattr(state, "douyin_min_interval", 3.0, raising=False)
    monkeypatch.setattr(state, "douyin_last_request_time", 999.0, raising=False)

    stream_select._douyin_rate_limit()

    assert sleeps == [pytest.approx(2.0)], f"间隔不足时未按差值补睡（实际 sleeps={sleeps}）"
    assert state.douyin_last_request_time == pytest.approx(1002.0), "限流后未把 douyin_last_request_time 推进到当前时刻"


def test_douyin_rate_limit_does_not_sleep_when_interval_already_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    # 反向锁：间隔已满足时**不得**无谓 sleep——否则每个房间每轮都被拖慢一秒，
    # 是「限流过严」这一族回归（比漏限流更隐蔽，因为它不会报错，只会变慢）。
    # 同样只换 stream_select 自己的 time 引用（浅拷贝 shim），理由见上一条用例。
    import types

    import src.streams.selection as stream_select

    sleeps: list[float] = []
    time_shim = types.SimpleNamespace(**vars(stream_select.time))
    time_shim.sleep = sleeps.append
    time_shim.time = lambda: 2000.0
    monkeypatch.setattr(stream_select, "time", time_shim)
    monkeypatch.setattr(state, "douyin_min_interval", 3.0, raising=False)
    monkeypatch.setattr(state, "douyin_last_request_time", 1990.0, raising=False)

    stream_select._douyin_rate_limit()

    assert sleeps == [], f"间隔已满足却仍 sleep：{sleeps}"
    assert state.douyin_last_request_time == pytest.approx(2000.0)


def test_concurrency_rate_limit_case_drives_src_not_a_local_copy() -> None:
    # 把 SEV-2222/SEV-2223 的**根因形态**固化成硬断言：tests/test_concurrency.py 里三个曾自建
    # 实现的类不得只靠自己重写的锁/限流/缓存闭包来「验证限流」——类体内必须出现对 src 真实入口
    # （下表符号任一）的引用，只允许打桩网络层与时间常量。
    #   [历史注] 2026-09-23 前本条写成 `if not drives_src: pytest.xfail(...)`：违例只软登记，
    #   修好后无人回来摘标记，此后改回自实现也会被悄悄放过；且判据只覆盖单个类。
    source = (ROOT / "tests" / "test_concurrency.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    wanted = {
        # 类名 -> 该类体内必须出现的**真实实现**符号（任一命中即算驱动了 src）
        "TestThreadSafeCredential": ("ttwid_module.get_ttwid", "src.streams.ttwid"),
        "TestRateLimit": ("stream_select._douyin_rate_limit", "_douyin_rate_limit"),
        "TestCredentialSharing": ("credentials.credential_cache.get_or_fetch", "get_or_fetch"),
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in wanted:
            found.append(node.name)
            body_src = ast.get_source_segment(source, node) or ""
            assert any(symbol in body_src for symbol in wanted[node.name]), (
                f"tests/test_concurrency.py::{node.name} 又回到「测试内自建锁/限流/缓存」的假绿形态"
                f"（体内找不到 {wanted[node.name]} 任一符号）—— SEV-2222/SEV-2223"
            )
    assert set(found) == set(wanted), f"三个自建实现类被改名/删除，锁位需重定位（实际找到 {found}）"


def _load_build_exe() -> Any:
    # build_exe.py 在 import 期只做常量/函数定义（main() 受 __name__ 守卫），加载安全。
    import importlib.util

    path = ROOT / "tool/build_exe.py"
    spec = importlib.util.spec_from_file_location("_regression_probe_build_exe", path)
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ────────────────────────────── SEV-2213 ──────────────────────────────
# 形态：build_exe.py 的 copy_external_binaries 把 config/ **整目录复制**进发布目录，
# 而 make_zip 把发布目录整棵树压缩——含 Cookie / 代理账密 / Web 口令的 config.ini 与
# 含房间地址（可能内嵌 query token）的 URL_config.ini 随之对外分发。
# 这里锁两层：① 脱敏器本身（不产生凭据）② make_zip 前的校验（产生了就拦住）。


def _secret_bearing_pairs(text: str) -> list[tuple[str, str, str]]:
    # 取出 ini 文本里「非空且命中敏感判定」的 (节, 键, 值)，作为「必须被脱敏」的基准集合。
    # 入参是**文本**（不是 Path）：既有 _load_build_exe 那条链读的就是 read_text 结果，
    # 传路径会以 TypeError 炸在 read_string，而不是给出「有没有凭据」这个要断言的结论。
    import configparser

    from src.api import config as web_config

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    parser.read_string(text)
    out: list[tuple[str, str, str]] = []
    for section in parser.sections():
        for key, value in parser.items(section):
            v = value.strip()
            if v and (web_config.is_sensitive_item(section, key) or web_config._looks_like_secret_value(v)):
                out.append((section, key, v))
    return out


def test_config_sanitizer_removes_every_sensitive_value() -> None:
    # SEV-2213 主锁（「不产生」一侧）：把**真实仓库 config.ini** 喂给脱敏器，断言所有「非空 +
    # 敏感」的键在产物里都变成空值，且节结构完整保留。用真实配置而非构造样本：缺陷本身就是
    # 「真实凭据被整目录复制」，只有真实样本才能证明脱敏对实际键名/节名成立。
    build_exe = _load_build_exe()
    src_ini = ROOT / "config" / "config.ini"
    if not src_ini.is_file():
        pytest.skip("本工作区无 config/config.ini（该文件被 gitignore，属正常缺省）")

    source_text = src_ini.read_text(encoding="utf-8-sig", errors="replace")
    sanitized = build_exe._sanitized_config_text(source_text)

    baseline = _secret_bearing_pairs(source_text)
    assert baseline, "真实 config.ini 里应有敏感键（若为空说明判定口径被改窄，需复核）"

    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    parser.read_string(sanitized)
    survived = [
        (section, key)
        for section, key, _value in baseline
        if parser.get(section, key, fallback="<MISSING>").strip() != ""
    ]
    assert not survived, f"脱敏后仍有敏感键保留真实值: {survived}"

    # 结构完整性：节名不得因脱敏而丢失（否则产物不是可用模板）
    src_parser = configparser.ConfigParser(interpolation=None)
    src_parser.optionxform = str  # type: ignore[method-assign,assignment]
    src_parser.read_string(source_text)
    assert parser.sections() == src_parser.sections(), "脱敏改变了节结构，产物不再是可用模板"


def test_config_sanitizer_fails_closed_on_unparsable_input() -> None:
    # 反向锁：ini 解析失败时**不得**退回「原样返回」——那正是本次的失效形态。
    # 判据：损坏输入下产物必须不含原文里的凭据串。
    build_exe = _load_build_exe()
    broken = "[录制设置]\n代理地址 = socks5://user:LEAKME@127.0.0.1:1080\n[[[ 坏结构\n"
    out = build_exe._sanitized_config_text(broken)
    assert "LEAKME" not in out, "解析失败时退回了原样文本（fail-open），凭据会随 zip 外发"


def test_url_config_sanitizer_yields_comment_only_template() -> None:
    # URL_config.ini 一律重写为注释样例：不得出现任何未注释的有效房间行。
    build_exe = _load_build_exe()
    out = build_exe._sanitized_url_config_text()
    for line in out.splitlines():
        stripped = line.strip()
        assert not stripped or stripped.startswith("#"), f"样例里出现了有效行（应为纯注释）：{stripped!r}"


def test_make_zip_aborts_when_release_dir_contains_real_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SEV-2213 主锁（「产生了就拦住」一侧）：人为把一份**含真实凭据**的 config.ini 放进
    # 发布目录，断言 make_zip 之前的校验会非零退出（SystemExit），即 zip 不会被生成。
    # 这是对「校验存在但没接在 make_zip 上」这一形态的直接否定——只断言函数存在是不够的。
    build_exe = _load_build_exe()

    release = tmp_path / "dylr"
    (release / "config").mkdir(parents=True)
    # 构造一个必然命中敏感判定的值（代理地址带账密，本仓 _PROXY_ADDR_KEYS 明确管这类键）
    (release / "config" / "config.ini").write_text(
        "[录制设置]\n代理地址 = http://realuser:REALPASS@10.0.0.1:8080\n",
        encoding="utf-8",
    )
    (release / "config" / "URL_config.ini").write_text(
        "https://live.douyin.com/123456?token=REALTOKEN\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(build_exe, "RELEASE_DIR", release)
    # 让源 config.ini 也含同一凭据，以便走「逐键比对源值」那条判据
    src_dir = tmp_path / "src_config"
    src_dir.mkdir()
    (src_dir / "config.ini").write_text(
        "[录制设置]\n代理地址 = http://realuser:REALPASS@10.0.0.1:8080\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_exe, "PROJECT_ROOT", tmp_path)

    with pytest.raises(SystemExit):
        build_exe.assert_no_credentials_in_release()


def test_make_zip_calls_credential_assertion_before_archiving() -> None:
    # 结构锁：校验与封印的**先后关系**必须写在 make_zip 内（而非靠调用方自觉）——只断言「校验
    # 函数存在」会漏掉「make_zip 没调它」。函数体经 _function_body_source 用 AST 取源码段，
    # 不用字符串 index 找边界（体内嵌套 def 会把边界切错，实测踩过）。
    src = (ROOT / "tool/build_exe.py").read_text(encoding="utf-8")
    body = _function_body_source(src, "make_zip")
    # 定位一律带括号（调用形态）：get_source_segment 是原文切片、**含函数体内的注释行**，只匹配
    # 裸函数名会先命中 build_exe.py 里的说明注释。
    # [历史注 2026-09-24] 压缩动作由 shutil.make_archive( 改为自建的 _zip_release_dir(（为指定
    # compresslevel=9），锚点同步改新接缝；锁的语义（凭据校验必须早于压缩）完全不变。
    assert "assert_no_credentials_in_release()" in body, "make_zip 未调用凭据校验"
    assert body.index("assert_no_credentials_in_release()") < body.index("_zip_release_dir("), (
        "凭据校验晚于压缩：zip 已生成才拦，等于没拦"
    )


def _function_body_source(source: str, name: str) -> str:
    # 返回名为 name 的顶层函数完整源码段（def 行到函数结束）。关键性质：ast.get_source_segment 做
    # 的是**原文切片**，体内 `#` 注释一并返回，故断言必须匹配可执行形态（调用带括号、参数带实参），
    # 否则注释文本就能喂饱断言，删掉调用点也不会变红。
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"tool.build_exe.py 里找不到顶层函数 {name}()")


def test_copy_external_binaries_does_not_copytree_config() -> None:
    # 形态锁：copy_external_binaries 里不得再出现对 config/ 的 copytree
    # （整目录复制正是本次的根因；shutil.copytree 会把 config.ini 原样带过去）。
    src = (ROOT / "tool/build_exe.py").read_text(encoding="utf-8")
    body = _function_body_source(src, "copy_external_binaries")
    assert "copytree(cfg_src" not in body, "config/ 仍在被整目录 copytree 复制（SEV-2213 复发）"


def test_no_credentials_in_release_passes_on_sanitized_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 正向对照：脱敏后的发布目录必须**通过**校验（否则校验会把正常构建也拦死）。
    build_exe = _load_build_exe()
    release = tmp_path / "dylr"
    (release / "config").mkdir(parents=True)
    (release / "config" / "config.ini").write_text("[录制设置]\n代理地址 = \n", encoding="utf-8")
    (release / "config" / "URL_config.ini").write_text("#https://live.douyin.com/000000000000\n", encoding="utf-8")
    monkeypatch.setattr(build_exe, "RELEASE_DIR", release)
    monkeypatch.setattr(build_exe, "PROJECT_ROOT", tmp_path / "nonexistent_root")

    build_exe.assert_no_credentials_in_release()  # 不抛异常即通过


def test_prepare_url_config_reentrant_with_sanitizer_output() -> None:
    # 兼容性锁：_prepare_url_config() 与 _prepare_config_dir() 必须**同源且可重入**——二者都会写
    # config/URL_config.ini，样例串不一致时后跑的那个会覆盖前者（冒烟路径与打包路径各调一次）。
    build_exe = _load_build_exe()
    assert build_exe._sanitized_url_config_text() == "#https://live.douyin.com/000000000000\n", (
        "URL_config.ini 的样例串与 _prepare_url_config 不一致（两处会互相覆盖）"
    )


def test_exe_name_derives_from_app_name() -> None:
    # MID-2200 定点锁：唯一 exe 的名字必须由 APP_NAME 派生——写死会在改名时漂移。
    build_exe = _load_build_exe()
    suffixes = re.findall(r"name='\{app\}([^']*)'", build_exe.SPEC_TEMPLATE)
    assert suffixes and set(suffixes) == {""}, f"exe 名称不再由 {{app}} 派生（实际后缀 {suffixes}）：改名时会漂移"


def test_smoke_fatal_markers_cover_import_failures() -> None:
    # MID-2200 定点锁：--smoke 的 FATAL_MARKERS 必须覆盖「导入失败」这一类——
    # 缺 h2/socksio（SEV-2216）在冻结产物里正是以 ModuleNotFoundError 形态出现。
    build_exe = _load_build_exe()
    for marker in ("Traceback (most recent call last)", "ModuleNotFoundError", "ImportError"):
        assert marker in build_exe.FATAL_MARKERS, f"冒烟致命标记缺少 {marker!r}"
