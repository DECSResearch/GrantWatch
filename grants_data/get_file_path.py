"""Locate the most recent grants JSON file."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from logs.status_logger import logger

_DATA_DIR = Path(__file__).resolve().parent / "grants_json_data"


def get_latest_file_path() -> Optional[Path]:
    if not _DATA_DIR.exists():
        logger("warning", f"Data directory {_DATA_DIR} does not exist")
        return None

    candidates = sorted(
        _DATA_DIR.glob("grants_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    latest = candidates[0] if candidates else None

    if latest:
        logger("info", f"Latest data file located at {latest}")
    else:
        logger("warning", "No grants data files found")

    return latest
