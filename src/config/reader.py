import configparser
import os
from pathlib import Path
from typing import Final

from src.support import i18n
from src.support.logger import logger

TEXT_ENCODING = "utf-8-sig"

_DUP_WARNED: set[tuple[str, float]] = set()
_DUP_WARN_CACHE_MAX: Final[int] = 32


def tolerant_config_parser(preserve_case: bool = False) -> configparser.ConfigParser:
    """构造「同一节内落出两行同名键时，仍能读出全部其它配置」的解析器。"""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    if preserve_case:
        parser.optionxform = str
    return parser


def find_duplicate_options(text: str) -> list[tuple[str, str, tuple[int, ...]]]:
    """枚举「同一节内出现两次以上的键」：返回 [(节名, 键名, (行号, ...))]，行号自 1 起、按出现顺序。"""
    buckets: dict[tuple[str, str], list[tuple[int, str]]] = {}
    section = ""
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if not any(delimiter in line for delimiter in ("=", ":")):
            continue
        head = line.split("=", 1)[0].split(":", 1)[0].strip()
        if not head:
            continue
        buckets.setdefault((section, head.casefold()), []).append((lineno, head))
    return [
        (section, items[0][1], tuple(lineno for lineno, _ in items))
        for (section, _lower), items in buckets.items()
        if len(items) > 1
    ]


def warn_duplicate_options(config_file: str | Path, encoding: str = TEXT_ENCODING) -> int:
    """读取 config.ini 并把重复键落一条 warning；返回重复键的条数（0 = 文件干净，或读不出内容）。"""
    try:
        mtime = os.stat(config_file).st_mtime
        with open(config_file, encoding=encoding, newline="") as fp:
            text = fp.read()
    except OSError, UnicodeDecodeError:
        return 0
    duplicates = find_duplicate_options(text)
    if not duplicates:
        return 0
    marker = (os.path.abspath(str(config_file)), mtime)
    if marker not in _DUP_WARNED:
        if len(_DUP_WARNED) >= _DUP_WARN_CACHE_MAX:
            _DUP_WARNED.clear()
        _DUP_WARNED.add(marker)
        detail = "; ".join(
            f"[{section}] {option} @ {', '.join(str(lineno) for lineno in lines)}"
            for section, option, lines in duplicates
        )
        logger.warning(i18n.tr("config.ini 存在重复配置项，已按最后一次取值读取: {detail}", detail=detail))
    return len(duplicates)
