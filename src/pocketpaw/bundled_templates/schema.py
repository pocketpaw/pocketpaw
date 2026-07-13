# src/pocketpaw/bundled_templates/schema.py
# Created: 2026-05-25 (feat/rfc-03-v2-schema-chokepoint) — Pydantic v2
# model for the RFC 03 v2 Pocket Template Schema. Implements every
# top-level field, every sub-schema, the shape x default_view
# compatibility matrix, the outcomes_emitted subset rule, and the
# state.id_field-resolves rule. CEL expressions parse via the
# expressions.py validator. Fabric tier-registered + via_link registry
# enforcement is intentionally out of scope for this PR.
# Modified: 2026-06-04 (feat/sites-landing-template-fastpath) — added
# "landing" to PatternT so the marketing landing-page fast-path template
# (a Paw Site composed by conversion role) validates. The landing-page
# template uses shape:"custom", which is already exempt from the
# columns-required and default_view-matrix rules — no shape change needed.
# Modified: 2026-06-08 (feat/sense-template-needs, Sense tier chunk 6a) —
# added `needs: list[str]` to PocketTemplate: the Sense ids a vertical
# template requires (e.g. ["paw.payments.v1"]). Each id is validated at
# template-load via senses.validate_sense_id (a malformed / unknown paw.*
# id raises SenseValidationError), mirroring the connector `senses:` field.
# `needs` is tenant-capability METADATA, not ripple spec — it is read at
# pocket-create to surface a prompt-to-connect when a provider is missing.
# Modified: 2026-06-08 (feat/sense-source, Sense tier chunk 6b) — DataSourceDef
# gains a `type: "http"|"sense"` field (default "http", backward compatible).
# `path` is now optional (required only for http via the type validator);
# sense sources add `sense_id`, `action`, `params`. A model validator enforces
# path-required-for-http and sense_id+action-required-for-sense, and validates
# sense_id via senses.validate_sense_id. The resolved Sense value binds into
# state like any HTTP source, so Ripple needs no changes.
# Modified: 2026-06-19 (feat/typed-ripplespec-phase1) — added the RFC-03-v2
# RUNTIME layer-split models ``TemplateLayer``, ``InstanceLayer`` and
# ``RippleSpec`` (distinct from the design-time ``PocketTemplate`` above).
# ``RippleSpec`` is a TYPE boundary over the flat rippleSpec dict MongoDB
# already stores — ``to_flat_dict`` is BSON-byte-equivalent (no migration).
# The split lets the cloud ``service.update`` + ``reconcile`` paths overwrite
# template-owned regions (ui/actions/sources/shape) WITHOUT clobbering the
# instance-owned regions (state/selections), the 2026-06-13 production bug.
# Phase 1 wires these models INTERNALLY in service.update + reconcile only;
# Beanie ``Pocket.rippleSpec`` and ``domain.Pocket.ripple_spec`` stay ``dict``
# and every reader still receives a flat dict (executor dual-path readers are
# deferred to a #1472-gated Phase 2). All compile_template passthrough keys are
# explicit typed fields so no ``.get("agents")`` reader silently breaks.
# Modified: 2026-07-11 (feat/guardrail-c2-composer, instinct-guardrail-ux
# Criterion 2) — added ``LlmStep`` + ``LlmStepPipeline``: the typed spec for
# the fixed 3-step LLM pipeline composer (extract → classify → recommend).
# A validator enforces the fixed order, 1-3 steps, each kind at most once,
# and that every ``input_from`` reference resolves to "input" or an EARLIER
# step. The executor lives in ``bundled_templates.step_composer``; the agent
# invokes it via ``tools.builtin.step_pipeline_tool``.
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

import logging
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pocketpaw.bundled_templates.expressions import CelExpression

logger = logging.getLogger(__name__)

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
    "landing",
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


_LLM_STEP_ORDER: tuple[str, ...] = ("extract", "classify", "recommend")


