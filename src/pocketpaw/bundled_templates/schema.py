# src/pocketpaw/bundled_templates/schema.py
# Created: 2026-05-25 (feat/rfc-03-v2-schema-chokepoint) — Pydantic v2
# model for the RFC 03 v2 Pocket Template Schema. Implements every
# top-level field, every sub-schema, the shape x default_view
# compatibility matrix, the outcomes_emitted subset rule, and the
# state.id_field-resolves rule. CEL expressions parse via the
# expressions.py validator. Fabric tier-registered + via_link registry
# enforcement is intentionally out of scope for this PR.
"""Pydantic v2 model for the RFC 03 v2 Pocket Template Schema.

This module is the **schema chokepoint** — every bundled template, every
installed template, and every CLI ``template lint`` call validates
through ``PocketTemplate`` (or a sub-model). Backwards-compatible
behaviour for v1 templates is preserved via the loader's
``_promote_v1_to_v2`` translation pass, which mutates a v1-shaped dict
into a v2-shaped dict BEFORE this model sees it.

Scope of this module:

* Pydantic v2 ``PocketTemplate`` + sub-models (``StateBinding``,
  ``ColumnDef``, ``SavedView``, ``JoinedEntity``, ``ActionDef``,
  ``AgentDef``, ``TriggerDef``, ``DataSourceDef``, ``PermissionsDef``,
  ``InstinctRulesDef``, ``InstinctRule``).
* Cross-field validators: shape x default_view, outcomes_emitted
  subset, state.id_field resolves.
* CEL expression parsing (via ``CelExpression`` from
  :mod:`pocketpaw.bundled_templates.expressions`).

Out of scope (separate PRs):

* Fabric ``tier:registered`` / ``via_link`` registry enforcement.
* Runtime composition (RFC 03 runtime concern, not the schema).
* CLI ``template lint / migrate / publish``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pocketpaw.bundled_templates.expressions import CelExpression

# ---------------------------------------------------------------------------
# Enumerations (kept as Literal aliases so Pydantic generates clean
# error messages and they stay greppable across the codebase)
# ---------------------------------------------------------------------------

ShapeT = Literal[
    "data-grid",
    "kanban",
    "calendar",
    "map",
    "timeline",
    "gantt",
    "treemap",
    "network",
    "tree",
    "chart",
    "custom",
]

PatternT = Literal[
    "app",
    "dashboard",
    "browser",
    "feed",
    "composer",
    "viewer",
    "wizard",
]

DefaultViewT = Literal["list", "grid", "kanban", "calendar", "map"]

ActionKindT = Literal["single-row", "bulk", "global"]

InstinctPolicyT = Literal["auto", "require_approval", "notify_only"]

InstinctRuleActionT = Literal["require_approval", "notify", "block"]

TriggerTypeT = Literal[
    "cron",
    "webhook",
    "signal",
    "calendar",
    "manual",
    "source_change",
    "temporal",
]

PermissionsScopeT = Literal["workspace", "org", "user"]

KbScopeT = Literal["pocket", "workspace", "global"]

DataSourceMethodT = Literal["GET"]

AgentBackendT = Literal[
    "claude_sdk",
    "openai",
    "codex",
    "gemini",
    "opencode",
    "goose",
    "deep_agents",
    "auto",
]

# ---------------------------------------------------------------------------
# shape x default_view compatibility matrix (RFC 03 v2, "Schema reference"
# section, "shape x default_view compatibility matrix"). None means
# the shape declares NO default_view at all.
# ---------------------------------------------------------------------------

_SHAPE_DEFAULT_VIEW_MATRIX: dict[str, set[str] | None] = {
    "data-grid": {"list", "grid", "kanban"},
    "kanban": {"kanban"},
    "calendar": {"calendar", "list"},
    "map": {"map", "list"},
    "tree": {"list", "grid"},
    "timeline": {"list"},
    "chart": None,
    "network": None,
    "gantt": None,
    "treemap": None,
    "custom": "any",  # type: ignore[dict-item] — sentinel; custom accepts anything
}


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------


class JoinedEntity(BaseModel):
    """One secondary entity reachable from the primary via FabricLink."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        description="Namespace used in column dot-paths (e.g. 'tenant').",
    )
    entity_type: str = Field(
        ...,
        description="Fabric ObjectType of the joined entity.",
    )
    via_link: str = Field(
        ...,
        description="Registered FabricLink name between primary and join.",
    )


