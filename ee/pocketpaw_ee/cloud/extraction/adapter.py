# adapter.py — ExtractionAdapter Protocol + ExtractionResult model.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Defines the pluggable adapter contract for file content extraction.
"""Extraction adapter protocol.

The captain's directive: keep the traditional extractors (pypdf, python-docx,
pytesseract) and add cloud-backed adapters as siblings, not replacements.
Each adapter declares the MIME types it handles and whether it needs network.
The chain runner picks the first matching adapter and falls through on
failure or offline conditions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ExtractionResult(BaseModel):
    """Output shape every adapter returns.

    text: the primary searchable content. Required.
    title: detected title or first heading; None when not detected.
    captions: per-image / per-page captions. Empty list when not applicable.
    metadata: free-form bag (author, page_count, language, source path, ...).
    backend: which adapter produced this result (e.g. "local", "gemini-flash").
    """

    text: str
    title: str | None = None
    captions: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    backend: str


@runtime_checkable
class ExtractionAdapter(Protocol):
    """Duck-typed extraction adapter.

    Attributes:
        name: stable identifier ("local", "gemini-flash"). Used in settings,
            logs, and the per-MIME override map.
        supports_mimes: set of MIME strings this adapter handles. Use "*" as
            a wildcard for adapters that handle anything.
        requires_network: when True the chain skips this adapter if the host
            is offline and falls through to the offline fallback.
    """

    name: str
    supports_mimes: set[str]
    requires_network: bool

    async def extract(self, path: Path, mime: str) -> ExtractionResult: ...