class LlmStep(BaseModel):
    """One step of the fixed 3-step LLM pipeline (extract → classify → recommend).

    ``instruction`` is the human-authored prompt for the step. ``input_from``
    names where the step's input comes from: ``"input"`` (the pipeline's initial
    input) or an EARLIER step's kind; ``None`` = the previous step's output
    (or the initial input for the first step). ``fields`` (extract only) names
    the fields to pull; ``labels`` (classify only) is the closed label set.
    """

    model_config = ConfigDict(extra="forbid")

    step: Literal["extract", "classify", "recommend"]
    instruction: str
    input_from: str | None = None
    fields: list[str] | None = None
    labels: list[str] | None = None


class LlmStepPipeline(BaseModel):
    """The fixed-order LLM pipeline a non-technical author composes.

    Deterministic Python owns structure and sequencing (the ``start_flow``
    discipline); the model is invoked once per step by the executor
    (``bundled_templates.step_composer``). The validator enforces: 1-3 steps,
    each kind at most once, the canonical order preserved, and every
    ``input_from`` resolving to ``"input"`` or an earlier step's kind.
    """

    model_config = ConfigDict(extra="forbid")

    steps: list[LlmStep]

    @model_validator(mode="after")
    def _fixed_order(self) -> LlmStepPipeline:
        if not 1 <= len(self.steps) <= 3:
            raise ValueError("pipeline.steps must contain 1-3 steps")
        kinds = [s.step for s in self.steps]
        if len(set(kinds)) != len(kinds):
            raise ValueError("pipeline.steps must not repeat a step kind")
        order = [_LLM_STEP_ORDER.index(k) for k in kinds]
        if order != sorted(order):
            raise ValueError("pipeline.steps must follow extract -> classify -> recommend order")
        seen: set[str] = {"input"}
        for s in self.steps:
            if s.input_from is not None and s.input_from not in seen:
                raise ValueError(
                    f"steps[{kinds.index(s.step)}].input_from={s.input_from!r} must be"
                    " 'input' or an earlier step's kind"
                )
            seen.add(s.step)
        return self


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
    """One read-only source that hydrates state at runtime (RFC 04).

    Two source ``type``s:

    * ``http`` (default, backward-compatible) — a relative-path HTTP GET.
      ``path`` is required.
    * ``sense`` — resolves a provider-agnostic Sense (Sense tier chunk 6b).
      ``sense_id`` + ``action`` are required; the resolved value lands in
      ``bind`` like any other source, so Ripple needs no changes. ``params``
      are STATIC in v1 (no ``{state.x}`` evaluation yet).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["http", "sense"] = "http"
    method: DataSourceMethodT = "GET"
    path: str | None = Field(
        default=None,
        description="Relative path (http sources); never absolute (SSRF-safe by RFC 04).",
    )
    bind: str = Field(..., description="state path that receives the result.")
    refresh: list[str] = Field(default_factory=lambda: ["pocket_open", "manual"])
    transform: str | None = None
    # Sense-source fields (type == "sense").
    sense_id: str | None = None
    action: str | None = None
    params: dict[str, Any] | None = None

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, v: str | None) -> str | None:
        # SSRF safety per RFC 04: relative paths only. Only enforced when
        # present — a sense source has no path.
        if v is not None and v.startswith(("http://", "https://", "//", "ftp://")):
            raise ValueError("data_sources[].path must be relative, not absolute")
        return v

    @model_validator(mode="after")
    def _type_conditionals(self) -> DataSourceDef:
        if self.type == "http":
            if not self.path:
                raise ValueError("data_sources[].path is required when type='http'")
        elif self.type == "sense":
            if not self.sense_id or not self.action:
                raise ValueError(
                    "data_sources[].sense_id and .action are required when type='sense'"
                )
            # Validate the sense id at template-load, consistent with the
            # connector ``senses:`` and template ``needs:`` validation. Imported
            # inside the validator to avoid a top-level import cycle with the
            # senses catalog. A malformed/unknown paw.* id raises.
            from pocketpaw.senses import validate_sense_id

            validate_sense_id(self.sense_id)
        return self


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
    needs: list[str] = Field(
        default_factory=list,
        description="Sense ids this template requires, e.g. ['paw.email.v1'].",
    )
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

    @field_validator("needs")
    @classmethod
    def _needs_are_valid_senses(cls, v: list[str]) -> list[str]:
        # Validate each declared sense id at template-load — a malformed or
        # unknown paw.* id raises SenseValidationError, consistent with the
        # connector ``senses:`` validation. Imported inside the validator to
        # avoid a top-level import cycle with the senses catalog.
        from pocketpaw.senses import validate_sense_id

        for sense_id in v:
            validate_sense_id(sense_id)
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


# ---------------------------------------------------------------------------
# RFC 03 v2 RUNTIME layer split — TemplateLayer / InstanceLayer / RippleSpec
# ---------------------------------------------------------------------------
#
# These are the RUNTIME models (distinct from the design-time ``PocketTemplate``
# above). ``compile_template`` turns a ``PocketTemplate`` into a flat rippleSpec
# dict; ``RippleSpec`` is the typed view OVER that flat dict. The split is a
# TYPE boundary, not a storage boundary: ``to_flat_dict`` produces the exact
# flat dict MongoDB already stores, so there is NO migration and revert is a
# one-line change at every call site.
#
# Ownership partition (the load-bearing contract — see reconcile.py):
#   * TEMPLATE-OWNED (reconcile overwrites from the source template):
#       ui, actions, sources, shape  → ``TemplateLayer``
#   * INSTANCE-OWNED (reconcile NEVER touches; agents read/write via state_ops):
#       state, selections            → ``InstanceLayer``
#   * Everything else (agents, triggers, kb_scope, schema_version, …) is a
#     compile_template PASSTHROUGH field — explicit on ``RippleSpec`` so a
#     ``.get("agents")`` reader never silently breaks (adversarial must-fix #2).


class TemplateLayer(BaseModel):
    """Template-owned rippleSpec regions.

    Reconcile owns these — it overwrites them from the freshly-loaded source
    template. Instance edits to these regions survive only until the user runs
    reconcile. NEVER write instance/runtime data into this layer.

    ``actions`` is typed ``dict | list`` because ``compile_template`` emits a
    list from ``PocketTemplate.actions`` while the runtime ``rippleSpec.actions``
    block (post-normalizer write-binding lift) is a dict; both shapes are valid
    on a stored doc.
    """

    model_config = ConfigDict(extra="forbid")

    ui: dict[str, Any] | None = None
    actions: dict[str, Any] | list[Any] | None = None
    sources: dict[str, Any] | None = None
    shape: str | None = None


class InstanceLayer(BaseModel):
    """Instance-owned rippleSpec regions.

    Reconcile NEVER touches these. Agents read/write ``state`` via state_ops.
    The clobber-fix preserves this layer when a partial ``update`` omits it.
    """

    model_config = ConfigDict(extra="forbid")

    state: dict[str, Any] = Field(default_factory=dict)
    selections: dict[str, Any] = Field(default_factory=dict)


class RippleSpec(BaseModel):
    """Typed, layer-split rippleSpec — the RFC-03-v2 runtime model.

    The layer split is a TYPE boundary, not a storage boundary. ``to_flat_dict``
    is byte-equivalent to the flat dict MongoDB already stores, so no document
    migration is required and any phase is independently revertable.

    Design notes embedded in the contract:

    * **Every ``compile_template`` passthrough key is an explicit field**
      (adversarial must-fix #2). No key silently disappears into
      ``__pydantic_extra__`` where a ``.get("agents")`` reader would miss it.
      Truly-unknown future keys still land in ``__pydantic_extra__`` (extra is
      allowed) and survive a round-trip — additive-safe.
    * **``to_flat_dict`` uses ``exclude_unset=True``** (must-fix #1) so a key
      absent from the original dict is never re-emitted as a null, while a key
      explicitly set to ``None`` (a deliberate clear) is preserved.
    * **``from_flat_dict`` is the sole promotion entry point** and NEVER raises
      — a corrupted spec must not break pocket load (returns ``None``).
    * **``with_template_layer`` / ``with_instance_layer`` return NEW objects.**
      No mutation — the ownership boundary is explicit at the call site.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Template layer (explicit — IDE autocomplete + grep + reconcile derives
    # _TEMPLATE_OWNED_REGIONS from TemplateLayer.model_fields, not from here).
    ui: dict[str, Any] | None = None
    actions: dict[str, Any] | list[Any] | None = None
    sources: dict[str, Any] | None = None
    shape: str | None = None

    # Instance layer.
    state: dict[str, Any] = Field(default_factory=dict)
    selections: dict[str, Any] = Field(default_factory=dict)

    # compile_template passthrough fields (explicit to prevent .get() breakage).
    schema_version: str | None = None
    name: str | None = None
    version: str | None = None
    agents: list[Any] | None = None
    triggers: list[Any] | None = None
    outcomes: list[str] | None = None
    kb_scope: str | None = None
    skill_refs: list[str] | None = None
    permissions: dict[str, Any] | None = None
    instinct_rules: dict[str, Any] | None = None

    # Any remaining keys land in __pydantic_extra__ — additive safe.

    @property
    def template_layer(self) -> TemplateLayer:
        """The template-owned regions as a typed ``TemplateLayer``.

        Only fields the spec actually SET are carried onto the layer, so a
        downstream ``model_dump(exclude_unset=True)`` does not resurrect a key
        the spec omitted (keeps the layer-merge faithful to the partial input).
        """
        return TemplateLayer.model_validate(
            {k: getattr(self, k) for k in TemplateLayer.model_fields if k in self.model_fields_set}
        )

    @property
    def instance_layer(self) -> InstanceLayer:
        """The instance-owned regions as a typed ``InstanceLayer``.

        Mirrors :attr:`template_layer`: only fields the spec SET are carried,
        so an absent ``selections`` is not re-introduced as ``{}`` on merge.
        """
        return InstanceLayer.model_validate(
            {k: getattr(self, k) for k in InstanceLayer.model_fields if k in self.model_fields_set}
        )

    @classmethod
    def from_flat_dict(cls, d: dict[str, Any] | None) -> RippleSpec | None:
        """Promote a legacy flat dict to ``RippleSpec``.

        Returns ``None`` on ``None``/non-dict input or any validation failure —
        NEVER raises, because a corrupted stored spec must not break pocket
        load. An already-constructed ``RippleSpec`` is returned unchanged
        (idempotent promotion).
        """
        if d is None:
            return None
        if isinstance(d, RippleSpec):
            return d
        if not isinstance(d, dict):
            return None
        try:
            return cls.model_validate(d)
        except Exception:  # noqa: BLE001 — promotion must never break load.
            logger.warning("RippleSpec.from_flat_dict: validation failed, returning None")
            return None

    def to_flat_dict(self) -> dict[str, Any]:
        """Serialize to a BSON-compatible flat dict.

        Byte-equivalent to the current MongoDB documents — no migration needed.
        ``exclude_unset=True`` avoids injecting null keys absent from the
        original document while preserving keys explicitly set to ``None``
        (tracked via ``model_fields_set``). See adversarial must-fix #1.
        """
        return self.model_dump(exclude_unset=True, mode="python")

    def with_template_layer(self, layer: TemplateLayer) -> RippleSpec:
        """Return a NEW ``RippleSpec`` with template-owned fields from ``layer``.

        Instance-owned fields (state, selections) and passthrough/extra keys
        are preserved verbatim. Only the fields ``layer`` actually SET are
        overlaid, so an empty ``TemplateLayer()`` is a no-op on the template
        regions (it never clears an existing ui).
        """
        flat = self.to_flat_dict()
        flat.update(layer.model_dump(exclude_unset=True))
        return RippleSpec.model_validate(flat)

    def with_instance_layer(self, layer: InstanceLayer) -> RippleSpec:
        """Return a NEW ``RippleSpec`` with instance-owned fields from ``layer``.

        Template-owned fields and passthrough/extra keys are preserved verbatim.
        Only the fields ``layer`` actually SET are overlaid.
        """
        flat = self.to_flat_dict()
        flat.update(layer.model_dump(exclude_unset=True))
        return RippleSpec.model_validate(flat)


__all__ = [
    "ActionDef",
    "AgentDef",
    "ColumnDef",
    "ConfirmDef",
    "DataSourceDef",
    "InstanceLayer",
    "InstinctRule",
    "InstinctRulesDef",
    "JoinedEntity",
    "PermissionsDef",
    "PocketTemplate",
    "RippleSpec",
    "SavedView",
    "StateBinding",
    "TemplateLayer",
    "TriggerDef",
]
