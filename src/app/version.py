"""项目版本读取。"""

import re
from pathlib import Path


def read_version() -> str:
    """从 pyproject.toml 读取版本；缺失时回退 0.0.0。"""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject_path.exists():
        text = pyproject_path.read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*["\'](.+?)["\']', text, re.MULTILINE)
        if m:
            return m.group(1)
    return "0.0.0"


version: str = f"v{read_version()}"
