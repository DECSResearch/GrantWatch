"""Utilities to validate grant application uploads against required forms."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
import re


def _normalise_label(value: str) -> str:
    """Normalise a form label or filename for comparison."""
    cleaned = re.sub(r"[^0-9a-z]+", "", value.lower())
    return cleaned


def _normalise_filename(filename: str) -> str:
    stem = Path(filename).stem
    return _normalise_label(stem)


@dataclass(frozen=True)
class RequiredForm:
    """Representation of a form required by a Grants.gov opportunity."""

    identifier: str
    display_name: str
    optional: bool = False
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    def match_keys(self) -> Tuple[str, ...]:
        keys = [_normalise_label(self.identifier), _normalise_label(self.display_name)]
        keys.extend(_normalise_label(alias) for alias in self.aliases)
        return tuple(key for key in keys if key)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "RequiredForm":
        identifier = str(data.get("formNumber") or data.get("id") or data.get("identifier") or "unknown")
        display_name = str(data.get("formName") or data.get("name") or identifier)
        optional = bool(data.get("optional") or data.get("isOptional", False))
        aliases: Sequence[str] = data.get("aliases") or data.get("alternateNames") or []  # type: ignore[assignment]
        return cls(identifier=identifier, display_name=display_name, optional=optional, aliases=tuple(str(alias) for alias in aliases))


@dataclass
class ValidationResult:
    matched: Dict[RequiredForm, List[str]]
    missing_required: List[RequiredForm]
    missing_optional: List[RequiredForm]
    extra_uploads: List[str]
    duplicates: Dict[str, List[str]]

    @property
    def is_valid(self) -> bool:
        return not self.missing_required and not self.duplicates

    def summary(self) -> str:
        lines: List[str] = []
        if self.is_valid:
            lines.append("All required forms are present. ?")
        else:
            lines.append("Validation issues detected. ??")

        if self.missing_required:
            lines.append("Missing required forms:")
            for form in self.missing_required:
                lines.append(f"  - {form.display_name} ({form.identifier})")

        if self.missing_optional:
            lines.append("Optional forms not provided:")
            for form in self.missing_optional:
                lines.append(f"  - {form.display_name} ({form.identifier})")

        if self.duplicates:
            lines.append("Duplicate uploads detected:")
            for key, files in self.duplicates.items():
                if len(files) > 1:
                    lines.append(f"  - {files[0]} ({len(files)} copies)")

        if self.extra_uploads:
            lines.append("Unexpected uploads:")
            for file in self.extra_uploads:
                lines.append(f"  - {file}")

        return "\n".join(lines)


def parse_required_forms(required_forms: Iterable[Mapping[str, object]]) -> List[RequiredForm]:
    return [RequiredForm.from_mapping(form) for form in required_forms]


def validate_uploads(required_forms: Sequence[RequiredForm], uploaded_files: Iterable[str]) -> ValidationResult:
    uploads: Dict[str, List[str]] = {}
    duplicates: Dict[str, List[str]] = {}

    for file in uploaded_files:
        key = _normalise_filename(file)
        uploads.setdefault(key, []).append(file)
        if len(uploads[key]) > 1:
            duplicates[key] = uploads[key]

    matched: Dict[RequiredForm, List[str]] = {}
    missing_required: List[RequiredForm] = []
    missing_optional: List[RequiredForm] = []
    consumed_keys: set[str] = set()

    for form in required_forms:
        matched_files: Optional[List[str]] = None
        for key in form.match_keys():
            files = uploads.get(key)
            if files:
                matched_files = files
                consumed_keys.add(key)
                break
        if matched_files:
            matched[form] = matched_files
        elif form.optional:
            missing_optional.append(form)
        else:
            missing_required.append(form)

    extra_uploads: List[str] = []
    for key, files in uploads.items():
        if key not in consumed_keys:
            extra_uploads.extend(files)

    return ValidationResult(
        matched=matched,
        missing_required=missing_required,
        missing_optional=missing_optional,
        extra_uploads=extra_uploads,
        duplicates=duplicates,
    )


__all__ = [
    "RequiredForm",
    "ValidationResult",
    "parse_required_forms",
    "validate_uploads",
]
