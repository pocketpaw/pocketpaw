# dto.py — Request/response DTOs for the Web Cursor Sandbox Registry.
# Created 2026-07-15 (WC-1, feat/websandbox-registry): distinct <Op>Request
# and <Entity>Response classes per ee/cloud Rule 4 — one model is never reused
# for both input and output, so a server-owned field (id, timestamps) can't
# leak into the write surface. Wire shape is camelCase to match the rest of
# the cloud surface.
#
# Changed 2026-07-15 (WC-2, feat/websandbox-vm-provision): added the
# cold-provision + file-tree DTOs — ``OpenSandboxRequest`` (the repo URL to open,
# optional branch), ``TreeEntryResponse`` (one node in the cloned repo's file
# tree), and ``SandboxTreeResponse`` (the tree wrapper carrying the bound Daytona
# id). Same Rule-4 discipline: the open surface accepts only a repo + branch and
# never a server-owned id/status.
#
# Changed 2026-07-15 (WC-S3, feat/websandbox-s3-durability): surfaced the durable
# snapshot pointer on the wire (``WebSandboxResponse.snapshotFileId``) and added
# ``SnapshotResponse`` — the ``{fileId}`` the snapshot endpoint returns after
# landing the workspace tarball in the tenant's blob storage.
#
# Changed 2026-07-15 (WC-5a, feat/websandbox-edit-agent): surfaced the
# auto-feature-branch on the wire (``WebSandboxResponse.branch``) and let the
# provisioner bind it via ``UpdateStatusRequest.branch``. This change also added
# the AI edit-agent DTOs, which were REMOVED again in CA-4 — see below.
#
# Changed 2026-07-21 (CA-4, feat/codeagent-edit): removed ``EditRequest`` /
# ``EditResponse`` / ``EditSelection`` along with ``websandbox/edit.py``. Taking a
# sandbox ``row_id`` was the whole problem: a WebContainer project runs in the
# user's tab and has no row, which is why Cmd-K shipped disabled there. Its
# replacement is ``codeagent`` — the client sends the code it already has, so the
# same path serves both runtimes.
#
# Changed 2026-07-16 (WC-8/P3b preview, feat/code-mode): added ``PreviewResponse``
# ({url, port}) — the iframe-embeddable public URL of a dev-server port running in
# the sandbox VM. Response-only (Rule 4); the requested port arrives as a query
# param, never a body, and tenancy comes from the RequestContext.
#
# Changed 2026-07-16 (WC-7/P4a git, feat/code-mode): added the git write-path DTOs —
# request bodies ``StageRequest`` (paths + unstage) and ``CommitRequest`` (message),
# and response models ``GitStatusResponse`` / ``GitFileEntry`` / ``GitCommitResponse``
# / ``GitPushResponse``. Same Rule-4 split: the request bodies carry only what the
# client supplies; tenancy comes from the RequestContext, never the body.
#
# Changed 2026-07-16 (review hardening): split the write surface. ``sandbox_id``
# and ``status`` are SERVER-OWNED — the Daytona id is the load-bearing input to
# ``authorize_sandbox``, so a client that could write it onto its own row could
# forge access to another tenant's VM. The client-facing register route now binds
# ``RegisterSandboxRequest`` (repo only); ``CreateSandboxRequest`` /
# ``UpdateStatusRequest`` are INTERNAL command objects the provisioner (and tests
# that stand in for it) use to bind server-generated runtime state, and are never
# bound by a router. The client ``PATCH`` route was removed with the same intent.
#
# Changed 2026-07-20 (RR-2, feat/code-runtime-requirements): added
# ``RuntimeRequirementsResponse`` — what a PROJECT NEEDS from a runtime
# (install / nativeToolchain / rawSockets) plus the ``reasons`` that produced
# each flag, so the runtime-routing decision is explainable rather than opaque.
# Also generalized the credential response: the broker shape was never
# BrowserPod-specific (any in-tab runtime needs a vendor key brokered the same
# way), so the canonical name is now ``RuntimeCredentialsResponse`` and
# ``BrowserPodCredentialsResponse`` is a deprecated alias kept for one release.
from __future__ import annotations

from pydantic import BaseModel, Field


class RegisterSandboxRequest(BaseModel):
    """Client-facing register body for ``POST /websandbox`` — repo ONLY.

    The write surface never accepts a ``sandbox_id`` or ``status``: those are
    server-owned. ``authorize_sandbox`` trusts the row's ``sandbox_id`` as the
    proof-of-ownership key, so letting a client set it would let an attacker who
    learned a victim's Daytona id register a row pointing at the victim's VM and
    pass the authorization oracle. The provisioner binds runtime state internally
    (see ``CreateSandboxRequest`` / ``UpdateStatusRequest`` below), never the client.
    """

    repo: str = Field(..., min_length=1, max_length=1024)


