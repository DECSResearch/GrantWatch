from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from doc_checker import service
from doc_checker.manifest import ManifestNotFoundError, get_manifest, list_manifests


router = APIRouter()


class StartSubmissionPayload(BaseModel):
    opportunity_id: Optional[str] = Field(default=None, description="Grants.gov opportunity identifier")


class StartSubmissionResponse(BaseModel):
    submission_id: str
    opportunity_id: Optional[str]


class UploadUrlPayload(BaseModel):
    filename: str
    contentType: str = Field(alias="contentType")
    submission_id: Optional[str] = Field(default=None, alias="submission_id")
    opportunity_id: Optional[str] = Field(default=None, alias="opportunity_id")
    requirement_id: Optional[str] = Field(default=None, alias="requirement_id")

    class Config:
        populate_by_name = True


class UploadDescriptor(BaseModel):
    url: str
    method: str
    headers: dict


class UploadUrlResponse(BaseModel):
    submission_id: str
    key: str
    requirement_id: Optional[str]
    upload: UploadDescriptor


class FileStatus(BaseModel):
    requirement_id: str
    filename: Optional[str]
    status: str
    messages: List[str]
    key: Optional[str]
    content_type: Optional[str]


class StatusResponse(BaseModel):
    submission_id: str
    opportunity_id: Optional[str]
    overall: str
    files: List[FileStatus]
    updated_at: Optional[str]


@router.post("/start-submission", response_model=StartSubmissionResponse)
def start_submission(payload: StartSubmissionPayload | None = None) -> StartSubmissionResponse:
    payload = payload or StartSubmissionPayload()
    record = service.start_submission(payload.opportunity_id)
    return StartSubmissionResponse(submission_id=record["submission_id"], opportunity_id=record.get("opportunity_id"))


@router.post("/upload-url", response_model=UploadUrlResponse)
def create_upload_url(payload: UploadUrlPayload) -> UploadUrlResponse:
    if not payload.requirement_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="requirement_id is required to track checklist status")
    try:
        descriptor = service.generate_presigned_upload(
            submission_id=payload.submission_id,
            requirement_id=payload.requirement_id,
            filename=payload.filename,
            content_type=payload.contentType,
            opportunity_id=payload.opportunity_id,
        )
    except ManifestNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Manifest not found for opportunity: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return UploadUrlResponse(
        submission_id=descriptor["submission_id"],
        key=descriptor["key"],
        requirement_id=payload.requirement_id,
        upload=UploadDescriptor(**descriptor["upload"]),
    )


@router.get("/status/{submission_id}", response_model=StatusResponse)
def submission_status(submission_id: str) -> StatusResponse:
    try:
        status_payload = service.get_status(submission_id)
    except service.SubmissionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown submission_id: {submission_id}") from exc
    files = [FileStatus(**file_entry) for file_entry in status_payload.get("files", [])]
    return StatusResponse(
        submission_id=status_payload["submission_id"],
        opportunity_id=status_payload.get("opportunity_id"),
        overall=status_payload.get("overall", "pending"),
        files=files,
        updated_at=status_payload.get("updated_at"),
    )


@router.get("/manifest")
def fetch_manifest(opportunity_id: str = Query(..., description="Grants.gov opportunity identifier")) -> dict:
    try:
        manifest = get_manifest(opportunity_id)
    except ManifestNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Manifest not found for opportunity {opportunity_id}") from exc
    return manifest


@router.get("/manifest/index")
def manifest_index() -> dict:
    return {"opportunities": list_manifests()}
