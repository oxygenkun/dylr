"""Shared download metadata; edit external_resources.json to update resources."""

import json
from pathlib import Path

RESOURCES = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