class CreateSandboxRequest(BaseModel):
    """INTERNAL registration command — provisioner / test-seed use only.

    Never bound by a router (the client-facing model is ``RegisterSandboxRequest``).
    Carries the server-owned ``sandbox_id`` / ``status`` / ``installation_id`` the
    provisioner sets as the VM boots. Creating is idempotent per
    (workspace, user, repo) — see ``service.create_sandbox``.
    """

    repo: str = Field(..., min_length=1, max_length=1024)
    sandbox_id: str | None = Field(default=None, max_length=256)
    status: str = Field(default="pending", max_length=32)
    installation_id: str | None = Field(default=None, max_length=256)


class UpdateStatusRequest(BaseModel):
    """INTERNAL lifecycle-bind command — provisioner / test-seed use only.

    Never bound by a router. Advances a sandbox's lifecycle state and binds its
    server-generated Daytona id / feature branch. The client cannot reach this:
    lifecycle is driven entirely by the provisioner (``provision.open_sandbox``)
    and the idle reaper.
    """

    status: str = Field(..., max_length=32)
    sandbox_id: str | None = Field(default=None, max_length=256)
    # The auto-created feature branch the provisioner checked out in the VM
    # (WC-5a). Bound in the same write that marks the row ``ready``.
    branch: str | None = Field(default=None, max_length=256)


class WebSandboxResponse(BaseModel):
    id: str
    workspaceId: str
    userId: str
    repo: str
    status: str
    sandboxId: str | None = None
    installationId: str | None = None
    snapshotFileId: str | None = None
    branch: str | None = None
    createdAt: str  # ISO-8601 UTC
    updatedAt: str  # ISO-8601 UTC


class WebSandboxListResponse(BaseModel):
    items: list[WebSandboxResponse]


# ---------------------------------------------------------------------------
# WC-2 — cold-provision + file tree.
# ---------------------------------------------------------------------------


class OpenSandboxRequest(BaseModel):
    """Open a sandbox against a public repo — cold-provision a VM and clone it.

    Only the repo URL (and an optional branch) crosses the wire; the Daytona
    ``sandbox_id`` and lifecycle ``status`` are server-owned and set by the
    provisioner as the VM boots (Rule 4 — the write surface never accepts them).
    """

    repo: str = Field(..., min_length=1, max_length=1024)
    branch: str | None = Field(default=None, max_length=256)


class TreeEntryResponse(BaseModel):
    """One node in the cloned repo's file tree (a single directory level)."""

    name: str
    isDir: bool
    size: int = 0


class SandboxTreeResponse(BaseModel):
    """The file tree of a ready sandbox, keyed to its bound Daytona id."""

    id: str
    sandboxId: str
    path: str
    entries: list[TreeEntryResponse]


# ---------------------------------------------------------------------------
# WC-S3 — workspace durability (snapshot / restore).
# ---------------------------------------------------------------------------


class SnapshotResponse(BaseModel):
    """The durable pointer minted by a snapshot — the blob-storage FileRecord id."""

    fileId: str


# ---------------------------------------------------------------------------
# WC-8/P3b — live dev-server preview.
# ---------------------------------------------------------------------------


class PreviewResponse(BaseModel):
    """The iframe-embeddable public URL for a dev-server port in the sandbox VM.

    Response-only (Rule 4): the requested ``port`` arrives as a query param and is
    echoed back alongside the resolved ``url``. ``url`` already carries the Daytona
    preview access token as a query param, so it embeds in an ``<iframe>`` directly.
    """

    url: str
    port: int


# ---------------------------------------------------------------------------
# BP-1b / RR-2 — in-tab runtime boot credential.
# ---------------------------------------------------------------------------


class RuntimeCredentialsResponse(BaseModel):
    """The credential a browser needs to boot an in-tab runtime.

    Response-only (Rule 4). ``available`` is false with a null ``apiKey`` in TWO
    cases that a client cannot and need not tell apart: the runtime is not
    configured on this deploy, and the runtime id is unknown. Both mean "route
    this project somewhere else", so both answer the same way (see
    ``websandbox/runtimes.py``).

    NOTE: this response necessarily carries the key to the client, because the
    runtime's ``boot()`` executes in the tab. Brokering it keeps the key out of
    the frontend bundle and makes it gateable/rotatable/auditable, but it is not
    secret from an authenticated caller — see ``websandbox/browserpod.py``.
    """

    available: bool
    apiKey: str | None = None


# DEPRECATED alias, kept for one release while callers move to the generalized
# name. The shape was never BrowserPod-specific — WebContainers needs a brokered
# browser-side key on exactly the same terms.
BrowserPodCredentialsResponse = RuntimeCredentialsResponse


# ---------------------------------------------------------------------------
# RR-2 — what a project NEEDS from a runtime, resolved before anything boots.
# ---------------------------------------------------------------------------


class RuntimeRequirementsResponse(BaseModel):
    """The capability demands of a project, for matching against a runtime.

    Response-only (Rule 4); the repo/ref arrive as query params and tenancy comes
    from the RequestContext.

    ``reasons`` is not decoration. This response drives a user-visible routing
    decision — fast in-tab runtime versus a slower real VM — and a decision that
    cannot be explained cannot be debugged. Every flag raised to ``true`` carries
    at least one reason naming the evidence that raised it, e.g.
    ``"pg in dependencies -> rawSockets"``.
    """

    # Needs to install dependencies from a registry.
    install: bool
    # Needs to execute native binaries (esbuild, rollup bindings, node-gyp…).
    nativeToolchain: bool
    # Needs real TCP — a database driver an in-tab runtime cannot emulate.
    rawSockets: bool
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# WC-7/P4a — git write path (status / stage / commit / push).
# ---------------------------------------------------------------------------


