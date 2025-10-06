from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

from .config import get_settings
from .manifest import ManifestNotFoundError, get_manifest


class SubmissionNotFoundError(Exception):
    """Raised when a submission cannot be located in DynamoDB."""


settings = get_settings()
_dynamodb = boto3.resource("dynamodb", region_name=settings.region_name) if settings.table_name else None
_s3 = boto3.client("s3", region_name=settings.region_name) if settings.bucket_name else None
_slug_pattern = re.compile(r"[^A-Za-z0-9_.-]+")


def _table():
    if not _dynamodb or not settings.table_name:
        raise RuntimeError("DOC_CHECKER_TABLE is not configured")
    return _dynamodb.Table(settings.table_name)


def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ttl_epoch() -> int:
    return int(time.time() + settings.ttl_seconds)


def _safe_filename(filename: str) -> str:
    cleaned = filename.strip().replace(" ", "_")
    cleaned = _slug_pattern.sub("-", cleaned)
    cleaned = cleaned.strip("-._")
    return cleaned or f"file-{int(time.time())}"


def _convert(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _convert(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(item) for item in value]
    return value


def start_submission(opportunity_id: Optional[str] = None, submission_id: Optional[str] = None) -> Dict[str, Any]:
    table = _table()
    submission_id = submission_id or str(uuid.uuid4())
    now = _now_iso()
    item = {
        "submission_id": submission_id,
        "opportunity_id": opportunity_id,
        "files": {},
        "overall": "pending",
        "created_at": now,
        "updated_at": now,
        "ttl": _ttl_epoch(),
    }
    table.put_item(Item=item)
    return item


def get_submission(submission_id: str) -> Dict[str, Any]:
    table = _table()
    response = table.get_item(Key={"submission_id": submission_id})
    item = response.get("Item")
    if not item:
        raise SubmissionNotFoundError(submission_id)
    return _convert(item)


def ensure_submission(submission_id: Optional[str], opportunity_id: Optional[str]) -> Dict[str, Any]:
    if submission_id:
        try:
            record = get_submission(submission_id)
            if opportunity_id and not record.get("opportunity_id"):
                update_submission(submission_id, {"opportunity_id": opportunity_id})
                record["opportunity_id"] = opportunity_id
            return record
        except SubmissionNotFoundError:
            return start_submission(opportunity_id, submission_id=submission_id)
    return start_submission(opportunity_id)


def update_submission(submission_id: str, updates: Dict[str, Any]) -> None:
    table = _table()
    now = _now_iso()
    ttl = _ttl_epoch()
    set_expr = []
    attr_values = {":updated": now, ":ttl": ttl}
    attr_names = {}

    for idx, (key, value) in enumerate(updates.items(), start=1):
        placeholder = f"#k{idx}"
        value_placeholder = f":v{idx}"
        attr_names[placeholder] = key
        attr_values[value_placeholder] = value
        set_expr.append(f"{placeholder} = {value_placeholder}")

    set_expr.append("updated_at = :updated")
    set_expr.append("ttl = :ttl")

    table.update_item(
        Key={"submission_id": submission_id},
        UpdateExpression="SET " + ", ".join(set_expr),
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )


def generate_presigned_upload(
    submission_id: Optional[str],
    requirement_id: str,
    filename: str,
    content_type: str,
    opportunity_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not _s3 or not settings.bucket_name:
        raise RuntimeError("DOC_CHECKER_BUCKET is not configured")

    submission = ensure_submission(submission_id, opportunity_id)
    manifest = None
    if opportunity_id or submission.get("opportunity_id"):
        try:
            manifest = get_manifest(opportunity_id or submission.get("opportunity_id"))
        except ManifestNotFoundError:
            manifest = None

    safe_name = _safe_filename(filename)
    timestamp = int(time.time())
    key = f"submissions/{submission['submission_id']}/{requirement_id}/{timestamp}-{safe_name}"

    params = {
        "Bucket": settings.bucket_name,
        "Key": key,
        "ContentType": content_type or "application/octet-stream",
    }
    url = _s3.generate_presigned_url(
        ClientMethod="put_object",
        Params=params,
        ExpiresIn=settings.presign_expiry_seconds,
    )

    placeholder = {
        "filename": filename,
        "key": key,
        "status": "pending",
        "messages": [],
        "content_type": content_type,
        "uploaded_at": _now_iso(),
    }

    table = _table()
    table.update_item(
        Key={"submission_id": submission["submission_id"]},
        UpdateExpression="SET files.#req = :file, overall = if_not_exists(overall, :pending), ttl = :ttl, updated_at = :updated",
        ExpressionAttributeNames={"#req": requirement_id},
        ExpressionAttributeValues={
            ":file": placeholder,
            ":pending": "pending",
            ":ttl": _ttl_epoch(),
            ":updated": _now_iso(),
        },
    )

    requirement = None
    if manifest:
        for doc in manifest.get("documents", []):
            if doc.get("id") == requirement_id:
                requirement = doc
                break

    return {
        "submission_id": submission["submission_id"],
        "key": key,
        "requirement": requirement,
        "upload": {
            "url": url,
            "method": "PUT",
            "headers": {"Content-Type": content_type or "application/octet-stream"},
        },
    }


def get_status(submission_id: str) -> Dict[str, Any]:
    record = get_submission(submission_id)
    files = record.get("files") or {}
    file_list = []
    for requirement_id, data in files.items():
        file_list.append(
            {
                "requirement_id": requirement_id,
                "filename": data.get("filename"),
                "status": data.get("status", "pending"),
                "messages": data.get("messages", []),
                "key": data.get("key"),
                "content_type": data.get("content_type"),
            }
        )

    return {
        "submission_id": record.get("submission_id"),
        "opportunity_id": record.get("opportunity_id"),
        "overall": record.get("overall", "pending"),
        "files": file_list,
        "updated_at": record.get("updated_at"),
    }


def update_file_status(
    submission_id: str,
    requirement_id: str,
    status: str,
    messages: Optional[list[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    table = _table()
    now = _now_iso()
    ttl = _ttl_epoch()
    expr = "SET files.#req.#status = :status, files.#req.#messages = :messages, updated_at = :updated, ttl = :ttl"
    attr_names = {"#req": requirement_id, "#status": "status", "#messages": "messages"}
    attr_values = {":status": status, ":messages": messages or [], ":updated": now, ":ttl": ttl}

    if extra:
        for idx, (key, value) in enumerate(extra.items(), start=1):
            placeholder = f"#extra{idx}"
            value_placeholder = f":extra{idx}"
            expr += f", files.#req.{placeholder} = {value_placeholder}"
            attr_names[placeholder] = key
            attr_values[value_placeholder] = value

    table.update_item(
        Key={"submission_id": submission_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )


def update_overall_status(submission_id: str, overall: str) -> None:
    update_submission(submission_id, {"overall": overall})
