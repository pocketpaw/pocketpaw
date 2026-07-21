# dto.py — Request/response DTOs for the scaffold surface (CS-1, rewritten CS-1b).
#
# Created 2026-07-21. REWRITTEN 2026-07-22: recipes became starters. A project is
# now one framework template from a pinned npm package rather than a base plus
# composed features.
#
# The shape still encodes the decision that outlives the pivot: a PLAN emits
# capability REQUIREMENTS, never a runtime choice. There is no `runtime` field
# here and there must not be one — the client's registry matches `requires`
# against what each adapter declares. That matters more after this rewrite than
# before it: none of these starters needs `workerd`, so an in-tab runtime is now
# a genuine candidate where the old Cloudflare template ruled it out.
from __future__ import annotations

from pydantic import BaseModel, Field

MAX_PROMPT_CHARS = 2000


class ScaffoldPlanRequest(BaseModel):
    """What the user typed on the landing page."""

    prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT_CHARS)


class StarterChoice(BaseModel):
    """The starter a plan selected, and why.

    `matched` separates a real match from a fallback. A UI should present "React,
    because you said 'react'" differently from "React, because you named no
    framework" — the second is a guess, and the user should be invited to change
    it rather than told what is happening.
    """

    id: str
    label: str
    summary: str
    why: str
    matched: bool
    #: The npm package and version the template comes from, surfaced so the UI
    #: can say exactly what it is about to install rather than "a template".
    source: str


class ScaffoldRequirements(BaseModel):
    """The capability demands of the planned project. Mirrors
    `websandbox.dto.RuntimeRequirementsResponse` so the client's existing
    capability matcher consumes it unchanged."""

    install: bool
    nativeToolchain: bool
    rawSockets: bool
    reasons: list[str] = Field(default_factory=list)


class ScaffoldPlanResponse(BaseModel):
    """What we intend to build, before building it.

    Cheap and side-effect free: no download, no VM, nothing written. The client
    shows this for confirmation and posts the (possibly changed) starter id back.
    """

    starter: StarterChoice
    projectName: str
    devPort: int
    requires: ScaffoldRequirements


class ScaffoldComposeRequest(BaseModel):
    """Fetch this starter. Takes an id rather than a plan, because the user is
    allowed to change the framework at the confirmation step — the server
    receives the decision, not the sentence it came from."""

    starter: str = Field(..., min_length=1, max_length=64)
    projectName: str = Field(default="", max_length=64)


class ScaffoldComposeResponse(BaseModel):
    """A starter as a source map.

    `files` is `{path: contents}` for text and `assets` is `{path: base64}` for
    the handful of binaries a template carries (a favicon, usually). Both use
    POSIX-relative paths — the shape both runtimes materialize from (tar-upload
    for Daytona, `fs.mount` in a tab). The server writes no directory, which is
    what lets one response serve both.

    Binaries are carried rather than dropped on purpose: a missing favicon is a
    mystery to whoever hits it, and a base64 blob is just a file.
    """

    starter: str
    projectName: str
    devPort: int
    files: dict[str, str] = Field(default_factory=dict)
    assets: dict[str, str] = Field(default_factory=dict)
    fileCount: int = 0


__all__ = [
    "MAX_PROMPT_CHARS",
    "ScaffoldComposeRequest",
    "ScaffoldComposeResponse",
    "ScaffoldPlanRequest",
    "ScaffoldPlanResponse",
    "ScaffoldRequirements",
    "StarterChoice",
]
