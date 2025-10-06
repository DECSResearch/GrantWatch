from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from .config import get_settings


class ManifestNotFoundError(Exception):
    """Raised when an opportunity manifest cannot be located."""


def _iter_manifest_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if not root.exists():
        return
    for path in sorted(root.glob("**/*.yaml")):
        if path.is_file():
            yield path


@lru_cache(maxsize=1)
def _load_manifests() -> Dict[str, Dict[str, Any]]:
    settings = get_settings()
    manifests: Dict[str, Dict[str, Any]] = {}

    for manifest_path in _iter_manifest_files(settings.manifest_path):
        data = yaml.safe_load(manifest_path.read_text())
        if not data:
            continue
        if "opportunities" in data:
            for entry in data["opportunities"]:
                _store_manifest(entry, manifests, manifest_path)
        else:
            _store_manifest(data, manifests, manifest_path)

    return manifests


def _store_manifest(data: Dict[str, Any], manifests: Dict[str, Dict[str, Any]], path: Path) -> None:
    settings = get_settings()
    opportunity_id = data.get("opportunity_id") or data.get("id")
    if not opportunity_id:
        raise ValueError(f"Manifest file {path} missing 'opportunity_id' field")

    documents = data.get("documents") or []
    normalised_docs = []
    for index, doc in enumerate(documents):
        doc_id = doc.get("id") or doc.get("name") or f"doc-{index+1}"
        normalised_docs.append(
            {
                "id": doc_id,
                "label": doc.get("label") or doc.get("name") or doc_id,
                "filename_pattern": doc.get("filename_pattern"),
                "required": bool(doc.get("required", True)),
                "content_types": doc.get("content_types") or [doc.get("content_type") or "application/pdf"],
                "max_mb": int(doc.get("max_mb") or settings.default_max_mb),
                "max_pages": int(doc.get("max_pages") or settings.default_max_pages),
                "required_sections": doc.get("required_sections") or [],
                "notes": doc.get("notes") or "",
            }
        )

    prepared = {
        "opportunity_id": opportunity_id,
        "title": data.get("title") or data.get("name") or opportunity_id,
        "documents": normalised_docs,
        "metadata": data.get("metadata") or {},
    }
    manifests[opportunity_id] = prepared


def get_manifest(opportunity_id: str) -> Dict[str, Any]:
    manifests = _load_manifests()
    if opportunity_id not in manifests:
        raise ManifestNotFoundError(opportunity_id)
    return manifests[opportunity_id]


def list_manifests() -> Dict[str, Any]:
    manifests = _load_manifests()
    return {key: {"title": value.get("title")} for key, value in manifests.items()}


def export_manifest_json(opportunity_id: str) -> str:
    manifest = get_manifest(opportunity_id)
    return json.dumps(manifest, separators=(",", ":"))
