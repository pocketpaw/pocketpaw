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

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Schema identifier the seed file must carry at its top level.
ATLAS_SCHEMA_V1 = "paw.atlas/v1"

# ``primitive`` and ``surface`` are seeded today; the rest are reserved so
# later tasks can add entries without a schema bump.
AtlasKind = Literal["primitive", "capability", "surface", "connector", "widget", "skill"]


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


class AtlasModel(BaseModel):
    """The full self-model document: schema tag + entries."""

    schema_: Literal["paw.atlas/v1"] = Field(alias="schema", default=ATLAS_SCHEMA_V1)
    entries: list[AtlasEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


__all__ = ["ATLAS_SCHEMA_V1", "AtlasEntry", "AtlasKind", "AtlasModel"]
