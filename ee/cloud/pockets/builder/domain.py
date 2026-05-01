# Pockets builder — domain value objects.
#
# Created 2026-05-01: native pocket-creation builder. Defines the typed
# value objects the spec-builder LLM call returns and the SSE event objects
# the service yields.  ``PocketSpec`` and ``PocketUpdatePatch`` are Pydantic
# models so ``providers.structured_call`` can validate provider responses
# against ``model_json_schema()``.  ``BuilderEvent`` / ``BuilderResult`` /
# ``IntentKind`` stay as lightweight types for in-process plumbing.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentKind(StrEnum):
    """Outcomes of the intent classifier call."""

    CREATE = "pocket_create"
    UPDATE = "pocket_update"
    NONE = "none"  # not a pocket intent — fall through to the normal agent run


class WidgetSpec(BaseModel):
    """A flat widget the spec builder may emit alongside (but not with) a
    ``ripple_spec``.  Maps loosely to ``ee.cloud.pockets.dto.AddWidgetRequest``
    but stays Pydantic-light so structured-output JSON schemas stay small."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: str = "metric"
    icon: str = ""
    color: str = ""
    span: str = "col-span-1"
    data_source_type: str = "static"
    config: dict[str, Any] = Field(default_factory=dict)
    props: dict[str, Any] = Field(default_factory=dict)
    data: Any = None
    assigned_agent: str | None = None


class PocketSpec(BaseModel):
    """Validated pocket spec produced by the builder.

    Maps 1:1 to ``ee.cloud.pockets.service.agent_create``'s kwargs.  The
    builder's spec-builder call must return JSON conforming to this schema;
    the service then converts it to ``agent_create`` arguments.

    Captain rule: ``widgets`` and ``ripple_spec`` are mutually exclusive —
    silent data corruption is worse than a loud validation error.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    type: str = "custom"  # UISpec category / pocket type
    icon: str = ""
    color: str = ""
    visibility: str = "workspace"
    ripple_spec: dict[str, Any] | None = None
    widgets: list[WidgetSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_widgets_and_ripple_both_set(self) -> PocketSpec:
        if self.widgets and self.ripple_spec:
            raise ValueError(
                "PocketSpec.widgets and PocketSpec.ripple_spec are mutually "
                "exclusive — pick one render path"
            )
        return self


class PocketUpdatePatch(BaseModel):
    """Partial patch spec produced when intent is ``pocket_update``.

    Only top-level fields — Phase 1.5 will add deep ``ripple_spec`` patching
    once the update path injects the current pocket spec into the prompt."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None


@dataclass(frozen=True)
class BuilderResult:
    """Terminal value summarising a ``run_intent_from_message`` flow."""

    intent: IntentKind
    pocket_id: str | None = None
    pocket_view: dict[str, Any] | None = None
    spec: PocketSpec | None = None
    error: str | None = None


@dataclass(frozen=True)
class BuilderEvent:
    """Typed value the SSE handler yields as an SSE frame.

    ``name`` maps to the SSE ``event:`` field. ``data`` maps to ``data:``.
    Defined event names:

      intent.detected — classifier finished; ``data: {intent, confidence}``
      spec.building   — LLM call for spec in flight; ``data: {}``
      pocket.created  — Mongo write succeeded; ``data: {pocket_id, pocket}``
      pocket.updated  — Mongo write succeeded; ``data: {pocket_id}``
      chunk           — confirmation text after a successful create
      error           — ``data: {code, message}``
    """

    name: str
    data: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "BuilderEvent",
    "BuilderResult",
    "IntentKind",
    "PocketSpec",
    "PocketUpdatePatch",
    "WidgetSpec",
]
