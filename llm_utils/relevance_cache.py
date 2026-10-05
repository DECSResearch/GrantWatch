"""Local cache of relevance scores so a run without a database still avoids rescoring."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from logs.status_logger import logger

_CACHE_PATH = Path(__file__).resolve().parents[1] / "grants_data" / "cache" / "relevance_cache.json"


def _path(path: Optional[Path]) -> Path:
    return Path(path) if path else _CACHE_PATH


def load_cached(fingerprint: str, path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return cached entries for ``fingerprint``; an older profile's cache is ignored."""
    location = _path(path)
    if not location.exists():
        return {}
    try:
        payload = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger("warning", f"Ignoring unreadable relevance cache at {location}: {exc}")
        return {}
    if not isinstance(payload, dict) or payload.get("profile") != fingerprint:
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_cached(fingerprint: str, entries: Dict[str, Dict[str, Any]], path: Optional[Path] = None) -> None:
    """Merge ``entries`` into the cache, replacing it when the profile changed."""
    if not entries:
        return
    location = _path(path)
    merged = load_cached(fingerprint, location)
    merged.update(entries)
    try:
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(json.dumps({"profile": fingerprint, "entries": merged}, indent=1), encoding="utf-8")
    except OSError as exc:
        logger("warning", f"Could not write relevance cache at {location}: {exc}")
