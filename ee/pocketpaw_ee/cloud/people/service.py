"""People domain — materialize a workspace member as a Fabric ``Person``.

Created: 2026-06-08 (feat/vip-fabric-person, pp#1366).

When a workspace invite is accepted, ``accept_invite`` calls
:func:`materialize_person_from_invite` to create (or update) a standalone
Fabric ``Person`` object for the new member. The object is the identity
"spine" a later VIP-onboarding flow reads.

Write path — the journal, not the legacy SQLite store
-----------------------------------------------------

Object lifecycle goes through :class:`FabricJournalStore` (the Wave 3 /
Org Architecture path), not the legacy ``FabricStore``. Two reasons:

1. It is the blessed object path — ``pocketpaw.fabric``'s own ``__init__``
   defers object lifecycle + scope-filtered queries to the journal store.
2. Provenance and tenancy are native there. The inviting admin becomes the
   journal ``Actor``; the workspace becomes the event ``scope``; and the
   ``fabric.object.created`` / ``fabric.object.updated`` events ARE the
   emit-on-write (the decisions subsystem already folds them) — so this
   module needs no separate cloud realtime event.

Idempotency
-----------

The Fabric object id is deterministic — ``person-{workspace_id}-{user_id}``
— so re-accepting (or otherwise re-materializing) the same member UPDATES
the existing object rather than minting a duplicate. The materializer
queries the projection for that id first: present → ``update``, absent →
``create``. The journal projection also overwrites by object id on replay,
so even a stray duplicate ``created`` event would not produce two rows.

STANDALONE
----------

This models identity only. It does NOT import or depend on the per-pocket
agent-policy surface profile (``PocketSurfaceProfile`` / entity-pocket
work) — that is a different axis. Keep them separate.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from soul_protocol.spec.journal import Actor

from pocketpaw.fabric.journal_store import FabricJournalStore
from pocketpaw.fabric.models import FabricObject, FabricQuery
from pocketpaw_ee.cloud.people.domain import (
    PERSON_TYPE_ID,
    PERSON_TYPE_NAME,
    SOURCE_ADMIN_CONTEXT,
    Person,
)
from pocketpaw_ee.cloud.workspace.domain import Invite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Journal-backed store accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _default_store() -> FabricJournalStore:
    """Build the process-wide ``FabricJournalStore`` over the org journal.

    Lazily imports ``get_journal`` (the same dependency the decisions +
    pockets paths use) so importing this module doesn't drag the journal
    open in test / smoke contexts that never materialize a Person. The
    store is cached for the life of the process; ``bootstrap()`` warms the
    projection from genesis so a read-after-write sees prior rows.

    Tests override the store by passing ``store=`` to
    :func:`materialize_person_from_invite`; they should not poke this cache.
    """

    from pocketpaw.journal_dep import get_journal

    store = FabricJournalStore(get_journal())
    store.bootstrap()
    return store


def _person_object_id(workspace_id: str, user_id: str) -> str:
    """Deterministic Fabric object id for a member's Person.

    Stable across re-accepts so materialization is an upsert keyed on
    ``(workspace_id, user_id)`` rather than a fresh insert each time.
    """

    return f"person-{workspace_id}-{user_id}"


def _person_scope(workspace_id: str) -> list[str]:
    """Journal scope for a Person write.

    A single workspace-rooted scope string (matching ``ScopeKind.WORKSPACE``
    = ``"workspace"``). The journal's EventEntry invariant requires a
    non-empty scope, and tenancy is enforced by this list — a query scoped
    to one workspace never sees another's people.
    """

    return [f"workspace:{workspace_id}"]


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def _build_person(
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    email: str,
    avatar: str,
    invite: Invite,
) -> Person:
    """Assemble the :class:`Person` from member profile + invite context.

    Identity fields (``name`` / ``email`` / ``avatar``) come from the
    member's own profile. ``role`` / ``group`` and the onboarding fields
    (``focus`` / ``profile_pic``) come from the invite. When
    ``invite.context`` is ``None`` the onboarding fields fall back to empty
    strings — the Person is still created from the available identity.
    """

    # ``context`` is None when the admin supplied no onboarding hints; even
    # when present, ``focus`` / ``profile_pic`` are individually optional.
    # Either way the Person is still created — the onboarding fields just
    # fall back to empty strings.
    context = invite.context
    focus = context.focus if context and context.focus else ""
    profile_pic = context.profile_pic if context and context.profile_pic else ""

    return Person(
        id=_person_object_id(workspace_id, user_id),
        workspace_id=workspace_id,
        user_id=user_id,
        name=name,
        email=email,
        avatar=avatar,
        role=invite.role,
        group=invite.group_id,
        focus=focus,
        profile_pic=profile_pic,
        invited_by=invite.invited_by,
        source=SOURCE_ADMIN_CONTEXT,
    )


async def materialize_person_from_invite(
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    email: str,
    avatar: str,
    invite: Invite,
    store: FabricJournalStore | None = None,
) -> Person:
    """Create or update the standalone Fabric ``Person`` for a new member.

    Called from ``accept_invite`` after the membership is written. Builds a
    :class:`Person` from the member's profile (``name`` / ``email`` /
    ``avatar``) plus the invite's admin context (``role`` / ``group`` /
    ``focus`` / ``profile_pic``), records provenance (``invited_by`` +
    ``source=admin_context``), and writes it to the Fabric journal under a
    workspace-scoped event attributed to the inviting admin.

    Idempotent: the object id is deterministic, so a second call for the
    same member UPDATES the existing object — never a duplicate.

    ``store`` is injectable for tests; production callers omit it and get
    the process-wide journal-backed store.

    Returns the materialized :class:`Person` (the caller's view; the
    journal write is fire-and-confirm via the projection).
    """

    fabric = store if store is not None else _default_store()
    person = _build_person(
        workspace_id=workspace_id,
        user_id=user_id,
        name=name,
        email=email,
        avatar=avatar,
        invite=invite,
    )
    scope = _person_scope(workspace_id)

    # Provenance: the inviting admin is the journal Actor. The scope_context
    # mirrors the write scope so the recorded actor identity carries the
    # same tenancy bound as the event.
    actor = Actor(
        kind="user",
        id=f"user:{person.invited_by}",
        scope_context=list(scope),
    )

    properties = person.to_properties()

    # Idempotency probe: is this member already materialized? Query the
    # projection for the Person type and look for our deterministic id.
    existing = await fabric.query(
        FabricQuery(type_id=PERSON_TYPE_ID, limit=10_000),
        requester_scopes=scope,
    )
    already = any(obj.id == person.id for obj in existing.objects)

    if already:
        # Update merges onto existing properties — every field we write is
        # supplied here, so a changed profile / context overwrites cleanly.
        await fabric.update(person.id, properties, scope=scope, actor=actor)
    else:
        obj = FabricObject(
            id=person.id,
            type_id=PERSON_TYPE_ID,
            type_name=PERSON_TYPE_NAME,
            properties=properties,
            # Native Fabric provenance, in addition to the property-bag
            # copy: source_connector flags the origin, source_id is the
            # member's user id (the external key this row tracks).
            source_connector=SOURCE_ADMIN_CONTEXT,
            source_id=user_id,
        )
        await fabric.create(obj, scope=scope, actor=actor)

    return person


__all__ = ["materialize_person_from_invite"]
