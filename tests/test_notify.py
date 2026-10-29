# -*- coding: utf-8 -*-
# 通知模块单测
#
# 覆盖 run_script 的超时保护：用户脚本挂起时必须被强制终止并回收管道，
# 不能让 communicate() 无 timeout 永久占用调用线程。
#
# 备注：项目用 loguru 记录错误，loguru 默认不桥接 stdlib logging，
# 故 caplog 抓不到——测试自带 InMemorySink 捕获消息文本。
import io
import shlex
import sys
import time
from collections.abc import Generator

import pytest
from loguru import logger

import src.notifications.notify as notify
from src.recording import state

# M-30：命令一律用 sys.executable，不写死字面量 `python`——生产 run_script 以 shell=False +
# shlex.split 在 PATH 里查找可执行文件，Linux/CI 镜像常只提供 python3（无 python 软链），旧写法在
# CI 上「正常完成 / 超时」两条用例必失败，本机 Windows 单跑必绿——典型的「本地绿、CI 红」。换绑
# 子进程使用当前项目解释器。
# 必须 shlex.quote：路径可能含空格，且 run_script 走 shlex.split(posix=True)——单引号内的 Windows
# 反斜杠按字面保留，round-trip 无损。本文件其余两处「可执行文件字面量」刻意不改写：
#   __definitely_not_a_real_binary_42__ 要的正是「PATH 里查不到」⇒ OSError 分支；
#   echo "unterminated 在 shlex.split 阶段就抛 ValueError，永远不会真的起进程。


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # 把超时压到 1 秒，让测试快速断言「超时后被 kill」而不是等满 300 秒
    monkeypatch.setattr(notify, "_SCRIPT_TIMEOUT_SECONDS", 1.0)


@pytest.fixture
def log_capture() -> Generator[io.StringIO, None, None]:
    # 内存 sink：直接接管 loguru 消息（不依赖 caplog / stdlib logging 桥接），
    # 退出 fixture 时自动 remove 不污染全局 handler
    buf = io.StringIO()
    sink_id = logger.add(buf, format="{message}", level="DEBUG")
    try:
        yield buf
    finally:
        try:
            logger.remove(sink_id)
        except ValueError:
            pass


def test_run_script_normal_completion(capsys: pytest.CaptureFixture[str]) -> None:
    # 正常完成的脚本：stdout 应原样打印到当前 stdout
    notify.run_script(f"{shlex.quote(sys.executable)} -c \"print('hello-from-script')\"")
    captured = capsys.readouterr()
    assert "hello-from-script" in captured.out


def test_run_script_timeout_kills_process(
    short_timeout: None,
    log_capture: io.StringIO,
) -> None:
    # 挂起脚本（sleep 30）必须在 1 秒超时后被 kill，且 log 记超时
    started = time.monotonic()
    notify.run_script(f'{shlex.quote(sys.executable)} -c "import time; time.sleep(30)"')
    elapsed = time.monotonic() - started
    # 超时 1 秒 + 杀树 + 二次 communicate 回收管道；余量由生产常量推导：超时 +
    # _TREE_KILL_TIMEOUT_SECONDS（杀树路径自身的设计预算）+ 2s 调度余量，杀树吃满预算也不误报。
    # 固定的 4.0s 手拍余量已被实测证伪：taskkill.exe 杀一个真实进程稳定 2.5~3.1s，1s + 3s ≈ 4.01s
    # 恰好撞线。进程树与超时分支的逐条锁在 tests/test_notify_script_guard.py，本文件只管
    # 「超时后确实不再占着调用线程」。
    kill_budget = notify._SCRIPT_TIMEOUT_SECONDS + notify._TREE_KILL_TIMEOUT_SECONDS + 2.0
    assert elapsed < kill_budget, f"超时未被强制回收: elapsed={elapsed:.2f}s"
    log_text = log_capture.getvalue()
    assert "执行自定义脚本超时" in log_text


def test_run_script_bad_command_logs_oserror(log_capture: io.StringIO) -> None:
    # 不存在的命令应被 shlex.Popen 触发 OSError 路径（Windows FileNotFoundError / Linux [Errno 2]）
    notify.run_script("__definitely_not_a_real_binary_42__")
    log_text = log_capture.getvalue()
    # 不强求精确匹配 "FileNotFoundError" / "No such file"：跨平台文案差异大
    # 仅要求确实进入 OSError 分支并记录了失败
    assert "执行自定义脚本失败" in log_text


def test_run_script_uses_safe_split_not_shell(log_capture: io.StringIO) -> None:
    # 验证 run_script 不走 shell=True：传入明显会触发 shell 展开的字符串时，
    # 子进程 Popen 收到的应是 list[str]（来自 shlex.split），若 shlex 解析失败应被 except ValueError 接住
    # 这里用一个会被 shlex 报 ValueError 的非闭合引号
    notify.run_script('echo "unterminated')
    log_text = log_capture.getvalue()
    assert "脚本命令解析失败" in log_text


