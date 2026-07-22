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
from __future__ import annotations

from dataclasses import dataclass
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
    # The current ephemeral sandbox (a WebSandbox row id), null when none is live.
    current_sandbox_id: str | None = None
    last_opened_at: datetime | None = None


__all__ = [
    "STARTER_PROVIDER",
    "CodeProjectId",
    "CodeProjectView",
    "is_scaffold_provider",
]
