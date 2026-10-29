from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.support import i18n


def _as_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(i18n.tr("字段 {field} 必须是字符串", field=field))
    return value


def _as_opt_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(i18n.tr("字段 {field} 必须是字符串或留空", field=field))
    return value


def _as_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(i18n.tr("字段 {field} 必须是布尔值", field=field))
    return value


def _as_str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(i18n.tr("字段 {field} 必须是字符串列表", field=field))
    for item in value:
        if not isinstance(item, str):
            raise ValueError(i18n.tr("字段 {field} 的元素必须是字符串", field=field))
    return list(value)


def _require_dict(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(i18n.tr("请求体必须是 JSON 对象"))
    return data


@dataclass
class LoginRequest:
    password: str

    @classmethod
    def parse(cls, data: Any) -> LoginRequest:
        body = _require_dict(data)
        return cls(password=_as_str(body.get("password"), "password"))


@dataclass
class RoomCreate:
    url: str
    quality: str | None = None
    name: str | None = None

    @classmethod
    def parse(cls, data: Any) -> RoomCreate:
        body = _require_dict(data)
        return cls(
            url=_as_str(body.get("url"), "url"),
            quality=_as_opt_str(body.get("quality"), "quality"),
            name=_as_opt_str(body.get("name"), "name"),
        )


@dataclass
class RoomUpdate:
    old_url: str
    url: str
    quality: str | None = None
    name: str | None = None

    @classmethod
    def parse(cls, data: Any) -> RoomUpdate:
        body = _require_dict(data)
        return cls(
            old_url=_as_str(body.get("old_url"), "old_url"),
            url=_as_str(body.get("url"), "url"),
            quality=_as_opt_str(body.get("quality"), "quality"),
            name=_as_opt_str(body.get("name"), "name"),
        )


@dataclass
class RoomToggle:
    url: str
    enable: bool

    @classmethod
    def parse(cls, data: Any) -> RoomToggle:
        body = _require_dict(data)
        return cls(
            url=_as_str(body.get("url"), "url"),
            enable=_as_bool(body.get("enable"), "enable"),
        )


@dataclass
class RoomQualityUpdate:
    url: str
    quality: str | None = None

    @classmethod
    def parse(cls, data: Any) -> RoomQualityUpdate:
        body = _require_dict(data)
        return cls(
            url=_as_str(body.get("url"), "url"),
            quality=_as_opt_str(body.get("quality"), "quality"),
        )


@dataclass
class QualityOptionsUpdate:
    options: list[str]

    @classmethod
    def parse(cls, data: Any) -> QualityOptionsUpdate:
        body = _require_dict(data)
        return cls(options=_as_str_list(body.get("options"), "options"))


@dataclass
class RecordingToggle:
    enable: bool

    @classmethod
    def parse(cls, data: Any) -> RecordingToggle:
        body = _require_dict(data)
        return cls(enable=_as_bool(body.get("enable"), "enable"))


@dataclass
class ConfigUpdate:
    section: str
    key: str
    value: str
    reauth_password: str | None = None

    @classmethod
    def parse(cls, data: Any) -> ConfigUpdate:
        body = _require_dict(data)
        return cls(
            section=_as_str(body.get("section"), "section"),
            key=_as_str(body.get("key"), "key"),
            value=_as_str(body.get("value"), "value"),
            reauth_password=_as_opt_str(body.get("reauth_password"), "reauth_password"),
        )


@dataclass
class LanguageUpdate:
    language: str

    @classmethod
    def parse(cls, data: Any) -> LanguageUpdate:
        body = _require_dict(data)
        return cls(language=_as_str(body.get("language"), "language"))
