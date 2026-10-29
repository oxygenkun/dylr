"""通用文件、文本处理与凭据脱敏，不依赖录制业务。"""

import hashlib
import os
import random
import re
import shutil
import stat
import string
import sys
import tempfile
from pathlib import Path

from src.support import i18n
from src.support.logger import logger

_EMOJI_PATTERN = re.compile(
    "["
    + "\U0001f1e0-\U0001f1ff"  # flags (iOS)
    + "\U0001f300-\U0001f5ff"  # symbols & pictographs
    + "\U0001f600-\U0001f64f"  # emoticons
    + "\U0001f680-\U0001f6ff"  # transport & map symbols
    + "\U0001f700-\U0001f77f"  # alchemical symbols
    + "\U0001f780-\U0001f7ff"  # Geometric Shapes Extended
    + "\U0001f800-\U0001f8ff"  # Supplemental Arrows-C
    + "\U0001f900-\U0001f9ff"  # Supplemental Symbols and Pictographs
    + "\U0001fa00-\U0001fa6f"  # Chess Symbols
    + "\U0001fa70-\U0001faff"  # Symbols and Pictographs Extended-A
    + "\U00002702-\U000027b0"  # Dingbats
    + "]+",
    flags=re.UNICODE,
)


def check_md5(file_path: str | Path) -> str:
    with open(file_path, "rb") as fp:
        return hashlib.md5(fp.read()).hexdigest()


def _fsync_dir(dir_path: str) -> None:
    if sys.platform == "win32":
        return
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(file_path: str | Path, text: str, encoding: str = "utf-8-sig", errors: str | None = None) -> bool:
    path_str = str(file_path)
    parent = os.path.dirname(os.path.abspath(path_str)) or os.getcwd()
    try:
        orig_mode: int | None = stat.S_IMODE(os.stat(path_str).st_mode)
    except OSError:
        orig_mode = None  # 目标不存在（首次写入）时按 mkstemp 默认的 0600 落盘
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=os.path.basename(path_str) + ".", suffix=".tmp")
    except OSError as e:
        logger.warning(i18n.tr("写入配置文件失败（已保留原文件）: {err}", err=e))
        return False
    try:
        with os.fdopen(fd, "w", encoding=encoding, errors=errors, newline="") as f:
            _ = f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # 不支持 fsync 的文件系统仍允许原子替换。
        if orig_mode is not None:
            try:
                os.chmod(tmp, orig_mode)
            except OSError:
                pass
        os.replace(tmp, path_str)
    except (OSError, UnicodeError) as e:
        logger.warning(i18n.tr("写入配置文件失败（已保留原文件）: {err}", err=e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    _fsync_dir(parent)
    return True


def get_file_paths(directory: str) -> list[str]:
    file_paths: list[str] = []
    for root, _, files in os.walk(directory):
        for file in files:
            file_paths.append(os.path.join(root, file))
    return file_paths


def remove_emojis(text: str, replace_text: str = "") -> str:
    return _EMOJI_PATTERN.sub(replace_text, text)


def check_disk_capacity(file_path: str | Path, show: bool = False) -> float:
    absolute_path = os.path.abspath(file_path)
    directory = os.path.dirname(absolute_path)
    disk_usage = shutil.disk_usage(directory)
    disk_root = Path(directory).anchor
    free_space_gb = disk_usage.free / (1024**3)
    if show:
        print(
            i18n.tr(
                "{disk_root} Total: {disk_usage} GB Used: {disk_usage_2} GB Free: {free_space_gb} GB\n",
                disk_root=disk_root,
                disk_usage=f"{disk_usage.total / (1024**3):.2f}",
                disk_usage_2=f"{disk_usage.used / (1024**3):.2f}",
                free_space_gb=f"{free_space_gb:.2f}",
            )
        )
    return free_space_gb


def generate_random_string(length: int) -> str:
    characters = string.ascii_uppercase + string.digits
    return "".join(random.choices(characters, k=length))


_SECRET_KEYS: tuple[str, ...] = (
    "signature",
    "sign",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "apikey",
    "api_key",
    "secret",
    "key",
    "x-bogus",
    "a-bogus",
    "a_bogus",
    "ms_token",
    "nonce",
    "sid",
    "sid_guard",
    "session",
    "sessionid",
    "session_id",
    "auth",
    "authorization",
    "proxy-authorization",
    "ticket",
    "pwd",
    "passwd",
    "password",
    "wssecret",
    "txsecret",
    "cookie",
    "set-cookie",
    "set_cookie",
    "ttwid",
    "verifyfingerprint",
    "verify_fingerprint",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "mstoken",
    "wsauth",
    "txauth",
    "verifyfp",
    "tk",
    "csrf",
    "csrftoken",
    "xsrf",
    "xsrftoken",
    "sessdata",
    "bili_jct",
    "sessionid_ss",
    "sid_tt",
)


_SECRET_KEY_ALT = "|".join(sorted(_SECRET_KEYS, key=lambda _k: (-len(_k), _k)))


_SECRET_CAMEL_BOUNDARY = r"(?<=[a-z])(?=[A-Z])"


_SECRET_PLAIN_BOUNDARY = r"(?<![A-Za-z0-9])"


_SECRET_LOWER_COMPOUND_BOUNDARY = r"(?<=[a-z])(?=(?i:(?:token|key)))"


_SECRET_QUERY_RE = re.compile(
    r"((?:(?:"
    + _SECRET_PLAIN_BOUNDARY
    + r"|"
    + _SECRET_CAMEL_BOUNDARY
    + r"|"
    + _SECRET_LOWER_COMPOUND_BOUNDARY
    + r")(?i:(?:"
    + _SECRET_KEY_ALT
    + r"))=))[^&\s\"']+"
)


_SECRET_HEADER_RE = re.compile(
    r"((?:(?:"
    + _SECRET_PLAIN_BOUNDARY
    + r"|"
    + _SECRET_CAMEL_BOUNDARY
    + r"|"
    + _SECRET_LOWER_COMPOUND_BOUNDARY
    + r")(?i:(?:"
    + _SECRET_KEY_ALT
    + r"))\s*:\s*))[^,;\r\n\"']+"
)


_SECRET_JSON_RE = re.compile('(?i)("(?:' + _SECRET_KEY_ALT + r')"\s*:\s*")([^"]*)(")')


_PROXY_CREDENTIAL_RE = re.compile(r"(?i)(://)[^/@\s]+@")


_BARE_PROXY_CREDENTIAL_RE = re.compile(r"(?i)(?<![/\w.])[^\s/:@\"']+:[^\s/@\"']+@")


def mask_credentials(text: str) -> str:
    masked = _PROXY_CREDENTIAL_RE.sub(r"\1***@", text)
    masked = _BARE_PROXY_CREDENTIAL_RE.sub("***@", masked)
    masked = _SECRET_JSON_RE.sub(r"\1***\3", masked)
    masked = _SECRET_HEADER_RE.sub(r"\1***", masked)
    return _SECRET_QUERY_RE.sub(r"\1***", masked)
