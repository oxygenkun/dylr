import configparser
import datetime
import io
import math
import os
import re
import shutil
import time

from loguru import logger

from src.config import files as config_files
from src.config.booleans import format_config_bool, parse_config_bool
from src.recording import state
from src.recording.process import _get_error_line
from src.support import i18n, utils


def update_file(file_path: str, old_str: str, new_str: str, start_str: str | None = None) -> str | None:
    """把 file_path 中所有 old_str 替换为 new_str（start_str 非空时给命中行加该前缀，如 "#" 注释掉），。"""
    if old_str == new_str and start_str is None:
        return old_str
    with state.file_update_lock:
        file_data: list[str] = []
        seen: set[str] = set()
        matched = False
        try:
            with open(file_path, "r", encoding=state.text_encoding, newline="") as f:
                for text_line in f:
                    rewritten = config_files.rewrite_line_by_segment(text_line, old_str, new_str)
                    if rewritten is not None:
                        matched = True
                        text_line = f"{start_str}{rewritten}" if start_str else rewritten
                    if text_line not in seen:
                        seen.add(text_line)
                        file_data.append(text_line)
        except (RuntimeError, UnicodeDecodeError, OSError) as e:
            logger.error(
                i18n.tr(
                    "错误信息: {type_name}: {e} 发生错误的行数: {get_error_line}",
                    type_name=type(e).__name__,
                    e=e,
                    get_error_line=_get_error_line(e),
                )
            )
            if state.ini_URL_content:
                _ = _atomic_write_text(file_path, state.ini_URL_content)
                return old_str
            return old_str
        if not file_data:
            return old_str
        if not matched:
            return old_str
        joined = "".join(file_data)
        if not _atomic_write_text(file_path, joined):
            return old_str
        state.ini_URL_content = joined
        return new_str


def _atomic_write_text(file_path: str, text: str) -> bool:
    """原子写文本：同目录临时文件写完后 os.replace 覆盖，读方只会看到旧/新完整内容之一。"""
    ok = utils.atomic_write_text(file_path, text, encoding=state.text_encoding)
    if not ok:
        logger.warning(i18n.tr("原子写失败（已保留原文件）: {file_path}", file_path=file_path))
    return ok


def update_anchor_name(url: str, new_name: str) -> bool:
    """将 URL 配置文件中指定 URL 所在行的主播名字段更新为 new_name（行内无主播名字段时追加），。"""
    if not url or not new_name:
        return False
    with state.file_update_lock:
        if not os.path.exists(state.url_config_file):
            return False
        try:
            with open(state.url_config_file, "r", encoding=state.text_encoding, newline="") as f:
                lines = f.readlines()
        except (RuntimeError, UnicodeDecodeError, OSError) as e:
            logger.error(i18n.tr("读取 URL 配置失败，跳过主播名更新: {e}", e=e))
            return False
        changed = False
        out_lines: list[str] = []
        for raw_line in lines:
            rewritten = _rewrite_anchor_field(raw_line, url, new_name)
            if rewritten is not None:
                changed = True
                out_lines.append(rewritten)
            else:
                out_lines.append(raw_line)
        if not changed:
            return False
        joined = "".join(out_lines)
        if not _atomic_write_text(state.url_config_file, joined):
            logger.warning(i18n.tr("主播名写回 URL 配置失败（已忽略，下轮重试）"))
            return False
        state.ini_URL_content = joined
        return True


def _rewrite_anchor_field(raw_line: str, url: str, new_name: str) -> str | None:
    """重写单行配置的主播名字段；行不匹配 URL 或已是目标名时返回 None（无需变更）。"""
    stripped = raw_line.rstrip("\r\n")
    eol = raw_line[len(stripped) :]
    comment_prefix = ""
    body = stripped
    if body.lstrip().startswith("#"):
        idx = body.find("#")
        comment_prefix = body[: idx + 1]
        body = body[idx + 1 :]
    if not body.strip():
        return None
    segments = [seg.strip() for seg in re.split(r"[,，]", body)]
    if url not in segments:
        return None
    idx = max(body.rfind("主播:"), body.rfind("主播："))
    if idx >= 0:
        head = body[:idx].rstrip()
        if body[idx + 3 :].strip() == new_name:
            return None  # 已是目标名，幂等跳过
        rebuilt = f"{head}主播: {new_name}"
    else:
        rebuilt = f"{body.rstrip()},主播: {new_name}"
    return f"{comment_prefix}{rebuilt}{eol}"


ROOM_ADDED = "added"
ROOM_EXISTS = "exists"
ROOM_DISABLED = "disabled"
ROOM_INVALID = "invalid"
ROOM_WRITE_FAILED = "write_failed"


