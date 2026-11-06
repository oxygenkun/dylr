# i18n 待翻译串提取器：AST 扫描运行时代码（main/web/msg_push/i18n + src/ 全部 .py），
# 提取 print() 的全部常量实参、logger.*() 与 i18n.tr() 的首参（常量串或 f-string 模板底稿），
# 与英文目录（en_US.json）比对并打印缺失清单。
# 只读工具、仅供维护期使用：不进运行时链路，也不在 AGENTS.md 的检查命令块里（缺失靠人工跟进）。
#
# 当前**扫不到的盲区**（AGENTS.md 第 14 条同源）——新增这类用户可见文案必须手工登记进四语
# JSON 目录，否则本脚本会一直报「0 缺失」的假绿：
#   ① color_obj.print_colored(...)（main.py 彩色控制台输出，不是 print 调用）；
# 另两个边界：web/app.js 自带四套内嵌目录（与后端目录互不相干，不在扫描面内，须手工五处同改）；
# 「疑似冗余」段只是参考级——孤儿 msgid（目录比代码多）不会变红，改源码删功能时要人工回查。
#
# 原盲区③「推送正文（msg_push.py 裸字面量 + replace 模板）」自 2026-10-22 起**已收编**：推送模板
# 统一经 msg_push.render_push_content/render_push_template 渲染，前者是「查表」入口，其首参常量
# 与 tr() 同义（占位符为 ASCII {ROOM_NAME}/{TIME}，永不进译文），故一并纳入本扫描面。
#
# f-string 模板还原约定（与既有目录一致）：
#   1. 丢弃格式说明符：{_backoff:.0f} → {_backoff}
#   2. 丢弃转换符：{value!r} → {value}
#   3. 表达式内双引号转单引号：{d.get("k", "v")} → {d.get('k', 'v')}（po msgid 内双引号需转义）
#   4. 隐式拼接（相邻字符串/f-string 字面量）按语义合并为单条模板
#   5. i18n.tr(模板, **kw) 的首参常量串即模板（占位符已是标识符形态）——不收录会让 tr 化后的
#      调用点从提取结果中消失，缺失检测退化为假绿（f-string 与 tr 双形态并存期必须都扫）
#   4/5 只递归**首参/常量实参**：实参位的 f-string（`_backoff=f"{v:.0f}"`）发生在查表之后、
#   不影响 msgid，故不收录。

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_FILES = sorted((ROOT / "src").rglob("*.py"))

# 只有这些 logger 方法名的首参被视为「可翻译串」。tests/test_i18n_migration.py 里另有一份
# 同名集合并注明与之同口径——改这里必须同时改那里，否则检查判的与提取的又是两套。
LOGGER_METHODS = {
    "debug",
    "info",
    "success",
    "warning",
    "error",
    "critical",
    "exception",
    "trace",
    "log",
    "catch",
}


def formatted_value_template(node: ast.FormattedValue, source: str) -> str:
    # 还原 {expr} 占位符：丢格式说明符与转换符、表达式内双引号转单引号
    seg = ast.get_source_segment(source, node.value) or ""
    seg = seg.replace('"', "'")
    return "{" + seg + "}"


def extract_constant_or_template(node: ast.AST, source: str) -> str | None:
    # 常量串直接返回；f-string（含隐式拼接）按 values 重建模板；其余返回 None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append(formatted_value_template(value, source))
        return "".join(parts) if parts else None
    return None


# i18n.tr(...) 的调用形态：裸 tr 或 <别名>.tr。别名集合必须覆盖仓内全部写法，
# 否则该调用点会从提取结果消失，缺失检测对它退化为假绿。
TR_CALLER_IDS = {"i18n", "i18n_module"}

# 会「查表」的推送模板渲染入口：msg_push.render_push_content(首参常量, ...) 等价于 tr(首参)。
# render_push_template 不在其列——它渲染的是用户 config 里手写的文本，本就不是目录条目。
PUSH_RENDER_CALLER_IDS = {"msg_push"}


