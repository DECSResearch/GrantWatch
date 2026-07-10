"""Retention helper for generated pipeline artifacts."""
from __future__ import annotations

import os
from pathlib import Path

from logs.status_logger import logger


def keep_limit(default: int = 10) -> int:
    raw = os.getenv("GRANTS_KEEP_ARTIFACTS")
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw.strip()))
    except ValueError:
        return default


def prune_old_files(directory: Path, pattern: str, keep: int) -> int:
    """Delete the oldest files matching ``pattern``, keeping the newest ``keep``."""
    try:
        candidates = sorted(
            (path for path in directory.glob(pattern) if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        logger("warning", f"Could not scan {directory} for retention: {exc}")
        return 0

    removed = 0
    for stale in candidates[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:
            logger("warning", f"Could not remove old artifact {stale}: {exc}")

    if removed:
        logger("info", f"Removed {removed} old artifact(s) from {directory}")
    return removed
