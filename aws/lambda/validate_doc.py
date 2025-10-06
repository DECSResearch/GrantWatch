from __future__ import annotations

import io
import json
import logging
import os
import re
from typing import Dict, List

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
    records = event.get("Records", [])
    for record in records:
        try:
            _process_record(record)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to process record: %s", exc)
    return {"processed": len(records)}


def _process_record(record: Dict) -> None:
    bucket = record["s3"]["bucket"]["name"]
    key = record["s3"]["object"]["key"]
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
    content_type = head.get("ContentType") or submission.get("files", {}).get(requirement_id, {}).get("content_type") or "application/octet-stream"

    status = "valid"
    messages: List[str] = []
    regex = requirement.get("filename_pattern")
    allowed_types = requirement.get("content_types") or []
    max_bytes = int(requirement.get("max_mb", settings.default_max_mb)) * 1024 * 1024
    max_pages = int(requirement.get("max_pages", settings.default_max_pages))

    if regex and not re.match(regex, filename):
        status = "invalid"
        messages.append(f"Filename '{filename}' does not match required pattern")

    if allowed_types and content_type not in allowed_types:
        status = "invalid"
        messages.append(f"Content type {content_type} is not one of {allowed_types}")

    if size_bytes > max_bytes:
        status = "invalid"
        messages.append(f"File size {size_bytes} bytes exceeds limit of {max_bytes} bytes")

    page_count = None
    extracted_text = ""
    is_pdf = filename.lower().endswith(".pdf") or content_type == "application/pdf"

    if is_pdf:
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
    if any(status in {"invalid", "error"} for status in statuses):
        overall = "needs_review"
    elif statuses and all(status == "valid" for status in statuses):
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
