# dto.py — Request/response DTOs for the scaffold surface (CS-1).
#
# Created 2026-07-21 (feat/codescaffold). Distinct <Op>Request / <Op>Response per
# ee/cloud Rule 4, camelCase on the wire like the rest of the cloud surface.
#
# The shape encodes the design's third decision: a PLAN emits capability
# REQUIREMENTS, never a runtime choice. There is no `runtime` field here and
# there must not be one — the client's registry matches `requires` against what
# each adapter declares, so the day an in-tab runtime can run workerd, this
# response is already correct without a change.
from __future__ import annotations

from pydantic import BaseModel, Field

# Bounds on a prompt. Generous enough for a paragraph, bounded enough that a
# runaway client cannot post a novel to a keyword matcher.
MAX_PROMPT_CHARS = 2000
MAX_RECIPES = 16


class ScaffoldPlanRequest(BaseModel):
    """What the user typed on the landing page."""

    prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT_CHARS)


class RecipeChoice(BaseModel):
    """One recipe the plan selected, and why.

    `why` is not decoration. This drives a confirmation step the user is expected
    to adjust before anything is written into their project, and "I'll set up
    auth" is a worse prompt for that decision than "I'll set up auth, because you
    said 'sign-in'".
    """

    id: str
    capability: str
    summary: str
    why: str


class ScaffoldRequirements(BaseModel):
    """The capability demands of the planned project.

    Mirrors `websandbox.dto.RuntimeRequirementsResponse` field for field so the
    client's existing capability matcher consumes it unchanged.
    """

    install: bool
    nativeToolchain: bool
    rawSockets: bool
    reasons: list[str] = Field(default_factory=list)


class ScaffoldPlanResponse(BaseModel):
    """What we intend to build, before building it.

    Cheap and side-effect free: no VM, no engine subprocess, nothing written. The
    client shows this for confirmation and posts the (possibly edited) recipe
    list back to `/compose`.
    """

    starter: str
    projectName: str
    recipes: list[RecipeChoice] = Field(default_factory=list)
    #: Secret NAMES the composed project will need. Names only — no value ever
    #: enters a plan, a source map, or a snapshot.
    secrets: list[str] = Field(default_factory=list)
    requires: ScaffoldRequirements


class ScaffoldComposeRequest(BaseModel):
    """Compose these recipes. Deliberately NOT "compose that plan": the user is
    allowed to edit the recipe list at the confirmation step, so the server takes
    the list rather than a plan id it would have to store."""

    recipes: list[str] = Field(default_factory=list, max_length=MAX_RECIPES)
    projectName: str = Field(default="", max_length=64)


class ScaffoldComposeResponse(BaseModel):
    """A composed project as a source map.

    `files` is `{path: contents}` with POSIX-relative paths — the shape both
    runtimes materialize from (tar-upload for Daytona, `fs.mount` in a tab). The
    server writes no directory, which is what lets one response serve both.

    `order` is the applied recipe order, and `secrets` the names the project will
    need before it can run — surfaced here so CE-2's required-vars checklist has
    a source that is not a grep over the generated code.
    """

    starter: str
    projectName: str
    order: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    fileCount: int = 0


__all__ = [
    "MAX_PROMPT_CHARS",
    "MAX_RECIPES",
    "RecipeChoice",
    "ScaffoldComposeRequest",
    "ScaffoldComposeResponse",
    "ScaffoldPlanRequest",
    "ScaffoldPlanResponse",
    "ScaffoldRequirements",
]
