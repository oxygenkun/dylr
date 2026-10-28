"""Node.js startup check using the shared direct downloader."""

from src.logger import script_path
from src.runtime_install import ensure_runtime


def check_node() -> bool:
    return ensure_runtime("node", script_path)