class ColumnDef(BaseModel):
    """One column declaration on a primary entity."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., description="Flat name or 'join.property' dot-path.")
    label: str | None = None
    widget: str = Field(..., description="Ripple Layer 1 display widget name.")
    options: dict[str, Any] | None = None
    sort: Literal["asc", "desc"] | None = None
    filter: CelExpression | None = None


class SavedView(BaseModel):
    """A preset filter + grouping surfaced as a view chip."""

    model_config = ConfigDict(extra="forbid")

    name: str
    filter: CelExpression | None = None
    group_by: str | None = None
    sort: str | None = None
    default: bool = False


class StateBinding(BaseModel):
    """Entity binding block — the primary entity + its columns + views."""

    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(..., description="Primary Fabric ObjectType.")
    id_field: str = Field(
        default="id",
        description="Row identifier column. Defaults to implicit 'id'.",
    )
    joined_entities: list[JoinedEntity] = Field(default_factory=list)
    columns: list[ColumnDef] = Field(default_factory=list)
    default_view: DefaultViewT | None = None
    saved_views: list[SavedView] = Field(default_factory=list)

    @model_validator(mode="after")
    def _id_field_resolves(self) -> StateBinding:
        """``id_field`` must be 'id' (implicit) OR match a declared
        column's ``field`` (flat name only; dot-paths are not row
        identifiers)."""
        if self.id_field == "id":
            return self
        flat_fields = {c.field for c in self.columns if "." not in c.field}
        if self.id_field not in flat_fields:
            raise ValueError(
                f"state.id_field={self.id_field!r} does not resolve to a "
                f"declared column field; declared flat fields: "
                f"{sorted(flat_fields)}"
            )
        return self


class ConfirmDef(BaseModel):
    """Object form of an action's ``confirm`` gate."""

    model_config = ConfigDict(extra="forbid")

    message: str | None = None
    type_to_confirm: str | None = None
    destructive: bool = False


class ActionDef(BaseModel):
    """One thing users or agents can DO from the pocket."""

    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    kind: ActionKindT
    instinct_policy: InstinctPolicyT
    connectors_required: list[str] = Field(default_factory=list)
    agent_required: str | None = None
    outcomes_emitted: list[str] = Field(default_factory=list)
    # confirm: True / False / object. The validator below normalises
    # ``confirm: true`` into ``{destructive: True}`` per RFC v2.
    confirm: bool | ConfirmDef | None = None
    description: str | None = None

    @field_validator("confirm", mode="before")
    @classmethod
    def _normalise_bool_confirm(cls, v: Any) -> Any:
        # ``confirm: true`` -> ``{destructive: True}`` per v2 backward
        # compat. ``confirm: false`` -> None (no gate).
        if v is True:
            return {"destructive": True}
        if v is False:
            return None
        return v


class AgentDef(BaseModel):
    """One named agent role the pocket can spawn."""

    model_config = ConfigDict(extra="forbid")

    name: str
    backend: AgentBackendT = "auto"
    system_prompt: str | None = None
    tools: list[str] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)
    soul_snippet: str | None = None


class TriggerDef(BaseModel):
    """One activation surface."""

    model_config = ConfigDict(extra="forbid")

    type: TriggerTypeT
    schedule: str | None = None
    source: str | None = None
    when: CelExpression | None = None
    filter: CelExpression | None = None
    action: str | None = None

    @model_validator(mode="after")
    def _conditionals(self) -> TriggerDef:
        if self.type == "cron" and not self.schedule:
            raise ValueError("triggers[].schedule is required when type=cron")
        if self.type in ("webhook", "signal", "calendar", "source_change"):
            if not self.source:
                raise ValueError(f"triggers[].source is required when type={self.type!r}")
        if self.type == "temporal" and not self.when:
            raise ValueError("triggers[].when is required when type=temporal")
        return self


