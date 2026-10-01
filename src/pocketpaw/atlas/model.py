# atlas/model.py — pydantic schema for the atlas OS self-model (AT-1).
# Created: 2026-07-02 (feat/atlas-core). Defines paw.atlas/v1: a flat list
# of capability entries that the seed data file (``atlas/data/atlas.json``)
# must validate against. The store (``atlas/store.py``) loads and searches
# these models; the ``pocketpaw_atlas`` in-process MCP server serves them
# to agents.
# Updated: 2026-07-02 (feat/atlas-surface, AT-3) — the seed now carries
# ``surface`` entries (the paw-enterprise client's user-facing routes) next
# to the primitives, and primitives with a natural home route populate
# their ``surface`` field. Docstrings updated to match; no schema change.
# Updated: 2026-07-02 (feat/atlas-compiler, AT-4) — additive only, still
# paw.atlas/v1: new ``sense`` kind (extracted from the senses vocabulary by
# the compiler) and a top-level ``generated`` provenance flag that the
# compiled artifact (``atlas/data/atlas.json``, now built by
# ``atlas/compile.py`` from ``atlas/authored/*.json`` + the connector YAMLs)
# sets to true. Hand-authored files omit it (defaults false).
# Updated: 2026-07-02 (feat/atlas-widgets, AT-6) — no schema change: the
# previously reserved ``widget`` (ripple canvas catalog) and ``skill``
# (bundled skills) kinds are now emitted by the compiler.
# Updated: 2026-07-05 (fix/atlas-relevance-round2) — additive optional ``gist``
# field on ``AtlasEntry`` (still paw.atlas/v1). A dedicated, complete
# one-liner for the always-on Paw OS primer so each primer line ends on a full
# clause instead of a mid-phrase truncation of ``summary`` that dropped
# load-bearing words (Belt's "Instinct gate", Branch's "review/merge/publish").
# Defaults to "" — the compiler passes it through and the primer builder
# prefers it, falling back to a clause-aware truncation of ``summary``.
# Updated: 2026-10-01 (feat/atlas-canonical) — atlas becomes the single source
# for what surfaces and composer verbs exist. Additive, still paw.atlas/v1:
#   * new ``verb`` kind (authored in ``authored/verbs.json``): an action the
#     user can run on an object (send to a channel, rename a file, complete a
#     task), with ``slash`` / ``applies_to`` / ``triggers`` / ``risk`` / ``undo``;
#   * surfaces gain ``slash`` / ``presentation`` / ``agent_openable``. The
#     open_surface tool's route allowlist is the set of ``agent_openable``
#     surfaces.
# All new fields default to None and the compiler drops None keys, so entries
# that don't use them serialize byte-identically to before. ``KIND_FIELDS``
# names the fields each kind must fill; the compiler enforces it at build time.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Schema identifier the seed file must carry at its top level.
ATLAS_SCHEMA_V1 = "paw.atlas/v1"

# ``primitive`` and ``surface`` are hand-authored; ``connector``, ``sense``
# (AT-4), ``widget``, and ``skill`` (AT-6) are extracted by the compiler;
# ``capability`` stays reserved so a later task can add entries without a
# schema bump.
AtlasKind = Literal[
    "primitive", "capability", "surface", "connector", "sense", "widget", "skill", "verb"
]

# How a surface opens in the no-UI shell: an inline view in the thread, or a
# separate window/route.
Presentation = Literal["inline", "window"]

# Who can start a verb: a composer slash command, an object verb (chip / menu
# on the thing it acts on), or the agent (the verb's work is done by the agent).
VerbTrigger = Literal["slash", "verb", "agent"]

# How much a verb changes: "read" changes nothing, "safe" is a benign change,
# "risky" acts as the user in a way others see or can't easily take back.
VerbRisk = Literal["read", "safe", "risky"]


# Fields a kind must fill (non-None). Enforced by ``compile_atlas`` on the
# authored sources, not on the model, so hand-built test fixtures stay light.
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "surface": ("presentation", "agent_openable"),
    "verb": ("applies_to", "triggers", "risk", "undo"),
}


class AtlasEntry(BaseModel):
    """One capability card in the OS self-model.

    The ``narrative`` is the load-bearing field: it tells an agent WHEN to
    reach for the primitive and what it pairs with, in paw meanings (not
    LLM-default meanings — "Pocket" is a workspace app container, not
    clothing).
    """

    id: str = Field(description="Stable id, e.g. 'primitive:pocket'.")
    kind: AtlasKind = Field(description="Entry kind; 'primitive' and 'surface' are seeded.")
    name: str = Field(description="Display name, e.g. 'Pocket'.")
    summary: str = Field(description="One-line description for ranked result cards.")
    gist: str = Field(
        default="",
        description=(
            "Optional complete, self-contained one-liner for the always-on Paw "
            "OS primer — authored so each primer line ends on a full clause "
            "instead of a mid-phrase truncation of ``summary``. Falls back to a "
            "clause-aware truncation of ``summary`` when empty."
        ),
    )
    narrative: str = Field(
        description="When to reach for it and what it pairs with — the agent-facing story."
    )
    how: str = Field(
        default="",
        description="The tool / verb / API that exercises the primitive, if any.",
    )
    surface: str = Field(
        default="",
        description=(
            "Optional route pointer to the frontend surface (e.g. '/belt'). "
            "Set on every kind='surface' entry and on primitives with a "
            "natural home route."
        ),
    )
    requires: list[str] = Field(
        default_factory=list,
        description="Optional entry ids this one depends on.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Search keywords — intent words a user/agent would actually say.",
    )
    # -- surface + verb fields (feat/atlas-canonical). None = not applicable;
    # the compiler drops None keys so other kinds serialize unchanged.
    slash: str | None = Field(
        default=None,
        description="Composer slash command (without '/'), or None when there is none.",
    )
    presentation: Presentation | None = Field(
        default=None, description="Surfaces only: 'inline' view or 'window'."
    )
    agent_openable: bool | None = Field(
        default=None,
        description="Surfaces only: whether the agent's open_surface tool may open it.",
    )
    applies_to: list[str] | None = Field(
        default=None,
        description="Verbs only: object types it acts on (channel, file, task, ...).",
    )
    triggers: list[VerbTrigger] | None = Field(
        default=None, description="Verbs only: who can start it."
    )
    risk: VerbRisk | None = Field(default=None, description="Verbs only: read / safe / risky.")
    undo: bool | None = Field(default=None, description="Verbs only: whether it can be undone.")


class AtlasModel(BaseModel):
    """The full self-model document: schema tag + entries.

    ``generated`` is the provenance header (AT-4): true on the compiled
    artifact written by ``pocketpaw atlas build``, absent/false on the
    hand-authored source files under ``atlas/authored/``. Additive field —
    the schema stays paw.atlas/v1.
    """

    schema_: Literal["paw.atlas/v1"] = Field(alias="schema", default=ATLAS_SCHEMA_V1)
    generated: bool = Field(
        default=False,
        description="True when this document was written by the atlas compiler.",
    )
    entries: list[AtlasEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


__all__ = [
    "ATLAS_SCHEMA_V1",
    "AtlasEntry",
    "AtlasKind",
    "KIND_FIELDS",
    "AtlasModel",
    "Presentation",
    "VerbRisk",
    "VerbTrigger",
]
