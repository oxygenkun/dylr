# ffmpeg -reconnect* 输入选项「带值 + 位于 -i 之前」回归测试（2026-09-11 事故沉淀）。
#
# 不变量：
#   1. 每个 -reconnect* 后必须紧跟一个非选项名的字面量取值；缺值时 ffmpeg 把下一个选项名
#      当作值（「Unable to parse "reconnect_streamed" option value "-reconnect_at_eof" as boolean」），
#      输入未打开即以 -22 退出；
#   2. -reconnect* 必须全部位于 -i 之前（input 级选项归属输入侧）：置于 -i 之后 ffmpeg
#      静默接受、退出码 0，重连完全失效且无可见症状；
#   3. m3u8 输入必须移除 -reconnect_at_eof——hls demuxer 依赖播放列表读到 EOF 才拉媒体段，
#      该选项令 http 层在 EOF 处无限重连（1/3/7/15/31/60s 指数退避），媒体段零字节、进程永不退出。
#      故 m3u8 在命令构造处删除该参数对；FLV 保留（CDN 掐断长连接时在 EOF 处重连续写同一文件）。
#
# 字面量错位能通过格式检查、类型检查与 AST 等价性校验，只有这类语义断言能拦住。

import ast
from pathlib import Path

_MAIN_PATH = Path(__file__).resolve().parent.parent / "src/recording/session.py"
# 同类回归的第二处定义点：tool/dylr_standalone.py 的两处录制命令
# 同样拼接 -reconnect*，与 main.py 保持一字不差是对它的既有要求，一并纳入防线。
_STANDALONE_PATH = Path(__file__).resolve().parent.parent / "tool" / "dylr_standalone.py"


