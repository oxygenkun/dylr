"""TOML 配置存储：pydantic 校验、热加载与旧 INI 迁移。"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import tomllib
from pathlib import Path
from typing import Annotated

import tomlkit
from pydantic import BeforeValidator, TypeAdapter, ValidationError
from tomlkit.items import Table

from src.config.booleans import FALSE_TOKENS, TRUE_TOKENS
from src.support import i18n
from src.support.logger import logger

type Scalar = bool | int | float | str


class ConfigError(RuntimeError):
    """配置文件不可解析或结构非法。"""


class ConfigValueError(ConfigError):
    """单个配置项的值不能转换为该键声明的类型。"""


# 已知类型键表：未列出的键按字符串处理。新增键时若代码用 read_config_bool / _safe_int 读取，须补到这里。
_BOOL_KEYS: dict[str, frozenset[str]] = {
    "录制设置": frozenset(
        {
            "是否跳过代理检测(是/否)",
            "是否启用日志文件(是/否)",
            "是否启动时清理陈旧字节码缓存(是/否)",
            "是否自动更新主播名(是/否)",
            "保存文件夹是否以作者区分",
            "保存文件夹是否以时间区分",
            "保存文件夹是否以标题区分",
            "保存文件名是否包含标题",
            "是否去除名称中的表情符号",
            "是否使用代理ip(是/否)",
            "是否显示循环秒数",
            "是否显示直播源地址",
            "分段录制是否开启",
            "是否启用hls采集(是/否)",
            "是否启用https录制",
            "是否强制启用https录制",
            "录制完成后自动转为mp4格式",
            "mp4格式重新编码为h264",
            "追加格式后删除原文件",
            "生成时间字幕文件",
            "是否录制完成后执行自定义脚本",
            "是否录制弹幕(是/否)",
            "是否弹幕监控(是/否)",
            "抖音私信分享自动录制(是/否)",
            "抖音私信自动录制仅在播(是/否)",
            "是否禁用SSL证书验证(是/否)",
            "虎牙是否禁用SSL证书验证(是/否)",
        }
    ),
    "推送配置": frozenset(
        {
            "钉钉通知@全体(是/否)",
            "是否使用smtp服务ssl加密(是/否)",
            "只推送通知不录制(是/否)",
            "开播推送开启(是/否)",
            "关播推送开启(是/否)",
        }
    ),
    "Web": frozenset({"web_auth_enable", "web_show_console"}),
}

_INT_KEYS: dict[str, frozenset[str]] = {
    "录制设置": frozenset(
        {
            "同一时间访问网络的线程数",
            "最大同时录制数(0为不限制)",
            "循环时间(秒)",
            "排队读取网址时间(秒)",
            "视频分段时间(秒)",
            "单次录制时长上限(秒,0为不限制)",
            "抖音关注轮询间隔(秒)",
            "抖音私信轮询间隔(秒)",
            "抖音私信自动录制每日上限(个)",
            "抖音私信自动录制冷却时间(秒)",
            "抖音私信自动录制回溯分钟",
        }
    ),
    "推送配置": frozenset({"直播推送检测频率(秒)"}),
    "Web": frozenset({"web_port", "web_token_expiry"}),
}

_FLOAT_KEYS: dict[str, frozenset[str]] = {
    "录制设置": frozenset({"录制空间剩余阈值(gb)", "弹幕分片时长(秒)"}),
}


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in TRUE_TOKENS:
            return True
        if token in FALSE_TOKENS:
            return False
    raise ValueError(f"not a boolean: {value!r}")


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(f"not an integer: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        raise ValueError(f"not an integer: {value!r}")
    if isinstance(value, str):
        text = value.strip()
        body = text[1:] if text[:1] in "+-" else text
        if body.isdigit():
            return int(text)
    raise ValueError(f"not an integer: {value!r}")


def _coerce_float(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError(f"not a number: {value!r}")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError as e:
            raise ValueError(f"not a number: {value!r}") from e
    else:
        raise ValueError(f"not a number: {value!r}")
    if not math.isfinite(result):
        raise ValueError(f"not a finite number: {value!r}")
    return result


_BOOL_ADAPTER = TypeAdapter(Annotated[bool, BeforeValidator(_coerce_bool)])
_INT_ADAPTER = TypeAdapter(Annotated[int, BeforeValidator(_coerce_int)])
_FLOAT_ADAPTER = TypeAdapter(Annotated[float, BeforeValidator(_coerce_float)])
_SCALAR_ADAPTER = TypeAdapter(bool | int | float | str)


def coerce_value(section: str, key: str, value: object) -> Scalar:
    """把单个配置值规范为该键声明的类型；未知键保留标量原值。"""
    try:
        if key in _BOOL_KEYS.get(section, frozenset()):
            return _BOOL_ADAPTER.validate_python(value)
        if key in _INT_KEYS.get(section, frozenset()):
            return _INT_ADAPTER.validate_python(value)
        if key in _FLOAT_KEYS.get(section, frozenset()):
            return _FLOAT_ADAPTER.validate_python(value)
        return _SCALAR_ADAPTER.validate_python(value)
    except ValidationError as e:
        raise ConfigValueError(f"[{section}] {key} 的值不合法: {value!r}") from e


def format_value(value: Scalar) -> str:
    """转换成旧访问器约定的字符串形态（布尔用「是/否」）。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


