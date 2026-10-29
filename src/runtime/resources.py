"""Shared download metadata; edit external_resources.json to update resources."""

import json
from pathlib import Path

RESOURCES = json.loads((Path(__file__).resolve().parents[1] / "external_resources.json").read_text(encoding="utf-8"))
