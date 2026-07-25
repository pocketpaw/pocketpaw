# domain.py — Frozen value objects for the Code Mode Project registry (CM-2a).
# Created 2026-07-16 (feat/code-mode): tenancy fields (workspace_id, user_id) are
# REQUIRED at construction with no defaults per ee/cloud Rule 3 — constructing a
# view without tenancy is a TypeError, so a leak can't be minted by omission.
# Mirrors websandbox/domain.py; a CodeProjectView is only ever built from a
# persisted, tenant-checked row.
#
# Modified 2026-07-22 (fix/starter-project-collision): added ``STARTER_PROVIDER``
# and ``is_scaffold_provider`` — the named test for "this project's ``repo`` field
# holds a TEMPLATE id, not a repo IDENTITY". ``create_project``'s idempotency
# reads it to decide whether a second create is the same project or a new one.
#
# Modified 2026-07-24 (feat/code-durable-project-store): added ``overlay`` to
# CodeProjectView so the project-keyed durability path can read the snapshot +
# overlay tiers off a single ``get_project`` (mirrors how WebSandboxView carries
# ``overlay`` for websandbox restore). Like WebSandbox, overlay is view-only — it
# is NOT surfaced on the wire ``CodeProjectResponse``; it is internal durability
# bookkeeping, not a client-facing field.
#
# Modified 2026-07-25 (feat/code-s3-authoritative): added ``overlay_complete`` —
# view-only like ``overlay`` (durability bookkeeping, never on the wire). It says
# whether ``overlay`` is a COMPLETE image of the workspace or just the delta of
# files that passed a write hook, which is the difference between "this path is
# absent because the user deleted it" and "this path is absent because nothing ever
# mirrored it". Restore reads it before doing anything destructive.
#
# Modified 2026-07-24 (feat/code-initial-prompt): added ``initial_prompt`` and
# ``initial_prompt_consumed`` to CodeProjectView. Unlike ``overlay`` these DO flow
# to the wire (``initialPrompt`` / ``initialPromptConsumed``) — the frontend reads
# the prompt on first open to auto-run one build turn and the consumed flag to
# avoid re-running it.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NewType

CodeProjectId = NewType("CodeProjectId", str)

#: The ``provider`` value marking a SCAFFOLD project — one composed from a pinned
#: starter template rather than cloned from a code host. Must stay spelled the
#: same as ``STARTER_PROVIDER`` in paw-enterprise ``core/codeproject/source.ts``;
#: the two are the same discriminator read from opposite ends of the wire.
STARTER_PROVIDER = "starter"

#: Providers whose ``repo`` field names a TEMPLATE rather than an IDENTITY.
#:
#: A frozenset rather than an ``== STARTER_PROVIDER`` check because "starter" is
#: the first scaffold source, not the only conceivable one (a template gallery,
#: an uploaded zip) — a second one should be a line here, not a second branch
#: at every call site.
_SCAFFOLD_PROVIDERS = frozenset({STARTER_PROVIDER})


def is_scaffold_provider(provider: str) -> bool:
    """Does this provider's ``repo`` value name a template instead of a project?

    The distinction drives ``create_project``'s idempotency. For an identity
    provider (``github``) the repo string IS the project — ``acme/widgets``
    means one project no matter how many times it is submitted, so a repeat
    create returns the existing row. For a scaffold provider the repo string is
    the *template* the project starts from, and the catalog holds four of them,
    so two unrelated projects sharing ``react`` is the normal case rather than a
    duplicate.
    """
    return provider in _SCAFFOLD_PROVIDERS


@dataclass(frozen=True)
class CodeProjectView:
    """Read model for one durable project row.

    Every field is required at construction — the tenancy fields
    (``workspace_id``, ``user_id``) most of all, per Rule 3. Optional runtime
    pointers (snapshot, current sandbox, last-opened) default to None so they
    land after the required tenancy fields without a second construction site.
    """

    id: CodeProjectId
    workspace_id: str
    user_id: str
    name: str
    provider: str
    repo: str
    created_at: datetime
    updated_at: datetime
    # Durable blob-storage snapshot pointer — the project's files between VMs.
    snapshot_file_id: str | None = None
    # The AUTHORITATIVE per-file store (``relpath -> FileRecord id``), keyed on the
    # project. View-only, mirroring WebSandboxView — not on the wire.
    overlay: dict[str, str] = field(default_factory=dict)
    # Whether ``overlay`` is a complete image of the workspace (see the model).
    overlay_complete: bool = False
    # The natural-language build prompt captured at create-from-description time
    # (WHAT to build), and whether the auto-run build turn has been kicked off for
    # it. Both flow to the wire, unlike ``overlay``.
    initial_prompt: str | None = None
    initial_prompt_consumed: bool = False
    # The current ephemeral sandbox (a WebSandbox row id), null when none is live.
    current_sandbox_id: str | None = None
    last_opened_at: datetime | None = None


__all__ = [
    "STARTER_PROVIDER",
    "CodeProjectId",
    "CodeProjectView",
    "is_scaffold_provider",
]