class ConfigStore:
    """TOML 配置的读写视图；get/items 返回字符串，写入时按类型校验。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._doc: tomlkit.TOMLDocument = tomlkit.document()
        self._values: dict[str, dict[str, Scalar]] = {}
        self._keymap: dict[str, dict[str, str]] = {}
        self._digest: str | None = None
        self._loaded = False
        self._read_error: ConfigError | None = None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def read_error(self) -> ConfigError | None:
        return self._read_error

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self, path: str | Path | None = None) -> None:
        """解析并校验整份配置；抛 ConfigError 表示文件不可用。

        提交是事务式的：解析/校验失败时不改动已加载的文档，热加载失败可继续用旧值。
        """
        if path is not None:
            self.path = Path(path)
        with self._lock:
            try:
                raw = self.path.read_bytes()
                text = raw.decode("utf-8-sig")
                parsed = tomllib.loads(text)
                document = tomlkit.parse(text)
            except OSError as e:
                raise self._fail(f"无法读取配置文件: {e}") from e
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                raise self._fail(f"TOML 解析失败: {e}") from e
            except Exception as e:  # tomlkit 的解析异常类型不统一
                raise self._fail(f"TOML 解析失败: {e}") from e
            values: dict[str, dict[str, Scalar]] = {}
            keymap: dict[str, dict[str, str]] = {}
            for section, table in parsed.items():
                if not isinstance(table, dict):
                    raise self._fail(f"配置节 [{section}] 必须是表（table）")
                bucket: dict[str, Scalar] = {}
                for key, value in table.items():
                    try:
                        bucket[key] = coerce_value(section, key, value)
                    except ConfigValueError:
                        # 非法值保留原文，交给调用方的默认值兜底，不因单键阻断整份配置。
                        bucket[key] = value if isinstance(value, (bool, int, float, str)) else str(value)
                values[section] = bucket
                keymap[section] = {key.casefold(): key for key in bucket}
            self._doc = document
            self._values = values
            self._keymap = keymap
            self._loaded = True
            self._read_error = None
            self._digest = hashlib.sha1(raw).hexdigest()

    def _reset(self) -> None:
        self._doc = tomlkit.document()
        self._values = {}
        self._keymap = {}
        self._digest = None
        self._loaded = False
        self._read_error = None

    def _fail(self, message: str) -> ConfigError:
        error = ConfigError(message)
        if not self._loaded:
            self._reset()
            self._read_error = error
        return error

    def reload_if_changed(self) -> bool:
        """内容未变化时不重新解析；按内容摘要判断，不依赖 mtime（粗粒度文件系统会漏变更）。"""
        try:
            raw = self.path.read_bytes()
        except OSError:
            return False
        if hashlib.sha1(raw).hexdigest() == self._digest:
            return False
        try:
            self.read()
        except ConfigError as e:
            logger.warning(i18n.tr("发生 I/O 或配置解析错误: {err}", err=f"{type(e).__name__}: {e}"))
            return False
        return True

    def has_section(self, section: str) -> bool:
        return section in self._values

    def sections(self) -> list[str]:
        return list(self._values)

    def _actual_key(self, section: str, option: str) -> str | None:
        """INI 时代 optionxform 会把键转小写；TOML 保留大小写，故统一按 casefold 查找。"""
        return self._keymap.get(section, {}).get(option.casefold())

    def has_option(self, section: str, option: str) -> bool:
        return self._actual_key(section, option) is not None

    def get(self, section: str, option: str) -> str:
        actual = self._actual_key(section, option)
        if actual is None:
            raise KeyError(f"{section}/{option}")
        return format_value(self._values[section][actual])

    def native(self, section: str, option: str) -> Scalar:
        actual = self._actual_key(section, option)
        if actual is None:
            raise KeyError(f"{section}/{option}")
        return self._values[section][actual]

    def items(self, section: str) -> list[tuple[str, str]]:
        table = self._values.get(section)
        if table is None:
            raise KeyError(section)
        return [(key, format_value(value)) for key, value in table.items()]

    def set(self, section: str, option: str, value: object) -> None:
        """校验并写入内存中的文档；调用 write() 才落盘。已存在的键按 casefold 复用原写法。"""
        with self._lock:
            coerced = coerce_value(section, option, value)
            table = self._doc.get(section)
            if not isinstance(table, Table):
                table = tomlkit.table()
                self._doc[section] = table
            actual = self._actual_key(section, option) or option
            table[actual] = coerced
            self._values.setdefault(section, {})[actual] = coerced
            self._keymap.setdefault(section, {})[option.casefold()] = actual

    def add_section(self, section: str) -> None:
        with self._lock:
            if not isinstance(self._doc.get(section), Table):
                self._doc[section] = tomlkit.table()
            self._values.setdefault(section, {})

    def remove_option(self, section: str, option: str) -> bool:
        with self._lock:
            actual = self._actual_key(section, option)
            if actual is None:
                return False
            table = self._doc.get(section)
            if isinstance(table, Table) and actual in table:
                del table[actual]
            bucket = self._values.get(section) or {}
            bucket.pop(actual, None)
            self._keymap.get(section, {}).pop(option.casefold(), None)
            return True

    def write(self) -> bool:
        """原子写回 TOML；读取曾失败时拒绝写入，避免用默认值覆盖坏文件。"""
        from src.support.utils import atomic_write_text

        with self._lock:
            if self._read_error is not None:
                return False
            text = tomlkit.dumps(self._doc)
            if not atomic_write_text(self.path, text, encoding="utf-8"):
                return False
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            return True


def migrate_ini_to_toml(ini_path: str | Path, toml_path: str | Path) -> bool:
    """把旧 INI 转成 TOML；返回是否由本次调用生成（TOML 已存在时返回 False）。"""
    ini = Path(ini_path)
    target = Path(toml_path)
    if target.exists() or not ini.is_file():
        return False
    from src.config.reader import tolerant_config_parser, warn_duplicate_options

    parser = tolerant_config_parser()
    parser.read(ini, encoding="utf-8-sig")
    warn_duplicate_options(ini)
    doc = tomlkit.document()
    doc.add(tomlkit.comment("dylr 配置（TOML）。由 config.ini 自动迁移生成。"))
    for section in parser.sections():
        table = tomlkit.table()
        for key, raw in parser.items(section):
            try:
                table[key] = coerce_value(section, key, raw)
            except ConfigValueError:
                table[key] = raw
        doc[section] = table
    from src.support.utils import atomic_write_text

    if not atomic_write_text(target, tomlkit.dumps(doc), encoding="utf-8"):
        return False
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return True


def ensure_config_migrated(config_path: str | Path, legacy_path: str | Path) -> bool:
    """启动期迁移入口：TOML 缺失而 INI 存在时转换并给旧文件改名，避免误改已废弃的配置。"""
    config = Path(config_path)
    legacy = Path(legacy_path)
    if config.exists():
        return False
    config.parent.mkdir(parents=True, exist_ok=True)
    if not legacy.is_file():
        if not config.exists():
            _ = config.write_text(_MIGRATION_HEADER, encoding="utf-8")
        return False
    if not migrate_ini_to_toml(legacy, config):
        return False
    try:
        legacy.replace(legacy.with_suffix(legacy.suffix + ".migrated"))
    except OSError:
        pass
    return True


_MIGRATION_HEADER = "# dylr 配置（TOML）。\n"


def read_config_entry(config_path: str | Path, section: str, key: str) -> str | None:
    """只读读取单个配置项；文件缺失、解析失败或键不存在时返回 None。"""
    store = ConfigStore(config_path)
    try:
        store.read()
    except ConfigError:
        return None
    if not store.has_option(section, key):
        return None
    return store.get(section, key)


__all__ = [
    "ConfigError",
    "ConfigStore",
    "ConfigValueError",
    "Scalar",
    "coerce_value",
    "ensure_config_migrated",
    "format_value",
    "migrate_ini_to_toml",
    "read_config_entry",
]
