# __init__.py — Public surface for the extraction package.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Exposes ExtractionResult, ExtractionAdapter, ExtractionChain, build_chain.
"""Pluggable file-content extraction.

Public API:
  - `ExtractionResult` — output model every adapter returns.
  - `ExtractionAdapter` — Protocol that adapters implement.
  - `ExtractionChain` — runner with offline detection and fallback.
  - `build_chain(settings)` — factory that reads pocketpaw Settings.

Concrete adapters (`LocalExtractor`, `GeminiFlashExtractor`) are
imported on demand from `build_chain` so this package can be imported
in environments missing google-genai.
"""

from pocketpaw_ee.cloud.extraction.adapter import ExtractionAdapter, ExtractionResult
from pocketpaw_ee.cloud.extraction.chain import ExtractionChain, build_chain

__all__ = [
    "ExtractionAdapter",
    "ExtractionChain",
    "ExtractionResult",
    "build_chain",
]
