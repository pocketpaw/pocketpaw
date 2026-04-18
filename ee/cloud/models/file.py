"""File metadata document — pre-signed URL storage."""

from __future__ import annotations

from beanie import Document, Indexed
from pydantic import Field


class FileObj(Document):
    """File metadata — actual bytes live in S3/GCS, not MongoDB."""

    owner: Indexed(str)  # type: ignore[valid-type]
    file_name: str
    bucket: str
    provider: str = Field(pattern="^(gcs|s3|local)$")
    path_in_bucket: str
    mime_type: str = ""
    size: int = 0
    public: bool = False

    class Settings:
        name = "files"
