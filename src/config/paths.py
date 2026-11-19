"""应用根目录与运行期路径。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src.config.reader import TEXT_ENCODING


def app_root() -> str:
    """应用根目录：源码运行时是项目根目录，冻结运行时是 exe 同级目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(sys.executable))
    return str(Path(__file__).resolve().parents[2])


script_path: str = app_root()
config_file: str = f"{script_path}/config/config.toml"
legacy_config_file: str = f"{script_path}/config/config.ini"
url_config_file: str = f"{script_path}/config/URL_config.ini"
backup_dir: str = f"{script_path}/backup_config"
text_encoding: str = TEXT_ENCODING
default_path: str = f"{script_path}/downloads"
