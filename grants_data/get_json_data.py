"""Load grants JSON data from disk."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from logs.status_logger import logger


def process_json_data(file_path: str | Path) -> List[Dict[str, object]]:
    path = Path(file_path)
    if not path.exists():
        logger("error", f"Grants file not found at {path}")
        return []

    try:
        with path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except json.JSONDecodeError as exc:
        logger("error", f"Failed to parse JSON at {path}: {exc}")
        return []

    if not isinstance(payload, list):
        logger("error", f"Unexpected JSON structure in {path}; expected a list")
        return []

    logger("info", f"Loaded {len(payload)} grants from {path}")
    return payload
