# dto.py — Request/response DTOs for the Code Mode Project registry (CM-2a).
# Created 2026-07-16 (feat/code-mode): distinct <Op>Request and <Entity>Response
# classes per ee/cloud Rule 4. The write surface accepts ONLY what a client may
# set — repo (+ optional name/provider). Server-owned runtime state
# (snapshot_file_id, current_sandbox_id, timestamps) is NEVER accepted on the
# wire, the same discipline that closed the WebSandbox cross-tenant hole. Wire
# shape is camelCase to match the rest of the cloud surface.
#
# The ``open`` endpoint does not have its own request/response here — it takes the
# project id from the path and returns a ``WebSandboxResponse`` (the ready
# runtime sandbox to connect to), reusing the websandbox contract. ``rename`` takes
# a ``RenameProjectRequest`` and returns the updated ``CodeProjectResponse``;
# ``delete`` takes only the path id and returns 204.
#
# Modified 2026-07-24 (feat/code-initial-prompt): ``CreateProjectRequest`` now
# accepts an optional ``initial_prompt`` (the natural-language description of WHAT
# to build a from-scratch ``/code`` project), and ``CodeProjectResponse`` exposes
# ``initialPrompt`` + ``initialPromptConsumed`` so the frontend can auto-run one
# build turn on first open and know when it's already been kicked off. Added
# ``ConsumePromptRequest`` for the mark-consumed / re-arm PATCH route.
#
# Modified 2026-07-25 (B1, feat/code-project-file-sync): added the browser-runtime
# file-sync trio — ``PutProjectFileRequest`` (one client-written file),
# ``ProjectFileWriteResponse`` (the acknowledged path + durable blob pointer) and
# ``ProjectFilesResponse`` (the whole overlay as the restore payload). ``content``
# is a string because the in-tab filesystem carries source text over JSON; the
# byte ceiling lives in ``websandbox/durability.py`` next to the storage, not here.
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    """Client-facing create body — a repo (+ optional name/provider) ONLY.

    Idempotent per (workspace, user, provider, repo): a second create for the
    same repo returns the existing project. Server-owned fields (the snapshot
    pointer, the current sandbox, timestamps) are never accepted here.
    """

    repo: str = Field(..., min_length=1, max_length=1024)
    name: str | None = Field(default=None, max_length=200)
    provider: str = Field(default="github", max_length=32)
    # The natural-language description of WHAT to build, when this project is
    # created from a prompt rather than an existing repo. Persisted verbatim; the
    # frontend reads it back on first open to auto-run one build turn.
    initial_prompt: str | None = Field(default=None, max_length=8000)


class RenameProjectRequest(BaseModel):
    """Client-facing rename body — a new display name ONLY.

    The name is trimmed + length-bounded; nothing else about the project (repo,
    provider, runtime binding) is mutable from the wire.
    """

    name: str = Field(..., min_length=1, max_length=200)


class ConsumePromptRequest(BaseModel):
    """Mark the project's initial build prompt consumed — or re-arm it.

    Sent when a build turn STARTS (``consumed=True``, the default) so a reopen
    doesn't re-run the same build; a retry-build path re-arms the prompt
    (``consumed=False``). Setting the state it's already in is a clean no-op.
    """

    consumed: bool = True


class PutProjectFileRequest(BaseModel):
    """One file the in-browser runtime just wrote — its path and its text.

    ``path`` is project-relative; the service normalizes it and rejects anything
    that escapes the project (absolute paths, ``..`` traversal). ``content`` is
    the whole file, last-write-wins per path — this is a write-through of an
    editor save, not a patch. An empty string is a legitimate empty file.
    """

    path: str = Field(..., min_length=1, max_length=1024)
    content: str = ""


class ProjectFileWriteResponse(BaseModel):
    """Acknowledgement of one persisted file.

    ``path`` echoes back the NORMALIZED key the overlay actually stored (which
    can differ from what the client sent — a leading slash is stripped), so the
    client can key its own dirty-tracking on the same string the later delete
    and read-back use.
    """

    ok: bool = True
    path: str
    fileId: str


class PutProjectFilesRequest(BaseModel):
    """The in-tab runtime's whole project: ``{relative path: file content}``.

    A REPLACEMENT, not a patch — a path absent from this map is absent from the
    project, which is what lets a delete stick. The browser is the only thing
    that can enumerate a filesystem living in a browser tab, so it walks its own
    FS and posts the result. Excluded trees (``node_modules``, ``.git``, build
    output) are filtered client-side before they get here, the same way the VM
    sync filters them.
    """

    files: dict[str, str] = Field(default_factory=dict)


class PutProjectFilesResponse(BaseModel):
    """How many files the store now holds, after a whole-project replacement."""

    ok: bool = True
    stored: int


class ProjectFilesResponse(BaseModel):
    """The project's whole persisted overlay: ``{relative path: file content}``.

    The restore payload for the in-tab runtime. Once the store is COMPLETE (see
    ``overlayComplete``) this is the whole project rather than a delta over a
    baseline — which is what lets the client replay it and then delete anything
    the map does not mention. An empty map means a project nothing has stored
    yet, not a project with no files.
    """

    files: dict[str, str]


class CodeProjectResponse(BaseModel):
    id: str
    workspaceId: str
    userId: str
    name: str
    provider: str
    repo: str
    initialPrompt: str | None = None
    initialPromptConsumed: bool = False
    # Whether the per-file store holds the WHOLE project rather than a delta over
    # a baseline. On the wire because the in-tab runtime needs it to decide
    # whether it may prune: replaying a COMPLETE store means a file the store
    # doesn't list was deleted, while replaying a partial one means no such thing.
    overlayComplete: bool = False
    snapshotFileId: str | None = None
    currentSandboxId: str | None = None
    lastOpenedAt: str | None = None  # ISO-8601 UTC, or null
    createdAt: str  # ISO-8601 UTC
    updatedAt: str  # ISO-8601 UTC


class CodeProjectListResponse(BaseModel):
    items: list[CodeProjectResponse]


__all__ = [
    "CodeProjectListResponse",
    "CodeProjectResponse",
    "ConsumePromptRequest",
    "CreateProjectRequest",
    "ProjectFileWriteResponse",
    "ProjectFilesResponse",
    "PutProjectFileRequest",
    "RenameProjectRequest",
]
