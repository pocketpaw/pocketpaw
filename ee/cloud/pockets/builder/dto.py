# Pockets builder — internal request/result DTOs.
#
# Created 2026-05-01: carries everything the builder needs from the SSE
# request context without importing ``ScopeContext`` (which would create a
# circular dependency with ``ee.cloud.chat``).

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BuildRequest(BaseModel):
    """Input to ``detect_intent``, ``build_pocket_spec``, and
    ``run_intent_from_message``.

    The chat router populates this once and passes it down through the
    builder pipeline.  ``intent_hint`` short-circuits the classifier call
    when the frontend pre-classified the intent."""

    model_config = ConfigDict(extra="ignore")

    user_message: str
    workspace_id: str
    user_id: str
    session_mongo_id: str | None = None
    pocket_id: str | None = None
    provider: str  # "anthropic" | "openai" | "ollama" | "codex_cli" | ...
    model: str | None = None  # provider-specific override; None = settings default
    # When set to "pocket_create" / "pocket_update", skip the classifier
    # call and go straight to the corresponding spec-builder call.
    intent_hint: str | None = None


class IntentDetectionResult(BaseModel):
    """Schema the classifier LLM call must return.  Used as the structured-
    output constraint for ``providers.structured_call``."""

    model_config = ConfigDict(extra="ignore")

    intent: str = Field(
        description="One of: pocket_create, pocket_update, none",
        pattern="^(pocket_create|pocket_update|none)$",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    pocket_name_hint: str | None = None
    pocket_type_hint: str | None = None


class BuildResponse(BaseModel):
    """Final result shape returned by service-layer callers that want a
    plain dict instead of iterating the async generator."""

    model_config = ConfigDict(extra="ignore")

    intent: str
    pocket_id: str | None = None
    pocket_view: dict[str, Any] | None = None
    error: str | None = None


__all__ = ["BuildRequest", "BuildResponse", "IntentDetectionResult"]