# 取「-reconnect* 选项常量在其所属 List 字面量中的位置」清单：
# (list_node, index)。ffmpeg 命令均为函数内的 list 字面量，AST 扫描即可覆盖。
def _reconnect_entries(path: Path) -> list[tuple[ast.List, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    entries: list[tuple[ast.List, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        for index, item in enumerate(node.elts):
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                if item.value.startswith("-reconnect"):
                    entries.append((node, index))
    return entries


class TestReconnectOptionsHaveValues:
    # 不变量一：每个 -reconnect* 后必须紧跟字面量取值——缺值时 ffmpeg 把下一个选项名
    # 当作值，boolean 解析直接失败

    def test_scan_finds_all_definition_points(self) -> None:
        # 扫描必须命中预期条目（main.py 3 个选项、standalone 2 处命令 × 3 选项），
        # 否则下面的断言会退化成空跑的假绿
        assert len(_reconnect_entries(_MAIN_PATH)) == 3, "src/main.py 的 -reconnect* 选项数量变化"
        assert len(_reconnect_entries(_STANDALONE_PATH)) == 6, "standalone 的 -reconnect* 选项数量变化"

    def test_every_reconnect_option_has_literal_value(self) -> None:
        for path in (_MAIN_PATH, _STANDALONE_PATH):
            for node, index in _reconnect_entries(path):
                label = ast.unparse(node.elts[index])
                if index + 1 >= len(node.elts):
                    raise AssertionError(f"{path.name}: {label} 缺少取值（位于列表末尾）")
                value_node = node.elts[index + 1]
                assert isinstance(value_node, ast.Constant), (
                    f"{path.name}: {label} 后必须紧跟字面量取值，发现: {ast.unparse(value_node)}"
                )
                assert isinstance(value_node.value, str) and not value_node.value.startswith("-"), (
                    f"{path.name}: {label} 的取值不能是选项名: {ast.unparse(value_node)}"
                )


class TestReconnectBeforeInputFlag:
    # 不变量二：-reconnect* 属 input 级(HTTP 协议)选项，必须位于 -i 之前。
    # 置于 -i 之后：ffmpeg 静默接受、退出码 0，输入侧从未应用，重连完全失效且无可见症状。

    def test_reconnect_options_precede_i_flag(self) -> None:
        for path in (_MAIN_PATH, _STANDALONE_PATH):
            for node, index in _reconnect_entries(path):
                i_indices = [
                    i for i, item in enumerate(node.elts) if isinstance(item, ast.Constant) and item.value == "-i"
                ]
                assert i_indices, f"{path.name}: 命令列表缺少 -i: {[ast.unparse(e) for e in node.elts[:5]]}..."
                label = ast.unparse(node.elts[index])
                assert index < min(i_indices), f"{path.name}: {label} 必须位于 -i 之前（input 级选项归属输入侧）"


# 取「'.m3u8' in <url> 判定 + 函数体删除 -reconnect_at_eof 参数对」的守卫清单：
# 事故三的防线。守卫 = If 节点（test 含 .m3u8）且函数体同时含 reconnect_at_eof
# 引用与 del 语句。
def _hls_drop_guards(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guards: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or ".m3u8" not in ast.unparse(node.test):
            continue
        body_src = "\n".join(ast.unparse(s) for s in node.body)
        if "reconnect_at_eof" in body_src and any(isinstance(s, ast.Delete) for s in node.body):
            guards.append(ast.unparse(node))
    return guards


# 取「选项名恰好等于 opt 的位置清单」：(list_node, index)。
def _option_positions(path: Path, opt: str) -> list[tuple[ast.List, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    entries: list[tuple[ast.List, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        for index, item in enumerate(node.elts):
            if isinstance(item, ast.Constant) and item.value == opt:
                entries.append((node, index))
    return entries


# 本机录制命令省略版本敏感的队列调优参数。
# [历史注 2026-10-22] 先前输出侧断言不兼容 Docker 自带的 FFmpeg 5.1，现锁定不设置该可选参数。


class TestOptionalQueueCompatibility:
    # The shipped Debian FFmpeg 5.1 rejects this optional output flag before writing any data.
    # Leave queue tuning to FFmpeg defaults so the same command works across runtime versions.
    def test_recorder_omits_version_specific_queue_option(self) -> None:
        assert not _option_positions(_MAIN_PATH, "-thread_queue_size")

    def test_standalone_omits_version_specific_queue_option(self) -> None:
        assert not _option_positions(_STANDALONE_PATH, "-thread_queue_size")


class TestReconnectAtEofDroppedForHls:
    # 不变量三：m3u8 输入必须移除 -reconnect_at_eof 参数对，且每个命令定义点都要有守卫
    # （main.py 1 处；standalone 的 build_ffmpeg_cmd 与 run_ffmpeg 两处须同步，任一缺失即命令不一致）。

    def test_main_py_drops_reconnect_at_eof_for_hls(self) -> None:
        guards = _hls_drop_guards(_MAIN_PATH)
        assert guards, "src/main.py 缺少「m3u8 输入移除 -reconnect_at_eof」守卫（HLS 录制会无限挂起）"

    def test_standalone_drops_reconnect_at_eof_for_hls_both_sites(self) -> None:
        guards = _hls_drop_guards(_STANDALONE_PATH)
        assert len(guards) == 2, (
            "standalone 的 build_ffmpeg_cmd 与 run_ffmpeg 两处命令必须同步移除"
            f"-reconnect_at_eof（m3u8 输入），实际守卫数: {len(guards)}"
        )


class TestLiveInputPacing:
    # 直播输入不得加 -re：它按时间戳节流（本用于文件实时回放），直播流断点/时间戳回跳时
    # ffmpeg 会空等——进程还在、输出文件不再增长。2026-11-03 真机：50s 上限只落 34.6s
    # （视频包止于 27.7s、音频 36s）；移除后用同一房间复验。

    def test_re_flag_absent_from_both_command_builders(self) -> None:
        for path in (_MAIN_PATH, _STANDALONE_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            literals = [
                node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            assert "-re" not in literals, f"{path.name} 仍在使用 -re 钳制直播输入"