def append_room_line(url_config_file: str, url: str, quality: str = "", name: str = "") -> str:
    """把一行直播间配置追加到 URL_config.ini；已存在时按启用状态返回 exists/disabled，绝不。"""
    from src.api import config as web_config

    clean_name = re.sub(r"[,，\r\n]+", " ", name or "").strip()[:80]
    try:
        line = web_config.format_url_line(url, quality, clean_name)
    except ValueError:
        return ROOM_INVALID
    target = web_config.normalize_url(url)
    with state.file_update_lock:
        try:
            with open(url_config_file, "r", encoding=state.text_encoding, newline="") as f:
                data = f.read()
        except FileNotFoundError:
            data = ""
        except (OSError, UnicodeDecodeError) as e:
            logger.error(i18n.tr("读取 URL 配置失败，跳过追加: {e}", e=e))
            return ROOM_WRITE_FAILED
        existing = web_config.parse_url_config(url_config_file) if data else []
        for room in existing:
            if web_config.normalize_url(str(room["url"])) == target:
                return ROOM_EXISTS if bool(room["enabled"]) else ROOM_DISABLED
        eol = "\r\n" if "\r\n" in data else "\n"
        if data and not data.endswith(("\n", "\r")):
            data += eol
        data += line + eol
        if not _atomic_write_text(url_config_file, data):
            return ROOM_WRITE_FAILED
        state.ini_URL_content = data
        return ROOM_ADDED


def delete_line(file_path: str, del_line: str, delete_all: bool = False) -> bool:
    """从 file_path 中删除与 del_line 完全相同的行，delete_all=True 时删除全部匹配行；。"""
    with state.file_update_lock:
        try:
            with open(file_path, "r", encoding=state.text_encoding, newline="") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError) as e:
            logger.error(i18n.tr("读取 URL 配置失败，跳过删除: {e}", e=e))
            return False
        target = del_line.rstrip("\r\n")
        deleted_one = False
        out: list[str] = []
        for txt_line in lines:
            if txt_line.rstrip("\r\n") == target and (delete_all or not deleted_one):
                deleted_one = True
                continue
            out.append(txt_line)
        if not deleted_one:
            return False
        joined = "".join(out)
        if not _atomic_write_text(file_path, joined):
            return False
        if file_path == state.url_config_file:
            state.ini_URL_content = joined
        return True


def read_config_value(
    config_parser: configparser.RawConfigParser, section: str, option: str, default_value: str | int | float | bool = ""
) -> str:
    """从 config_parser 读取 section/option 的值；缺节或缺键时把 default_value 补写回配置文件。"""
    try:
        if "录制设置" not in config_parser.sections():
            config_parser.add_section("录制设置")
        if "推送配置" not in config_parser.sections():
            config_parser.add_section("推送配置")
        if "Cookie" not in config_parser.sections():
            config_parser.add_section("Cookie")
        if "Authorization" not in config_parser.sections():
            config_parser.add_section("Authorization")
        if "账号密码" not in config_parser.sections():
            config_parser.add_section("账号密码")
        return config_parser.get(section, option)
    except configparser.NoSectionError, configparser.NoOptionError:
        with state.file_update_lock:
            if section not in config_parser.sections():
                config_parser.add_section(section)
            config_parser.set(section, option, str(default_value))
            try:
                buffer = io.StringIO()
                config_parser.write(buffer)
                if not _atomic_write_text(state.config_file, buffer.getvalue()):
                    _ = config_parser.remove_option(section, option)
            except (OSError, configparser.Error) as e:
                logger.warning(
                    i18n.tr(
                        "配置项 {section}/{option} 缺省值写回失败（已忽略）: {type_name}: {e}",
                        section=section,
                        option=option,
                        type_name=type(e).__name__,
                        e=e,
                    )
                )
                _ = config_parser.remove_option(section, option)
        return str(default_value)


def read_config_bool(
    config_parser: configparser.RawConfigParser, section: str, option: str, default: bool = False
) -> bool:
    """读取布尔型配置项（全仓统一入口）：语义 = read_config_value + parse_config_bool。"""
    raw = read_config_value(config_parser, section, option, format_config_bool(default))
    return parse_config_bool(raw, default)


def _safe_int(value: str | None, default: int) -> int:
    """把配置项 value 安全转为 int；为空或非法时打印告警并返回 default。"""
    try:
        text = str(value).strip()
        if "_" in text:
            raise ValueError("underscore in numeric config value")
        return int(text)
    except TypeError, ValueError:
        logger.warning(i18n.tr("配置项数值非法: {value}，使用默认值 {default}", value=repr(value), default=default))
        return default


