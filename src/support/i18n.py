import builtins
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

SUPPORTED_LANGUAGES: dict[str, str] = {
    "zh_CN": "中文",
    "en_US": "English",
}

DEFAULT_LANGUAGE = "zh_CN"

FALLBACK_LANGUAGE = DEFAULT_LANGUAGE

_LANGUAGE_ALIASES: dict[str, str] = {
    "zh-cn": "zh_CN",
    "zh": "zh_CN",
    "zh-hans": "zh_CN",
    "zh-sg": "zh_CN",
    "en": "en_US",
    "en-us": "en_US",
    "en-gb": "en_US",
    "zh-tw": "zh_CN",
    "zh-hant": "zh_CN",
    "zh-hk": "zh_CN",
    "zh-mo": "zh_CN",
}


def is_recognized_language(value: str | None) -> bool:
    """判断语言标识是否可识别（受支持码/已知别名/带编码后缀变体）；不回退默认值。"""
    if not value:
        return False
    v = value.strip()
    if v in SUPPORTED_LANGUAGES:
        return True
    low = v.lower().replace("_", "-")
    if low in _LANGUAGE_ALIASES:
        return True
    prefix = low.split(".")[0].split("@")[0]
    if prefix in _LANGUAGE_ALIASES:
        return True
    return prefix.replace("-", "_") in SUPPORTED_LANGUAGES


def normalize_language(value: str | None) -> str:
    """把任意语言标识归一化为受支持的规范语言码：精确匹配 → 别名表（小写 + 连字符）→。"""
    if not value:
        return DEFAULT_LANGUAGE
    v = value.strip()
    if v in SUPPORTED_LANGUAGES:
        return v
    low = v.lower().replace("_", "-")
    if low in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[low]
    prefix = low.split(".")[0].split("@")[0]
    if prefix in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[prefix]
    if prefix.replace("-", "_") in SUPPORTED_LANGUAGES:
        return prefix.replace("-", "_")
    return DEFAULT_LANGUAGE


def has_catalog(lang: str) -> bool:
    """中文无需目录；英文目录缺失或损坏时不得回报虚假的切换成功。"""
    return lang == DEFAULT_LANGUAGE or bool(_load_translations(locale_path, lang))


def resolve_language(value: str | None) -> str:
    language = normalize_language(value)
    return language if has_catalog(language) else DEFAULT_LANGUAGE


module_dir = Path(__file__).resolve().parents[2]
_meipass = getattr(sys, "_MEIPASS", None)
if os.path.exists(module_dir / "_internal/i18n"):
    locale_path = module_dir / "_internal/i18n"  # PyInstaller 打包版位置
elif _meipass and os.path.exists(Path(_meipass) / "i18n"):
    locale_path = Path(_meipass) / "i18n"  # 冻结运行 data 目录
else:
    locale_path = module_dir / "i18n"  # 源码运行位置

_project_root = os.path.normpath(str(module_dir))


def _should_translate(caller_file: str) -> bool:
    """判断调用者文件是否位于需要翻译的项目源码目录下。"""
    caller_norm = os.path.normpath(caller_file)
    return caller_norm == _project_root or caller_norm.startswith(_project_root + os.sep)


def _load_json_catalog(path: Path) -> dict[str, str] | None:
    """从 JSON 文件加载「原文 → 译文」映射；文件不存在或解析失败时返回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _load_translations(locale_dir: str | Path, lang: str) -> dict[str, str] | None:
    """中文原文无需加载翻译文件。"""
    if lang == DEFAULT_LANGUAGE:
        return None
    return _load_json_catalog(Path(locale_dir) / f"{lang}.json")


def _build_translator(locale_dir: str | Path, lang: str) -> Callable[[str], str]:
    """构建某语言的翻译函数：加载翻译目录，命中返回译文、未命中回退原文。"""
    catalog = _load_translations(locale_dir, lang)
    if not catalog:
        return lambda text: text
    return lambda text: catalog.get(text, text)


_current_language: str = DEFAULT_LANGUAGE

_tr: Callable[[str], str] = _build_translator(locale_path, DEFAULT_LANGUAGE)
original_print = builtins.print  # 保存原始 print 函数


def set_language(lang: str | None) -> str:
    """返回实际生效的语言，供调用方写回配置。"""
    global _current_language, _tr
    effective = resolve_language(lang)
    _current_language = effective
    _tr = _build_translator(locale_path, effective)
    return effective


def get_language() -> str:
    """返回当前语言规范码。"""
    return _current_language


def available_languages() -> dict[str, str]:
    """返回受支持的语言映射（键为语言码、值为显示名），供选择器渲染。"""
    return dict(SUPPORTED_LANGUAGES)


def translated_print(
    *args: object,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """包装 print：对来自源码目录的输出自动翻译后再打印。"""
    try:
        frame = inspect.currentframe()
        caller_file = frame.f_back.f_code.co_filename if frame and frame.f_back else ""
        should_translate = _should_translate(caller_file)
    except ValueError, AttributeError:
        should_translate = False

    translated_args: list[str] = []
    for arg in args:
        text = str(arg)
        if should_translate:
            text = _tr(text)  # 翻译文本
        translated_args.append(text)

    original_print(sep.join(translated_args), sep=sep, end=end, file=file, flush=flush)  # 调用原始 print


def tr(template: str, **kwargs: Any) -> str:
    """带占位符的翻译入口（先查表再插值）。"""
    translated = _tr(template)
    try:
        return translated.format(**kwargs)
    except KeyError, IndexError, ValueError, AttributeError, TypeError:
        try:
            return template.format(**kwargs)
        except KeyError, IndexError, ValueError, AttributeError, TypeError:
            return template