def _is_push_render_call(func: ast.expr) -> bool:
    # msg_push.render_push_content(...)：模板本体要进四语目录，故首参按 tr 处理
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "render_push_content"
        and isinstance(func.value, ast.Name)
        and func.value.id in PUSH_RENDER_CALLER_IDS
    )


def scan_file(path: Path) -> set[str]:
    # 单文件扫描：print 的每个实参、logger.* 与 tr 的首参，各按规则 1~5 还原成模板形态。
    # 已知局限（MID-68 同源）：只认「常量串 / 单个 f-string」两种首参，`f"A" + (f"B" if x else "")`
    # 这种 ast.BinOp 形态这里看不见——所以「目录覆盖完整」对本脚本是盲区，只能由
    # tests/test_i18n_migration.py 的收紧判据（首参**子树**）兜住；新增形参日志一律写 tr(常量模板)。
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            for arg in node.args:
                text = extract_constant_or_template(arg, source)
                if text is not None:
                    found.add(text)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOGGER_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and node.args
        ):
            text = extract_constant_or_template(node.args[0], source)
            if text is not None:
                found.add(text)
        # i18n.tr(模板, **kw) / tr(模板, **kw)：首参常量串即模板（规则 5）
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_tr = (isinstance(func, ast.Name) and func.id == "tr") or (
                isinstance(func, ast.Attribute)
                and func.attr == "tr"
                and isinstance(func.value, ast.Name)
                and func.value.id in TR_CALLER_IDS
            )
            if is_tr or _is_push_render_call(func):
                text = extract_constant_or_template(node.args[0], source)
                if text is not None:
                    found.add(text)

    return found


def is_valuable(text: str) -> bool:
    # 过滤无翻译价值项：纯符号装饰线 / 纯占位符无自然语言（与既有收录约定一致）
    if not text.strip():
        return False
    if set(text.strip()) <= set("=-.*#|+_/ \t\r\n"):
        return False
    # 剥掉 {expr} 占位符块后须残留自然语言（CJK 或字母词），否则属纯占位符
    # 模板（如 {color}{text}{Color.RESET} / {rec_info}/{filename}），无翻译价值。
    # 不能先剥花括号再查字母：占位符表达式内的标识符（color/Color）也是字母，会把纯模板
    # 误判为有价值——必须以「花括号块之外」的残渣为准。
    residue = re.sub(r"\{[^{}]*\}", "", text)
    return any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in residue)


def load_catalog_keys() -> dict[str, set[str]]:
    path = ROOT / "i18n" / "en_US.json"
    return {path.name: set(json.loads(path.read_text(encoding="utf-8")))}


def main() -> int:
    # 恒退 0：本脚本是「报告器」而不是检查判据（AGENTS.md 的检查命令块里也没有它），
    # 缺失条目由维护者跟进；真正会让 CI 变红的是 tests/test_i18n_migration.py 的三条不变量。
    runtime_strings: set[str] = set()
    for path in SCAN_FILES:
        if not path.exists():
            continue
        runtime_strings |= scan_file(path)
    runtime_strings = {s for s in runtime_strings if is_valuable(s)}

    catalogs = load_catalog_keys()
    english_keys = catalogs["en_US.json"]

    missing = sorted(runtime_strings - english_keys)
    stale = sorted(english_keys - runtime_strings)

    print(f"运行时提取（有价值）串：{len(runtime_strings)} 条")
    print(f"en_US.json 现有条目：{len(english_keys)} 条")
    print(f"\n== 缺失（运行时有、目录无，{len(missing)} 条）==")
    for s in missing:
        print(f"  + {s!r}")
    print(f"\n== 疑似冗余（目录有、运行时无，{len(stale)} 条，含历史/兼容条目，仅参考）==")
    for s in stale[:25]:
        print(f"  - {s!r}")
    if len(stale) > 25:
        print(f"  …（另 {len(stale) - 25} 条省略）")
    _ = sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
