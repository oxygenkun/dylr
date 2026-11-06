"""Shared download metadata and local resource paths."""

import json
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1]

JS_SCRIPT_PATH = _SRC_ROOT / "javascript"

RESOURCES = json.loads((_SRC_ROOT / "external_resources.json").read_text(encoding="utf-8"))
