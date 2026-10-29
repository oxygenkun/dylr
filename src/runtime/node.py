"""Node.js startup check using the shared direct downloader."""

from src.runtime.installer import ensure_runtime
from src.support.logger import script_path


def check_node() -> bool:
    return ensure_runtime("node", script_path)