# 通知标题解析（build_push_title）：标题必须带得出主播名。
#
# 背景（2026-10-22）：标题原先恒等于 push_message_title，未配置时是写死的常量
# 「直播间状态更新通知」——Bark 只把 title 显示成通知首行，锁屏/横幅上看不到是哪个主播，
# 主播名只躺在正文里被折叠。故标题也走同一套模板占位符（2026-10-22 起为 ASCII
# {ROOM_NAME}/{TIME}：机器字段永不进翻译层，中文 [直播间名称]/[时间] 仅作旧配置兼容别名）。
def test_build_push_title_default_carries_room_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # 未配置自定义标题时，兜底标题必须含 record_name（而不是死文本）
    monkeypatch.setattr(state, "push_message_title", "")
    assert notify.build_push_title("序号37 哈那TωTira", "2026-10-22 18:11:45") == "序号37 哈那TωTira 正在直播中"


def test_build_push_title_replaces_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    # 自定义标题里的占位符按约定替换：正文与标题共用 {ROOM_NAME}/{TIME} 两个记号
    monkeypatch.setattr(state, "push_message_title", "开播啦：{ROOM_NAME} @ {TIME}")
    title = notify.build_push_title("序号37 哈那TωTira", "2026-10-22 18:11:45")
    assert title == "开播啦：序号37 哈那TωTira @ 2026-10-22 18:11:45"


def test_build_push_title_accepts_legacy_chinese_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    # 旧配置（改了占位符语法之前写下的）继续可用：中文别名在渲染时同样展开，
    # 不需要用户去 config.ini 里改一个字符
    monkeypatch.setattr(state, "push_message_title", "开播啦：[直播间名称] @ [时间]")
    title = notify.build_push_title("序号37 哈那TωTira", "2026-10-22 18:11:45")
    assert title == "开播啦：序号37 哈那TωTira @ 2026-10-22 18:11:45"


def test_build_push_title_without_placeholder_is_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    # 没写占位符的自定义标题保持原样：不给老用户的通知标题强插主播名（对齐「只加占位符支持」的取舍）
    monkeypatch.setattr(state, "push_message_title", "直播间状态更新通知")
    assert notify.build_push_title("序号37 哈那TωTira", "2026-10-22 18:11:45") == "直播间状态更新通知"


def test_build_push_title_survives_braces_in_room_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # 主播昵称带花括号是常态（如「兔{酱}」）；渲染走 str.replace 而非 str.format，才不会被当占位符炸掉
    monkeypatch.setattr(state, "push_message_title", "{ROOM_NAME} 开播")
    assert notify.build_push_title("序号1 兔{酱}", "t") == "序号1 兔{酱} 开播"


def test_push_message_sends_room_name_as_bark_title(monkeypatch: pytest.MonkeyPatch) -> None:
    # 端到端：push_message 传给 bark 的 title 必须带主播名，body 维持 main.py 拼好的正文
    monkeypatch.setattr(state, "push_message_title", "")
    monkeypatch.setattr(state, "live_status_push", "BARK")
    sent: dict[str, object] = {}

    def fake_bark(api: str, title: str = "", content: str = "", **kwargs: object) -> dict[str, list[str | int]]:
        sent.update(title=title, content=content)
        return {"success": [api], "error": []}

    monkeypatch.setattr(notify, "bark", fake_bark)
    notify.push_message(
        "序号37 哈那TωTira",
        "https://live.douyin.com/123",
        "直播间状态更新：序号37 哈那TωTira 正在直播中，时间：2026-10-22 18:11:45",
    )
    assert sent["title"] == "序号37 哈那TωTira 正在直播中"
    assert "序号37 哈那TωTira" in str(sent["content"])


# 跨语言锁（2026-10-22 的真实回归）：占位符曾经是中文的 [直播间名称]/[时间]，且被写进了译文
# （en_US 的 "[Room name]"、zh_TW 的 "[直播間名稱]"）。tr() 一命中目录，代码里的 replace 就匹配
# 不上，英文界面下推送标题退化成 "[Room name] is now live"。此处逐语言断言「渲染后既没有漏替换的
# 占位符、也没有被翻译的字段」，坏掉会立刻红。
@pytest.mark.parametrize("lang", ["zh_CN", "en_US"])
def test_build_push_title_keeps_placeholder_out_of_translation(monkeypatch: pytest.MonkeyPatch, lang: str) -> None:
    from src.support import i18n

    original = i18n.get_language()
    monkeypatch.setattr(state, "push_message_title", "")
    try:
        i18n.set_language(lang)
        title = notify.build_push_title("序号37 哈那TωTira", "2026-10-22 18:11:45")
    finally:
        i18n.set_language(original)
    assert "序号37 哈那TωTira" in title, f"{lang}: 标题里没有主播名 -> {title!r}"
    for leaked in ("ROOM_NAME", "Time]", "[Room name]", "[直播間名稱]", "[時間]", "{"):
        assert leaked not in title, f"{lang}: 占位符泄漏进通知文案 -> {title!r}"
