# tool/smoke_test.py 的配置校验与退出码回归测试。

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script(filename: str) -> Any:
    # tool/ 不在包路径内：按文件路径显式加载。
    # 返回 Any 而非 ModuleType：被测面是「脚本内的私有函数 + 模块全局替换」，
    # ModuleType 的属性访问本就是 Any，这里不靠类型收窄、只靠真实调用。
    spec = importlib.util.spec_from_file_location(f"_{filename[:-3]}_probe", ROOT / "tool" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke_test = _load_script("smoke_test.py")

# ---------- smoke_test：配置形态与退出码 ----------
# 畸形形态清单：(配置对象, 报错里必须出现的键名, 报错里必须出现的期望形状词)。
# 期望形状词用 JSON 术语（array/object），与 _json_type / _SHAPE_REQUIREMENTS 的文案同源。
MALFORMED_CONFIGS: list[tuple[object, str, str]] = [
    ({"checks": {"path": "/"}}, "checks", "array"),
    ({"checks": "/health"}, "checks", "array"),
    ({"checks": ["just-a-string"]}, "checks[0]", "object"),
    ({"checks": [{"path": "/", "headers": "Referer: https://x/"}]}, "headers", "object"),
    ({"checks": [{"path": "/", "headers": ["Referer"]}]}, "headers", "object"),
    ({"checks": [{"path": "/", "expect_json": ["status"]}]}, "expect_json", "object"),
    ({"checks": [{"path": "/", "expect_contains": "欢迎"}]}, "expect_contains", "array"),
    # L-31（2026-10-18）：容器形态对、元素形态错同样属畸形。run_check 的 `token in content`
    # 对数字/对象元素抛 TypeError 并穿透 main()（只接 OSError/ValueError）→ rc=1 崩溃栈，
    # 被 .github/actions/retry 读成「面板接口挂了」。needle 带下标，定位到具体那一个元素。
    ({"checks": [{"path": "/", "expect_contains": ["ok", 200]}]}, "expect_contains[1]", "string"),
    ({"checks": [{"path": "/", "expect_contains": [{"k": "v"}]}]}, "expect_contains[0]", "string"),
    ({"checks": [{"path": "/", "expect_contains": [None]}]}, "expect_contains[0]", "string"),
    ({"checks": [{"path": "/"}], "base_url": 8000}, "base_url", "string"),
]


def _write_cfg(tmp_path: Path, payload: object, name: str = "cfg.json") -> Path:
    cfg = tmp_path / name
    _ = cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg


def _call_main(monkeypatch: pytest.MonkeyPatch, cfg_path: Path, extra: tuple[str, ...] = ()) -> int:
    # 进程内驱动 main()：退出码就是 SystemExit.code，与真实子进程一致（main 只做 argparse +
    # sys.exit，无 atexit/日志归档等旁路）。真实 rc 另由 test_malformed_real_process_rc 兜一次。
    monkeypatch.setattr(sys, "argv", ["smoke_test.py", "--config", str(cfg_path), *extra])
    with pytest.raises(SystemExit) as excinfo:
        smoke_test.main()
    code = excinfo.value.code
    assert isinstance(code, int), f"main() 应以整数码退出，实际 {code!r}"
    return code


class _FakeResponse:
    # urlopen 返回值的最小形态：run_check 只用 __enter__ / status / read。
    def __init__(self, status: int, body: str) -> None:
        self._status = status
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    @property
    def status(self) -> int:
        return self._status

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def _install_fake_urlopen(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> list[str]:
    # 同样走「替换被测模块全局引用」的路子：urllib.request 是 stdlib 本体，就地打桩会波及
    # 同进程其它用例；这里把 smoke_test 模块里的 urllib 引用换成浅 namespace，只改 urlopen 一支。
    seen: list[str] = []

    def fake_urlopen(req: Any, timeout: object = None) -> _FakeResponse:
        seen.append(str(getattr(req, "full_url", req)))
        return response

    request_shim = types.SimpleNamespace(**vars(smoke_test.urllib.request))
    request_shim.urlopen = fake_urlopen
    urllib_shim = types.SimpleNamespace(request=request_shim, error=smoke_test.urllib.error)
    monkeypatch.setattr(smoke_test, "urllib", urllib_shim)
    return seen


def _spawn_smoke_child(cfg_path: Path) -> subprocess.CompletedProcess[bytes] | None:
    # 真起子进程拿真实退出码（错误处理契约的最终裁判）。返回 None = 本机起不了子进程。
    # 只对「spawn 本身失败」重试：中文 Windows 的本机 harness 在多 agent 并发时偶发
    # `OSError: [WinError 6] 句柄无效`（子进程句柄复制被拦；实测 2026-09-29 单跑连过 6 次、
    # 并发窗口内 3 次重试全被拦），属 AGENTS.md「本机偶发环境噪声」，与退出码判定无关。
    # **断言永不参与重试**：一旦拿到 CompletedProcess，rc/输出不合规就是红。
    # 「起不来」刻意用 Optional 返回值交调用方 skip，而不是在辅助函数里直接 pytest.skip：
    # 按 AGENTS.md 口径辅助函数的终态不依赖 pytest 的 NoReturn 可见性（CI typecheck job
    # 装不装 pytest 结论都该一致），可省形态也免掉「不可达语句」那类二次告警。
    # 走 skip 的边界（不是放宽检查）：
    #   · 被跳过的只有「进程边界」这一层；rc=2 + 无崩溃栈的判定在进程内由
    #     test_main_exits_two_without_traceback 全量覆盖（SystemExit.code 即退出码），
    #     故本机跳这条不会让任何一条畸形形态失去用例。
    #   · CI（ubuntu runner）能稳定起子进程，本条在检查面上恒为真实执行，不存在
    #     「靠 skip 让 CI 少跑一段平台分支」的那种静默失效（AGENTS.md 反对的是那一类）。
    #   · skip reason 会出现在 `-rs`/汇总里，本机不会形成「全绿但其实没跑」的错觉。
    # 具体异常类型（FileExistsError 之外的 OSError 家族，实测为 PermissionError 型拦截与
    # WinError 6）不进返回值：调用方只需区分「跑得起来 / 跑不起来」，判定与理由都在用例里写。
    for _attempt in range(3):
        try:
            return subprocess.run(
                [sys.executable, str(ROOT / "tool" / "smoke_test.py"), "-c", str(cfg_path)],
                capture_output=True,
            )
        except OSError:
            continue
    return None


class TestSmokeConfigMalformed:
    @pytest.mark.parametrize("payload,needle,shape", MALFORMED_CONFIGS)
    def test_load_config_raises_value_error_not_attribute_error(
        self, tmp_path: Path, payload: object, needle: str, shape: str
    ) -> None:
        # 契约核心：畸形必须走 ValueError（→ main 退 2），不得留 AttributeError/TypeError 给
        # run_check 崩栈（旧形态就是 rc=1 + 满屏 Traceback，被消费方读成「面板接口挂了」）。
        cfg = _write_cfg(tmp_path, payload)
        with pytest.raises(ValueError) as excinfo:
            smoke_test.load_config(str(cfg))
        msg = str(excinfo.value)
        assert needle in msg, f"报错未定位到畸形处 {needle}: {msg}"
        assert shape in msg, f"报错未给出期望形状 {shape}: {msg}"

    @pytest.mark.parametrize("payload,needle,shape", MALFORMED_CONFIGS)
    def test_main_exits_two_without_traceback(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path, payload: object, needle: str, shape: str
    ) -> None:
        cfg = _write_cfg(tmp_path, payload)
        assert _call_main(monkeypatch, cfg) == 2
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err, f"畸形配置不得逃逸崩溃栈: {captured.err}"
        assert "ERROR:" in captured.err
        assert needle in captured.err and shape in captured.err
        # 配置坏了就绝不该发出任何请求：stdout 里连冒烟报告的头都没有。
        assert "冒烟测试报告" not in captured.out

    def test_malformed_real_process_rc_is_two(self, tmp_path: Path) -> None:
        # 真实子进程口径复核（按字节比较，与码页/语言解耦，见 AGENTS.md「探测子进程输出一律
        # 按字节比较」）：rc=2、无 Traceback、stderr 指明期望形状。
        cfg = _write_cfg(tmp_path, {"checks": [{"path": "/", "headers": "Referer: https://x/"}]}, "hdr_str.json")
        proc = _spawn_smoke_child(cfg)
        if proc is None:
            # 边界与理由见 _spawn_smoke_child 注释：只有「本机 harness 起不了子进程」会走到这里，
            # 判定面本身由 test_main_exits_two_without_traceback 在进程内全量覆盖，CI 侧恒真跑。
            pytest.skip(
                "子进程三次重试都起不来（本机 harness 句柄拦截，属环境限制非被测面缺陷）；"
                "rc=2 与「无崩溃栈」由 test_main_exits_two_without_traceback 进程内覆盖"
            )
        assert proc.returncode == 2, f"headers 为字符串应退 2，实际 rc={proc.returncode} stderr={proc.stderr!r}"
        assert b"Traceback" not in proc.stderr
        assert b"ERROR:" in proc.stderr
        assert b"headers" in proc.stderr
        assert b"object" in proc.stderr

    def test_broken_config_emits_no_request_and_no_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 半执行状态防线：第 1 条合法、第 2 条 headers 坏 → 整体拒绝，一条请求都不发。
        seen = _install_fake_urlopen(monkeypatch, _FakeResponse(200, "{}"))
        cfg = _write_cfg(
            tmp_path,
            {"base_url": "http://127.0.0.1:9", "checks": [{"path": "/ok"}, {"path": "/bad", "headers": "x: y"}]},
        )
        assert _call_main(monkeypatch, cfg) == 2
        assert seen == []

    def test_bad_element_in_expect_contains_exits_two_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # L-31 的进程边界形态：元素级畸形必须同样落 rc=2（配置问题），而不是 run_check 抛
        # TypeError 的 rc=1（接口问题）。这两档在 tool/_ci_web_smoke.sh 里处置相反——
        # 前者直接停、后者还会退避重试，混同等于把「配置写坏了」重跑一遍。
        seen = _install_fake_urlopen(monkeypatch, _FakeResponse(200, "ok"))
        cfg = _write_cfg(
            tmp_path,
            {"base_url": "http://127.0.0.1:9", "checks": [{"path": "/", "expect_contains": ["ok", 200]}]},
        )
        assert _call_main(monkeypatch, cfg) == 2
        assert seen == [], "畸形元素仍发出了请求 = 半执行状态回来了"


class TestSmokeConfigValid:
    def test_valid_dict_config_exits_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # 合法形态（headers object / expect_contains array / expect_json object）：
        # 原有判定路径不变 —— 断言执行且全通过 → rc 0。
        seen = _install_fake_urlopen(monkeypatch, _FakeResponse(200, '{"status": "ok"}'))
        cfg = _write_cfg(
            tmp_path,
            {
                "base_url": "http://127.0.0.1:9",
                "checks": [
                    {
                        "name": "健康检查",
                        "path": "/health",
                        "method": "GET",
                        "expected_status": 200,
                        "headers": {"Referer": "https://example.invalid/"},
                        "expect_contains": ["status"],
                        "expect_json": {"status": "ok"},
                    }
                ],
            },
        )
        assert _call_main(monkeypatch, cfg) == 0
        assert seen == ["http://127.0.0.1:9/health"]

    def test_valid_top_level_list_config_with_cli_base_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 顶层直接是 array 的合法形态 + 命令行 --base-url：同样不回归（该形态无 base_url 位）。
        seen = _install_fake_urlopen(monkeypatch, _FakeResponse(204, ""))
        cfg = _write_cfg(tmp_path, [{"path": "/ping", "expected_status": 204, "headers": {}}])
        assert _call_main(monkeypatch, cfg, ("--base-url", "http://127.0.0.1:9")) == 0
        assert seen == ["http://127.0.0.1:9/ping"]

    def test_explicit_null_containers_are_legal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # 显式 null 与「不写该键」等价（校验放行 null）。旧形态下 `"headers": null` 会让
        # `check.get("headers", {})` 返回 None 再 .items() 崩栈 —— 故 run_check 一并改成 `or {}`。
        _ = _install_fake_urlopen(monkeypatch, _FakeResponse(200, "hello"))
        cfg = _write_cfg(
            tmp_path,
            {"base_url": "http://127.0.0.1:9", "checks": [{"path": "/", "headers": None, "expect_contains": None}]},
        )
        assert _call_main(monkeypatch, cfg) == 0

    def test_interface_failure_stays_rc_one(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # 2 与 1 不得混同（文件头契约）：配置合法、接口状态码不符 → 仍 1。
        _ = _install_fake_urlopen(monkeypatch, _FakeResponse(503, "down"))
        cfg = _write_cfg(tmp_path, {"base_url": "http://127.0.0.1:9", "checks": [{"path": "/"}]})
        assert _call_main(monkeypatch, cfg) == 1

    def test_empty_and_missing_checks_keep_rc_two(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # MIN-2260 的既有语义必须原样保留：空 array 与缺 checks 键都退 2（本次收口不得挤掉）。
        empty = _write_cfg(tmp_path, {"checks": []}, "empty.json")
        assert _call_main(monkeypatch, empty) == 2
        missing = _write_cfg(tmp_path, {"base_url": "http://127.0.0.1:9"}, "missing.json")
        assert _call_main(monkeypatch, missing) == 2

    def test_shipped_smoke_web_config_still_loads(self) -> None:
        # 仓库自带的 CI 配置（tool/smoke_web.json）是新校验的第一个消费者：必须仍能解析，
        # 且解析出 2 条断言（MIN-2260 的「0 条也算红」在消费侧还有第二道判据）。
        checks, base_url = smoke_test.load_config(str(ROOT / "tool" / "smoke_web.json"))
        assert len(checks) == 2
        assert base_url == "http://127.0.0.1:8000"
