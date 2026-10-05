"""Research profile: what the group works on and how to filter for it."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from logs.status_logger import logger

_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "research_profile.yml"
# A git-ignored copy wins over the committed template, so a public repo never
# has to carry the group's actual research description.
_LOCAL_PATH = _DEFAULT_PATH.with_name("research_profile.local.yml")
_PLACEHOLDER = "REPLACE ME"
_DEFAULT_KEYWORDS = ["research", "education", "innovation", "technology", "infrastructure"]
_DEFAULT_HORIZONS = [7, 30, 60, 90]


@dataclass
class Profile:
    summary: str = ""
    institution: str = ""
    applicant_type: str = ""
    keywords: List[str] = field(default_factory=lambda: list(_DEFAULT_KEYWORDS))
    keyword_threshold: int = 1
    agencies_exclude: List[str] = field(default_factory=list)
    min_score_to_notify: int = 4
    deadline_horizons: List[int] = field(default_factory=lambda: list(_DEFAULT_HORIZONS))
    include_forecasted: bool = True
    source: Optional[Path] = None

    @property
    def is_configured(self) -> bool:
        """True once the summary has been written (scoring stays off until then)."""
        text = (self.summary or "").strip()
        return bool(text) and not text.upper().startswith(_PLACEHOLDER)

    def fingerprint(self) -> str:
        """Short hash of what the scorer sees; a change means every grant is rescored."""
        basis = "\n".join([self.summary.strip(), self.institution.strip(), self.applicant_type.strip()])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]

    def excludes_agency(self, agency_code: object, agency_name: object) -> bool:
        code = str(agency_code or "").upper()
        name = str(agency_name or "").lower()
        for pattern in self.agencies_exclude:
            needle = str(pattern).strip()
            if not needle:
                continue
            if code.startswith(needle.upper()) or needle.lower() in name:
                return True
        return False

    def as_prompt_text(self) -> str:
        parts = [self.summary.strip()]
        if self.institution:
            parts.append(f"Institution: {self.institution.strip()}.")
        if self.applicant_type:
            parts.append(f"Applicant type: {self.applicant_type.strip()}.")
        return " ".join(part for part in parts if part)


def _as_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def load_profile(path: str | os.PathLike[str] | None = None) -> Profile:
    """Load the YAML profile; environment variables override individual fields.

    ``GRANTS_PROFILE_FILE`` picks the file; otherwise a git-ignored
    ``research_profile.local.yml`` next to the template is used when present.
    ``GRANTS_PROFILE_SUMMARY`` and ``GRANTS_PROFILE_INSTITUTION`` override the
    text fields (handy on Vercel or GitHub Actions). ``GRANTS_KEYWORDS``,
    ``GRANTS_KEYWORD_THRESHOLD``, ``GRANTS_INCLUDE_FORECAST`` and
    ``GRANTS_MIN_SCORE`` override the matching keys so the scheduled workflow
    can tune a run without editing the file.
    """
    if path:
        location = Path(path)
    elif _env("GRANTS_PROFILE_FILE"):
        location = Path(_env("GRANTS_PROFILE_FILE"))  # type: ignore[arg-type]
    elif _LOCAL_PATH.exists():
        location = _LOCAL_PATH
    else:
        location = _DEFAULT_PATH
    raw: Dict[str, Any] = {}
    if location.exists():
        try:
            loaded = yaml.safe_load(location.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
            else:
                logger("warning", f"Research profile at {location} is not a mapping; using defaults")
        except (OSError, yaml.YAMLError) as exc:
            logger("warning", f"Could not read research profile at {location}: {exc}")
    else:
        logger("warning", f"Research profile not found at {location}; using defaults")

    keywords = _as_list(_env("GRANTS_KEYWORDS")) or _as_list(raw.get("keywords")) or list(_DEFAULT_KEYWORDS)
    horizons = sorted({h for h in (_as_int(v, 0) for v in _as_list(raw.get("deadline_horizons"))) if h > 0})

    profile = Profile(
        summary=(_env("GRANTS_PROFILE_SUMMARY") or str(raw.get("summary") or "")).strip(),
        institution=(_env("GRANTS_PROFILE_INSTITUTION") or str(raw.get("institution") or "")).strip(),
        applicant_type=str(raw.get("applicant_type") or "").strip(),
        keywords=keywords,
        keyword_threshold=max(0, _as_int(_env("GRANTS_KEYWORD_THRESHOLD") or raw.get("keyword_threshold"), 1)),
        agencies_exclude=_as_list(raw.get("agencies_exclude")),
        min_score_to_notify=min(5, max(0, _as_int(_env("GRANTS_MIN_SCORE") or raw.get("min_score_to_notify"), 4))),
        deadline_horizons=horizons or list(_DEFAULT_HORIZONS),
        include_forecasted=_as_bool(_env("GRANTS_INCLUDE_FORECAST"), _as_bool(raw.get("include_forecasted"), True)),
        source=location if location.exists() else None,
    )
    return profile