class DataSourceDef(BaseModel):
    """One read-only source that hydrates state at runtime (RFC 04)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    method: DataSourceMethodT = "GET"
    path: str = Field(
        ...,
        description="Relative path; never absolute (SSRF-safe by RFC 04).",
    )
    bind: str = Field(..., description="state path that receives the result.")
    refresh: list[str] = Field(default_factory=lambda: ["pocket_open", "manual"])
    transform: str | None = None

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, v: str) -> str:
        # SSRF safety per RFC 04: relative paths only.
        if v.startswith(("http://", "https://", "//", "ftp://")):
            raise ValueError("data_sources[].path must be relative, not absolute")
        return v


class PermissionsDef(BaseModel):
    """RBAC scope for the pocket."""

    model_config = ConfigDict(extra="forbid")

    scope: PermissionsScopeT = "workspace"
    roles_allowed: list[str] = Field(default_factory=lambda: ["admin", "member"])
    actions_role_map: dict[str, list[str]] = Field(default_factory=dict)


class InstinctRule(BaseModel):
    """A workspace-scoped Instinct rule."""

    model_config = ConfigDict(extra="forbid")

    when: CelExpression
    action: InstinctRuleActionT


class InstinctRulesDef(BaseModel):
    """Workspace-scoped rule set plus an escalation target."""

    model_config = ConfigDict(extra="forbid")

    escalation: str | None = None
    rules: list[InstinctRule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level model
# ---------------------------------------------------------------------------


class PocketTemplate(BaseModel):
    """Top-level RFC 03 v2 Pocket Template Schema model.

    Validates a v2-shaped dict. v1 dicts must be promoted via
    ``loader._promote_v1_to_v2`` BEFORE being passed in.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = Field(..., description="Schema version (v2 only).")
    name: str = Field(
        ...,
        max_length=64,
        description="kebab-case slug. Forms the Registry URL.",
    )
    version: str = Field(..., description="Semver MAJOR.MINOR.PATCH.")
    pattern: PatternT
    vertical: str = Field(..., description="Free-form lower-case slug.")
    display_name: str | None = None
    description: str
    shape: ShapeT
    state: StateBinding
    actions: list[ActionDef] = Field(default_factory=list)
    connectors: list[str] = Field(default_factory=list)
    agents: list[AgentDef] = Field(default_factory=list)
    triggers: list[TriggerDef] = Field(default_factory=list)
    outcomes: list[str] = Field(default_factory=list)
    data_sources: list[DataSourceDef] = Field(default_factory=list)
    kb_scope: KbScopeT = "pocket"
    skill_refs: list[str] = Field(default_factory=list)
    instinct_rules: InstinctRulesDef | None = None
    permissions: PermissionsDef | None = None
    screenshots: list[str] = Field(default_factory=list)
    icon: str | None = None
    color: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_kebab(cls, v: str) -> str:
        # Lower-case kebab-case with optional -v<n> suffix per the RFC.
        # Reject path separators, whitespace, uppercase.
        if not v:
            raise ValueError("name must not be empty")
        if any(ch.isspace() for ch in v):
            raise ValueError("name must not contain whitespace")
        if v != v.lower():
            raise ValueError("name must be lower-case")
        if "/" in v or "\\" in v:
            raise ValueError("name must not contain path separators")
        return v

    @field_validator("vertical")
    @classmethod
    def _vertical_is_lower_slug(cls, v: str) -> str:
        if not v or v != v.lower() or any(ch.isspace() for ch in v):
            raise ValueError("vertical must be a lower-case slug")
        return v

    @model_validator(mode="after")
    def _shape_default_view_matrix(self) -> PocketTemplate:
        """Enforce the RFC v2 shape x default_view compatibility matrix."""
        allowed = _SHAPE_DEFAULT_VIEW_MATRIX.get(self.shape)
        # ``custom`` accepts any default_view.
        if allowed == "any":
            return self
        if allowed is None:
            # Shape declares NO default_view — reject any value.
            if self.state.default_view is not None:
                raise ValueError(
                    f"shape={self.shape!r} declares no default_view; got "
                    f"{self.state.default_view!r}"
                )
            return self
        if self.state.default_view is None:
            # default_view is optional — omitting is always OK.
            return self
        if self.state.default_view not in allowed:
            raise ValueError(
                f"shape={self.shape!r} does not allow default_view="
                f"{self.state.default_view!r}; allowed: {sorted(allowed)}"
            )
        return self

    @model_validator(mode="after")
    def _columns_required_unless_custom(self) -> PocketTemplate:
        """``state.columns`` must have at least one entry unless
        ``shape == "custom"``. Custom-shape templates render via a
        bespoke widget (e.g. the Decision Graph's SvelteFlow surface)
        and do not project rows into columns, so the columns
        declaration is metadata-only and may be empty."""
        if self.shape == "custom":
            return self
        if not self.state.columns:
            raise ValueError(
                f"state.columns must declare at least one column for "
                f"shape={self.shape!r}; only shape='custom' may declare "
                f"an empty columns list"
            )
        return self

    @model_validator(mode="after")
    def _outcomes_emitted_subset(self) -> PocketTemplate:
        """Every ``actions[].outcomes_emitted`` entry must be declared
        in the top-level ``outcomes[]`` catalog."""
        catalog = set(self.outcomes)
        for action in self.actions:
            missing = [o for o in action.outcomes_emitted if o not in catalog]
            if missing:
                raise ValueError(
                    f"actions[{action.name!r}].outcomes_emitted contains "
                    f"undeclared outcome(s) {missing}; declare them in the "
                    f"top-level outcomes[] catalog"
                )
        return self


__all__ = [
    "ActionDef",
    "AgentDef",
    "ColumnDef",
    "ConfirmDef",
    "DataSourceDef",
    "InstinctRule",
    "InstinctRulesDef",
    "JoinedEntity",
    "PermissionsDef",
    "PocketTemplate",
    "SavedView",
    "StateBinding",
    "TriggerDef",
]
