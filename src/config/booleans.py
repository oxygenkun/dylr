from __future__ import annotations

__all__ = ["TRUE_TOKENS", "FALSE_TOKENS", "parse_config_bool", "format_config_bool"]

TRUE_TOKENS: frozenset[str] = frozenset({"是", "true", "t", "yes", "y", "on", "1"})
FALSE_TOKENS: frozenset[str] = frozenset({"否", "false", "f", "no", "n", "off", "0"})


def parse_config_bool(raw: str | bool | int | None, default: bool) -> bool:
    """把配置值解析为布尔。识别集合以上方 TRUE/FALSE_TOKENS 为准（strip + lower 后比较，。"""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    token = str(raw).strip().lower()
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    return default


def format_config_bool(value: bool) -> str:
    """把布尔值序列化为本仓配置的规范写法「是」/「否」。"""
    return "是" if value else "否"