def _safe_float(value: str | None, default: float) -> float:
    """把配置项 value 安全转为 float；为空或非法时打印告警并返回 default。"""
    try:
        text = str(value).strip()
        if "_" in text:
            raise ValueError("underscore in numeric config value")
        result = float(text)
        if not math.isfinite(result):
            raise ValueError("non-finite value")
        return result
    except TypeError, ValueError:
        logger.warning(i18n.tr("配置项数值非法: {value}，使用默认值 {default}", value=repr(value), default=default))
        return default


def _backup_mask_secrets() -> bool:
    """CR-07：备份内容的凭据脱敏开关。"""
    return not parse_config_bool(os.environ.get("DLR_BACKUP_KEEP_SECRETS", ""), False)


def _redact_ini_secrets(text: str) -> str:
    """备份内容的凭据脱敏：按「节/键名命中 **或** 值本身形如凭据」两道判据把敏感值替换为 ***。"""
    try:
        from src.api.config import _looks_like_secret_value, is_sensitive_item
        from src.config.reader import tolerant_config_parser
    except ImportError:
        return text
    parser = tolerant_config_parser()
    try:
        parser.read_string(text)
    except configparser.Error as e:
        logger.warning(
            i18n.tr(
                "配置备份未能脱敏（解析失败：{e}），本次副本可能包含明文凭据，请勿外传",
                e=type(e).__name__,
            )
        )
        return text
    out = io.StringIO()
    for section in parser.sections():
        _ = out.write(f"[{section}]\n")
        for key, value in parser.items(section):
            if value.strip() and (is_sensitive_item(section, key) or _looks_like_secret_value(value)):
                _ = out.write(f"{key} = ***\n")
            else:
                _ = out.write(f"{key} = {value}\n")
        _ = out.write("\n")
    return out.getvalue()


def _harden_permissions(path: str) -> None:
    """收紧文件权限为「仅属主可读写」（best-effort）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def backup_file(file_path: str, backup_dir_path: str, limit_counts: int = 6) -> bool:
    """备份配置文件到 backup_config 目录；返回是否真的落成了备份（False = 建目录/写入/复制任一环节抛错）。"""
    try:
        if not os.path.exists(backup_dir_path):
            os.makedirs(backup_dir_path)

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_file_name = os.path.basename(file_path) + "_" + timestamp
        backup_file_path = os.path.join(backup_dir_path, backup_file_name).replace("\\", "/")
        if _backup_mask_secrets():
            try:
                with open(file_path, "r", encoding=state.text_encoding, errors="replace") as src_f:
                    content = src_f.read()
                if not _atomic_write_text(backup_file_path, _redact_ini_secrets(content)):
                    raise OSError(f"atomic write failed: {backup_file_path}")
            except OSError:
                _ = shutil.copy2(file_path, backup_file_path)
        else:
            _ = shutil.copy2(file_path, backup_file_path)
        _harden_permissions(backup_file_path)

        files = os.listdir(backup_dir_path)
        _files = [f for f in files if f.startswith(os.path.basename(file_path))]
        try:
            _files.sort(key=lambda x: os.path.getmtime(os.path.join(backup_dir_path, x)))
        except OSError:
            _files.sort()

        while len(_files) > limit_counts:
            oldest_file = _files[0]
            try:
                os.remove(os.path.join(backup_dir_path, oldest_file))
            except OSError as e:
                logger.warning(i18n.tr("清理过期备份 {oldest_file} 失败（已保留）：{e}", oldest_file=oldest_file, e=e))
                break
            _files = _files[1:]

        return True

    except Exception as e:
        logger.error(i18n.tr("\r备份配置文件 {file_path} 失败：{e}", file_path=file_path, e=e))
        logger.warning(
            i18n.tr(
                "配置文件备份未成功，本轮不更新备份基线（下轮将重试）: {file_path} - {type_name}: {e}",
                file_path=file_path,
                type_name=type(e).__name__,
                e=e,
            )
        )
        return False


def backup_file_start() -> None:
    """守护线程主体：每 10 分钟比对 config.ini / URL_config.ini 的 MD5，仅在内容变化时备份。"""
    config_md5 = ""
    url_config_md5 = ""

    while True:
        try:
            if os.path.exists(state.config_file):
                new_config_md5 = utils.check_md5(state.config_file)
                if new_config_md5 != config_md5 and backup_file(state.config_file, state.backup_dir):
                    config_md5 = new_config_md5

            if os.path.exists(state.url_config_file):
                new_url_config_md5 = utils.check_md5(state.url_config_file)
                if new_url_config_md5 != url_config_md5:
                    if backup_file(state.url_config_file, state.backup_dir):
                        url_config_md5 = new_url_config_md5
        except Exception as e:
            logger.error(i18n.tr("备份配置文件失败, 错误信息: {e}", e=e))
        time.sleep(600)
