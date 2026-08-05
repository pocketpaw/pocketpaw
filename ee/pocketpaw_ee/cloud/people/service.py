"""People domain — materialize and read a workspace member's Fabric ``Person``.

Created: 2026-06-08 (feat/vip-fabric-person, pp#1366).
Changes: 2026-06-08 (feat/vip-agent-block, pp#1367) — added the read side,
:func:`get_person`, that queries the journal projection for a member's
deterministic Person id (workspace-scoped) and maps the projected object's
property bag back to a typed :class:`Person`, or ``None`` when the member
has no Person yet. The agent-orientation flow reads it to render an
"about this member" block; the read mirrors the materializer's query +
scope shape.
Changes: 2026-08-05 (feat/coupling-person-freshness, T-2) — the Person is
no longer a one-time invite-accept snapshot. Added:
:func:`materialize_person_from_membership` (upsert from live membership
data — used for the workspace owner at create and every non-invite path),
:func:`refresh_person_role` (re-materialize on a ``workspace.member_role``
event), :func:`refresh_person_profile` (re-materialize name/avatar in
every workspace on a ``profile.updated`` event), and a LAZY BACKFILL in
:func:`get_person` — a miss for a user who IS a live member materializes
the Person on first read (chosen over a startup backfill sweep: no house
pattern for data backfills exists, the read path is the only consumer, and
lazy converges without a migration). The shared write path is
:func:`_upsert_person`; every entry point funnels through it, keyed on the
same deterministic id.

When a workspace invite is accepted, ``accept_invite`` calls
:func:`materialize_person_from_invite` to create (or update) a standalone
Fabric ``Person`` object for the new member. The object is the identity
"spine" a later VIP-onboarding flow reads. :func:`get_person` is that read.
Role changes and profile edits re-run the same idempotent upsert via the
``people/listeners.py`` bus subscribers, so agents never orient on a stale
role, name, or avatar (a stale-role Person has leaked wrong cards before).

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
from dataclasses import replace
from functools import lru_cache

from soul_protocol.spec.journal import Actor

from pocketpaw.fabric.journal_store import FabricJournalStore
from pocketpaw.fabric.models import FabricObject, FabricQuery
from pocketpaw_ee.cloud.people.domain import (
    PERSON_TYPE_ID,
    PERSON_TYPE_NAME,
    SOURCE_ADMIN_CONTEXT,
    SOURCE_MEMBERSHIP,
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
    # Provenance: the inviting admin is the journal Actor.
    await _upsert_person(person, actor_user_id=person.invited_by, store=fabric)
    return person


async def _upsert_person(
    person: Person,
    *,
    actor_user_id: str,
    store: FabricJournalStore,
) -> None:
    """Write ``person`` to the Fabric journal — create or update by id.

    The shared write path every materialization entry point funnels through
    (invite accept, workspace-create owner, role-change refresh, profile
    refresh, lazy backfill). Idempotent: probes the projection for the
    deterministic id first — present → ``update`` (every field is supplied,
    so changed values overwrite cleanly), absent → ``create``.

    ``actor_user_id`` becomes the journal ``Actor`` (the inviting admin on
    the invite path; the member themselves on membership-derived paths).
    The scope_context mirrors the write scope so the recorded actor
    identity carries the same tenancy bound as the event.
    """

    scope = _person_scope(person.workspace_id)
    actor = Actor(
        kind="user",
        id=f"user:{actor_user_id}",
        scope_context=list(scope),
    )

    properties = person.to_properties()

    # Idempotency probe: is this member already materialized? Query the
    # projection for the Person type and look for our deterministic id.
    existing = await store.query(
        FabricQuery(type_id=PERSON_TYPE_ID, limit=10_000),
        requester_scopes=scope,
    )
    already = any(obj.id == person.id for obj in existing.objects)

    if already:
        # Update merges onto existing properties — every field we write is
        # supplied here, so a changed profile / context overwrites cleanly.
        await store.update(person.id, properties, scope=scope, actor=actor)
    else:
        obj = FabricObject(
            id=person.id,
            type_id=PERSON_TYPE_ID,
            type_name=PERSON_TYPE_NAME,
            properties=properties,
            # Native Fabric provenance, in addition to the property-bag
            # copy: source_connector flags the origin, source_id is the
            # member's user id (the external key this row tracks).
            source_connector=person.source,
            source_id=person.user_id,
        )
        await store.create(obj, scope=scope, actor=actor)


async def materialize_person_from_membership(
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    email: str,
    avatar: str,
    role: str,
    group: str | None = None,
    store: FabricJournalStore | None = None,
) -> Person:
    """Create or update a member's Fabric ``Person`` from live membership data.

    The non-invite materialization path (T-2): the workspace OWNER at
    workspace-create, the lazy backfill on a ``get_person`` miss, and a
    role-change refresh for a member who never had a Person. Identity
    fields come from the member's own profile; ``role`` comes from the
    membership record. Onboarding fields (``focus`` / ``profile_pic``) are
    empty — no admin context exists on this path — and ``invited_by`` is
    empty because nobody invited them; ``source=membership`` records that
    provenance.

    Same idempotent upsert as the invite path (deterministic id), so a
    later re-accept or refresh updates rather than duplicating. The member
    themselves is the journal Actor.
    """

    fabric = store if store is not None else _default_store()
    person = Person(
        id=_person_object_id(workspace_id, user_id),
        workspace_id=workspace_id,
        user_id=user_id,
        name=name,
        email=email,
        avatar=avatar,
        role=role,
        group=group,
        focus="",
        profile_pic="",
        invited_by="",
        source=SOURCE_MEMBERSHIP,
    )
    await _upsert_person(person, actor_user_id=user_id, store=fabric)
    return person


async def _materialize_from_profile(
    workspace_id: str,
    user_id: str,
    *,
    role: str,
    store: FabricJournalStore,
) -> Person:
    """Materialize a Person by reading the member's live auth profile.

    Fetches ``full_name`` / ``email`` / ``avatar`` through the auth service
    (the sole owner of ``models.user`` reads, per cloud rule 2) and funnels
    into :func:`materialize_person_from_membership`. Lazy import: the auth
    entity is only touched when a refresh/backfill actually runs, and the
    people module stays import-light for unit tests that inject a store.

    Raises ``NotFound`` when the user record is missing — callers on
    best-effort paths (listeners, lazy backfill) catch and degrade.
    """

    from pocketpaw_ee.cloud.auth import service as auth_service

    profile = await auth_service.get_profile_by_id(user_id)
    return await materialize_person_from_membership(
        workspace_id=workspace_id,
        user_id=user_id,
        name=profile.full_name or "",
        email=profile.email or "",
        avatar=profile.avatar or "",
        role=role,
        store=store,
    )


async def refresh_person_role(
    workspace_id: str,
    user_id: str,
    role: str,
    *,
    store: FabricJournalStore | None = None,
) -> Person | None:
    """Re-materialize a member's Person after a role change.

    Called by the ``workspace.member_role`` bus subscriber. An existing
    Person keeps every identity/onboarding field and gets the new ``role``;
    a member with NO Person yet (pre-existing member, workspace owner from
    before owners were materialized) is materialized fresh from their live
    profile + the new role — the same convergence the lazy ``get_person``
    backfill provides, just triggered by the role event instead of a read.

    Returns the refreshed Person, or ``None`` if the member's user record
    cannot be read (the caller treats that as a skipped refresh, never an
    error — the lazy backfill converges it later).
    """

    fabric = store if store is not None else _default_store()
    existing = await get_person(workspace_id, user_id, store=fabric, materialize_missing=False)
    if existing is not None:
        person = replace(existing, role=role)
        await _upsert_person(person, actor_user_id=user_id, store=fabric)
        return person
    return await _materialize_from_profile(workspace_id, user_id, role=role, store=fabric)


async def refresh_person_profile(
    user_id: str,
    *,
    store: FabricJournalStore | None = None,
) -> list[Person]:
    """Re-materialize a user's Person in EVERY workspace they belong to.

    Called by the ``profile.updated`` bus subscriber after an auth profile
    edit (full_name / avatar). Reads the fresh profile + membership list
    through the auth service (one call carries both), then per membership:
    an existing Person keeps role/group/focus/pic/provenance and gets the
    new name/email/avatar; a membership with no Person yet is materialized
    fresh from the membership record (which doubles as backfill).

    Returns the refreshed Persons. Raises ``NotFound`` when the user record
    is missing — the listener catches and logs.
    """

    from pocketpaw_ee.cloud.auth import service as auth_service

    fabric = store if store is not None else _default_store()
    profile = await auth_service.get_profile_by_id(user_id)

    refreshed: list[Person] = []
    for membership in profile.workspaces:
        existing = await get_person(
            membership.workspace, user_id, store=fabric, materialize_missing=False
        )
        if existing is not None:
            person = replace(
                existing,
                name=profile.full_name or "",
                email=profile.email or existing.email,
                avatar=profile.avatar or "",
            )
            await _upsert_person(person, actor_user_id=user_id, store=fabric)
        else:
            person = await materialize_person_from_membership(
                workspace_id=membership.workspace,
                user_id=user_id,
                name=profile.full_name or "",
                email=profile.email or "",
                avatar=profile.avatar or "",
                role=membership.role,
                store=fabric,
            )
        refreshed.append(person)
    return refreshed


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _person_from_object(obj: FabricObject, *, workspace_id: str) -> Person:
    """Map a projected Fabric object back to a typed :class:`Person`.

    The inverse of :meth:`Person.to_properties`. ``id`` comes from the
    object's own id; ``workspace_id`` from the read scope (the journal does
    NOT fold it into the property bag — it's carried by the event scope); the
    remaining identity / provenance fields come straight from the property
    bag. ``group`` stays ``None`` when the stored value is falsy.
    """

    props = obj.properties
    return Person(
        id=obj.id,
        workspace_id=workspace_id,
        user_id=str(props.get("user_id", "")),
        name=str(props.get("name", "")),
        email=str(props.get("email", "")),
        avatar=str(props.get("avatar", "")),
        role=str(props.get("role", "")),
        group=props.get("group") or None,
        focus=str(props.get("focus", "")),
        profile_pic=str(props.get("profile_pic", "")),
        invited_by=str(props.get("invited_by", "")),
        source=str(props.get("source", SOURCE_ADMIN_CONTEXT)),
    )


async def get_person(
    workspace_id: str,
    user_id: str,
    *,
    store: FabricJournalStore | None = None,
    materialize_missing: bool = True,
) -> Person | None:
    """Read a member's materialized Fabric ``Person``, or ``None``.

    Queries the journal projection for the ``Person`` type under the member's
    own workspace scope and matches the deterministic id
    (``person-{workspace_id}-{user_id}``). Returns the typed :class:`Person`
    when present. The caller treats ``None`` as "no block", never an error.

    LAZY BACKFILL (T-2): on a miss, when ``materialize_missing`` is true
    (the default), the member's live workspace role is checked — a user who
    IS a member (pre-existing member, or a workspace owner from before
    owners were materialized) gets a Person materialized from their auth
    profile on this first read, and that Person is returned. A non-member
    still returns ``None`` (tenancy holds), and ANY failure on the backfill
    path — membership lookup, profile read, journal write — degrades to
    ``None`` exactly as before, so unit contexts with no cloud DB behave
    unchanged. Internal callers that must not recurse (the refresh paths)
    pass ``materialize_missing=False``.

    Tenant-scoped: ``requester_scopes`` is the single workspace scope, so the
    read can only ever see this workspace's people. Mirrors the materializer's
    query (same ``type_id`` + scope), then narrows to the deterministic id.

    ``store`` is injectable for tests; production callers omit it and get the
    process-wide journal-backed store.
    """

    fabric = store if store is not None else _default_store()
    scope = _person_scope(workspace_id)
    target_id = _person_object_id(workspace_id, user_id)

    result = await fabric.query(
        FabricQuery(type_id=PERSON_TYPE_ID, limit=10_000),
        requester_scopes=scope,
    )
    for obj in result.objects:
        if obj.id == target_id:
            return _person_from_object(obj, workspace_id=workspace_id)

    if not materialize_missing:
        return None

    # Lazy backfill: a real member with no Person converges on first read.
    # Fully defensive — the read contract stays "None, never an error".
    try:
        from pocketpaw_ee.cloud.workspace import service as workspace_service

        role = await workspace_service.get_member_role(workspace_id, user_id)
        if role is None:
            return None
        return await _materialize_from_profile(workspace_id, user_id, role=role, store=fabric)
    except Exception:  # noqa: BLE001 — backfill is best-effort; the miss stays a miss
        logger.debug(
            "lazy Person backfill failed for user=%s workspace=%s",
            user_id,
            workspace_id,
            exc_info=True,
        )
        return None


__all__ = [
    "get_person",
    "materialize_person_from_invite",
    "materialize_person_from_membership",
    "refresh_person_profile",
    "refresh_person_role",
]
