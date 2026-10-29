"""配置文件的读取、原子修改、去重与 URL 更新。"""

import io
import os
import re
from collections import OrderedDict
from pathlib import Path

from src.config.reader import tolerant_config_parser
from src.recording import state
from src.support import i18n
from src.support.logger import logger
from src.support.utils import atomic_write_text, mask_credentials


def read_ini_value(file_path: str | Path, section: str, key: str) -> str | None:
    """读取指定配置项；缺失或读取失败时返回 None。"""
    config = tolerant_config_parser()

    try:
        _ = config.read(file_path, encoding="utf-8-sig")
    except Exception as e:
        logger.warning(i18n.tr("Error occurred while reading the configuration file: {e}", e=e))
        return None

    if section in config:
        if key in config[section]:
            return config[section][key]
        logger.warning(i18n.tr("Key [{key}] does not exist in section [{section}].", key=key, section=section))
    else:
        logger.warning(i18n.tr("Section [{section}] does not exist in the file.", section=section))

    return None


def update_config(file_path: str | Path, section: str, key: str, new_value: str) -> None:
    """持锁修改配置项，并原子写回文件。"""
    with state.file_update_lock:
        config = tolerant_config_parser()

        try:
            _ = config.read(file_path, encoding="utf-8-sig")
        except Exception as e:
            logger.warning(i18n.tr("An error occurred while reading the configuration file: {e}", e=e))
            return

        if section not in config:
            logger.warning(i18n.tr("Section [{section}] does not exist in the file.", section=section))
            return

        config[section][key] = new_value

        buf = io.StringIO()
        config.write(buf)
        if not atomic_write_text(file_path, buf.getvalue()):
            return
    try:
        os.chmod(str(file_path), 0o600)
    except OSError:
        pass
    logger.debug(
        i18n.tr(
            "The value of {key} under [{section}] in the configuration file has been updated.",
            key=key,
            section=section,
        )
    )


def remove_duplicate_lines(file_path: str | Path) -> None:
    """持锁去重配置行，保留首次出现顺序与行尾。"""
    unique_lines: OrderedDict[str, str] = OrderedDict()
    text_encoding = "utf-8-sig"
    with state.file_update_lock:
        try:
            with open(file_path, "r", encoding=text_encoding, newline="") as input_file:
                for line in input_file:
                    _ = unique_lines.setdefault(line.rstrip("\r\n"), line)
        except UnicodeDecodeError:
            unique_lines.clear()
            with open(file_path, "r", encoding=text_encoding, errors="surrogateescape", newline="") as input_file:
                for line in input_file:
                    _ = unique_lines.setdefault(line.rstrip("\r\n"), line)
        out = "".join(unique_lines.values())
        if out and not out.endswith(("\n", "\r")):
            out += "\n"
        _ = atomic_write_text(file_path, out, encoding=text_encoding, errors="surrogateescape")


def rewrite_line_by_segment(text_line: str, old_str: str, new_str: str) -> str | None:
    """仅替换匹配的整行或逗号分隔字段，保留行尾。"""
    raw = text_line.rstrip("\r\n")
    eol = text_line[len(raw) :]
    target = old_str.strip()
    if not target:
        return None
    cleaned_new = new_str.rstrip("\r\n")
    if raw.strip() == target:
        return f"{cleaned_new}{eol}"
    parts = re.split(r"([,，])", raw)
    hit = False
    for i in range(0, len(parts), 2):
        if parts[i].strip() == target:
            parts[i] = cleaned_new
            hit = True
    if not hit:
        return None
    return "".join(parts) + eol


def replace_url(file_path: str | Path, old: str, new: str) -> None:
    """持锁精确替换 URL 配置字段并原子写回。"""
    with state.file_update_lock:
        try:
            with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(
                i18n.tr(
                    "An error occurred: {masked}",
                    masked=mask_credentials(f"读取配置文件失败，跳过 URL 替换: {type(e).__name__}: {e} in {file_path}"),
                )
            )
            return
        out_lines: list[str] = []
        for line in lines:
            rewritten = rewrite_line_by_segment(line, old, new)
            if rewritten is not None:
                if not rewritten.endswith("\n"):
                    rewritten += "\n"
                out_lines.append(rewritten)
            else:
                out_lines.append(line)
        _ = atomic_write_text(file_path, "".join(out_lines), encoding="utf-8-sig")
