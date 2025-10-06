from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List


@dataclass
class Settings:
    bucket_name: str
    table_name: str
    region_name: str
    presign_expiry_seconds: int = 900
    ttl_days: int = 2
    allowed_origins: List[str] = field(default_factory=lambda: ["*"])
    manifest_path: Path = field(default=Path("config/doc_manifests"))
    default_max_mb: int = 25
    default_max_pages: int = 50
    textract_feature_type: str | None = None

    @property
    def ttl_seconds(self) -> int:
        return self.ttl_days * 86400


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    bucket = os.getenv("DOC_CHECKER_BUCKET", "")
    table = os.getenv("DOC_CHECKER_TABLE", "")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    presign = int(os.getenv("DOC_CHECKER_PRESIGN_SECONDS", "900"))
    ttl_days = int(os.getenv("DOC_CHECKER_TTL_DAYS", "2"))
    allowed_raw = os.getenv("DOC_CHECKER_ALLOWED_ORIGINS", "*")
    allowed = [item.strip() for item in allowed_raw.split(",") if item.strip()]
    manifest_env = os.getenv("DOC_CHECKER_MANIFEST_PATH")
    manifest_path = Path(manifest_env) if manifest_env else Path("config/doc_manifests")
    max_mb = int(os.getenv("DOC_CHECKER_DEFAULT_MAX_MB", "25"))
    max_pages = int(os.getenv("DOC_CHECKER_DEFAULT_MAX_PAGES", "50"))
    textract_feature = os.getenv("DOC_CHECKER_TEXTRACT_FEATURE", "") or None

    return Settings(
        bucket_name=bucket,
        table_name=table,
        region_name=region,
        presign_expiry_seconds=presign,
        ttl_days=ttl_days,
        allowed_origins=allowed or ["*"],
        manifest_path=manifest_path,
        default_max_mb=max_mb,
        default_max_pages=max_pages,
        textract_feature_type=textract_feature,
    )
