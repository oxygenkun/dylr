"""Node.js startup check using the shared direct downloader."""

import os
from pathlib import Path

from src.config.booleans import parse_config_bool
from src.runtime.installer import ensure_runtime
from src.support.logger import script_path


def check_node() -> bool:
    return ensure_runtime("node", script_path)


def initialize_node_runtime() -> None:
    """把随包 Node.js 目录加入 PATH，并在未跳过运行时检查时确保其可用。"""
    node_dir = Path(script_path) / "node"
    env_path = os.environ.get("PATH", "")
    if node_dir.is_dir() and str(node_dir) not in env_path.split(os.pathsep):
        os.environ["PATH"] = str(node_dir) + os.pathsep + env_path
    if not parse_config_bool(os.environ.get("DOUYIN_SKIP_RUNTIME_CHECK"), False):
        check_node()