class StageRequest(BaseModel):
    """Stage (or unstage) a set of paths for the next commit.

    ``paths`` are project-relative and each is ``shlex.quote``d before it reaches
    the shell (git.py). ``unstage`` flips ``git add`` to ``git reset HEAD``.
    """

    paths: list[str] = Field(default_factory=list)
    unstage: bool = False


class CommitRequest(BaseModel):
    """Commit the staged changes with ``message`` (shlex-quoted before the shell)."""

    message: str = Field(..., min_length=1, max_length=8192)


class GitFileEntry(BaseModel):
    """One changed path from ``git status --porcelain`` — the two status columns
    plus a convenience ``staged`` flag (the index column is a real change)."""

    path: str
    index: str  # the index (staged) column: 'M','A','D','R','?'…
    worktree: str  # the worktree (unstaged) column
    staged: bool


class GitStatusResponse(BaseModel):
    """Working-tree status: current branch, upstream ahead/behind, changed files."""

    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    files: list[GitFileEntry] = Field(default_factory=list)


class GitCommitResponse(BaseModel):
    """The result of a commit — the new ``sha`` and whether anything was committed
    (``committed`` is False when there was nothing staged)."""

    sha: str
    committed: bool


class GitPushResponse(BaseModel):
    """The result of a push — ``pushed`` plus the ``branch`` and, on failure, a
    human-readable ``detail`` (a push failure is NEVER an error response)."""

    pushed: bool
    branch: str
    detail: str | None = None


class CreatePrRequest(BaseModel):
    """Open a pull request for the sandbox's feature branch (WC-7/P4b).

    Only the ``title`` and optional ``body`` cross the wire; the head branch, base
    branch, and repo are all server-resolved from the sandbox row + the caller's
    GitHub connection (never client-supplied)."""

    title: str = Field(..., min_length=1, max_length=256)
    body: str = Field(default="", max_length=65536)


class GitPrResponse(BaseModel):
    """An opened pull request — its ``url`` (html_url) and ``number``."""

    url: str
    number: int


# ---------------------------------------------------------------------------
# CS-2 — scaffold a composed project into a provisioned sandbox.
# ---------------------------------------------------------------------------


class ScaffoldIntoSandboxRequest(BaseModel):
    """Compose these recipes and bring the project up in this sandbox.

    Takes a recipe LIST rather than a prompt: the user is shown the plan and
    allowed to edit it before anything is written, so the server receives the
    decision, not the sentence it came from.
    """

    recipes: list[str] = Field(default_factory=list, max_length=16)
    projectName: str = Field(default="", max_length=64)
    #: Dev-server port. Optional — the default is Vite's own 5173.
    port: int | None = Field(default=None, ge=1, le=65535)


class ScaffoldStepResponse(BaseModel):
    """One bring-up stage and how it went.

    `output` is populated ONLY on failure, and that is the point of this shape:
    CS-2's acceptance is that a failed `npm install` shows a visible failed
    state rather than a spinner, so npm's own error text has to reach the UI.
    A successful step carries nothing — nobody reads a clean install log.
    """

    name: str
    ok: bool
    exitCode: int | None = None
    output: str = ""
    durationMs: int = 0


class ScaffoldIntoSandboxResponse(BaseModel):
    """What was composed, and how far bring-up got.

    ``running`` means the dev-server START command returned cleanly — NOT that
    the server is serving. It was backgrounded, so nothing server-side could know
    that yet; the preview pane resolving the port is what actually proves it.
    Named honestly so it is not read as a promise.
    """

    projectName: str
    order: list[str] = Field(default_factory=list)
    #: Secret NAMES the project needs before it can run. Names only, always.
    secrets: list[str] = Field(default_factory=list)
    fileCount: int = 0
    port: int
    running: bool = False
    steps: list[ScaffoldStepResponse] = Field(default_factory=list)
    #: The first stage that failed, or null. Lets the client branch without
    #: re-deriving it from the step list.
    failedStep: str | None = None


__all__ = [
    "ScaffoldStepResponse",
    "ScaffoldIntoSandboxResponse",
    "ScaffoldIntoSandboxRequest",
    "CommitRequest",
    "CreatePrRequest",
    "CreateSandboxRequest",
    "GitCommitResponse",
    "GitFileEntry",
    "GitPrResponse",
    "GitPushResponse",
    "GitStatusResponse",
    "OpenSandboxRequest",
    "PreviewResponse",
    "RegisterSandboxRequest",
    "SandboxTreeResponse",
    "SnapshotResponse",
    "StageRequest",
    "TreeEntryResponse",
    "UpdateStatusRequest",
    "WebSandboxListResponse",
    "WebSandboxResponse",
]
