from __future__ import annotations

import io
import json
import logging
import os
import re
from typing import Dict, List, Tuple
from urllib.parse import unquote_plus

import boto3
import pdfplumber

from doc_checker import service
from doc_checker.config import get_settings
from doc_checker.manifest import get_manifest, ManifestNotFoundError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

settings = get_settings()
s3 = boto3.client("s3", region_name=settings.region_name)
textract = boto3.client("textract", region_name=settings.region_name)

_ENABLE_TEXTRACT = os.getenv("DOC_CHECKER_ENABLE_TEXTRACT", "false").lower() in {"true", "1", "yes"}


def handler(event, _context):
    objects = _extract_object_events(event)
    if not objects:
        logger.warning("Event contained no recognizable S3 object references: %s", json.dumps(event)[:1000])
    processed = 0
    for bucket, key in objects:
        try:
            _process_object(bucket, key)
            processed += 1
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to process s3://%s/%s: %s", bucket, key, exc)
    return {"processed": processed}


def _extract_object_events(event: Dict) -> List[Tuple[str, str]]:
    """Yield (bucket, key) pairs from EventBridge or legacy S3 notification events."""
    detail = event.get("detail") or {}
    if detail.get("bucket") and detail.get("object"):
        # EventBridge "Object Created" event; keys are NOT URL-encoded here
        return [(detail["bucket"]["name"], detail["object"]["key"])]

    pairs: List[Tuple[str, str]] = []
    for record in event.get("Records", []):
        s3_info = record.get("s3") or {}
        bucket = (s3_info.get("bucket") or {}).get("name")
        key = (s3_info.get("object") or {}).get("key")
        if bucket and key:
            # S3 notification keys ARE URL-encoded
            pairs.append((bucket, unquote_plus(key)))
    return pairs


def _process_object(bucket: str, key: str) -> None:
    submission_id, requirement_id, filename = _parse_key(key)
    if not submission_id:
        logger.warning("Skipping key without submission id: %s", key)
        return

    try:
        submission = service.get_submission(submission_id)
    except service.SubmissionNotFoundError:
        logger.warning("Unknown submission id %s for object %s", submission_id, key)
        return

    opportunity_id = submission.get("opportunity_id")
    manifest = None
    requirement = None
    if opportunity_id:
        try:
            manifest = get_manifest(opportunity_id)
            requirement = _lookup_requirement(manifest, requirement_id)
        except ManifestNotFoundError:
            logger.warning("Manifest missing for opportunity %s", opportunity_id)
    if requirement is None:
        requirement = {
            "id": requirement_id,
            "label": requirement_id,
            "filename_pattern": None,
            "content_types": [],
            "max_mb": settings.default_max_mb,
            "max_pages": settings.default_max_pages,
            "required_sections": [],
        }

    head = s3.head_object(Bucket=bucket, Key=key)
    size_bytes = head.get("ContentLength", 0)
    stored_file = submission.get("files", {}).get(requirement_id, {})
    content_type = head.get("ContentType") or stored_file.get("content_type") or "application/octet-stream"
    # The S3 key segment is "<timestamp>-<sanitized-name>"; validate the
    # original filename the client declared, not the mangled key.
    original_filename = stored_file.get("filename") or filename

    status = "valid"
    messages: List[str] = []
    regex = requirement.get("filename_pattern")
    allowed_types = requirement.get("content_types") or []
    max_bytes = int(requirement.get("max_mb", settings.default_max_mb)) * 1024 * 1024
    max_pages = int(requirement.get("max_pages", settings.default_max_pages))

    if regex and not re.match(regex, original_filename):
        status = "invalid"
        messages.append(f"Filename '{original_filename}' does not match required pattern")

    if allowed_types and content_type not in allowed_types:
        status = "invalid"
        messages.append(f"Content type {content_type} is not one of {allowed_types}")

    if size_bytes > max_bytes:
        status = "invalid"
        messages.append(f"File size {size_bytes} bytes exceeds limit of {max_bytes} bytes")

    page_count = None
    extracted_text = ""
    is_pdf = original_filename.lower().endswith(".pdf") or content_type == "application/pdf"

    if is_pdf and size_bytes > max_bytes:
        messages.append("Content checks skipped because the file exceeds the size limit")
    elif is_pdf:
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                page_count = len(pdf.pages)
                for page in pdf.pages:
                    extracted_text += (page.extract_text() or "") + "\n"
        except Exception as exc:  # pylint: disable=broad-except
            status = "error"
            messages.append(f"Failed to read PDF: {exc}")
            extracted_text = ""

        if page_count is not None and page_count > max_pages:
            status = "invalid"
            messages.append(f"PDF has {page_count} pages; limit is {max_pages}")

        extracted_normalised = extracted_text.lower()
        missing_sections = [section for section in requirement.get("required_sections", []) if section.lower() not in extracted_normalised]
        if missing_sections:
            status = "invalid"
            messages.append(f"Missing sections: {', '.join(missing_sections)}")

        if not extracted_text.strip() and _ENABLE_TEXTRACT:
            textract_text = _run_textract(bucket, key)
            if not textract_text.strip():
                status = "invalid"
                messages.append("Textract could not extract readable text")
            else:
                extracted_text = textract_text
    else:
        if requirement.get("required_sections"):
            messages.append("Section validation skipped for non-PDF upload")

    service.update_file_status(
        submission_id,
        requirement_id,
        status,
        messages,
        extra={
            "size_bytes": size_bytes,
            "page_count": page_count,
            "content_type": content_type,
        },
    )

    _recalculate_overall(submission_id)


def _recalculate_overall(submission_id: str) -> None:
    try:
        record = service.get_submission(submission_id)
    except service.SubmissionNotFoundError:
        return

    files = record.get("files", {})
    statuses = [data.get("status", "pending") for data in files.values()]

    required_ids: List[str] = []
    opportunity_id = record.get("opportunity_id")
    if opportunity_id:
        try:
            manifest = get_manifest(opportunity_id)
            required_ids = [
                doc.get("id")
                for doc in manifest.get("documents", [])
                if doc.get("id") and doc.get("required", True)
            ]
        except ManifestNotFoundError:
            required_ids = []

    missing_required = [req_id for req_id in required_ids if req_id not in files]

    if any(status in {"invalid", "error"} for status in statuses):
        overall = "needs_review"
    elif statuses and all(status == "valid" for status in statuses) and not missing_required:
        overall = "passed"
    else:
        overall = "pending"
    service.update_overall_status(submission_id, overall)


def _parse_key(key: str):
    parts = key.split("/")
    if len(parts) < 4:
        return None, None, os.path.basename(key)
    _, submission_id, requirement_id, filename = parts[0], parts[1], parts[2], parts[-1]
    return submission_id, requirement_id, filename


def _lookup_requirement(manifest: Dict, requirement_id: str):
    for doc in manifest.get("documents", []):
        if doc.get("id") == requirement_id:
            return doc
    return None


def _run_textract(bucket: str, key: str) -> str:
    try:
        response = textract.detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Textract failed: %s", exc)
        return ""
    text = []
    for block in response.get("Blocks", []):
        if block.get("BlockType") == "LINE" and block.get("Text"):
            text.append(block["Text"])
    return "\n".join(text)
