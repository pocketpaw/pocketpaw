# tests/cloud/member_day_digest/test_member_day_digest_router.py
# Created: 2026-06-08 — VIP Onboarding Phase B chunk 6 (the intent board's
#   read API). Pins the gated GET surface over the chunk-5 digest service.
#
# The centerpiece is the per-member isolation guarantee carried to the REST
# door: the digest is built for the AUTHENTICATED principal (``ctx.user_id``)
# and ONLY them — there is NO ``member_id`` query/body param, so a caller can
# structurally NEVER request another member's digest. This mirrors the
# outcomes router's "tenancy from auth context, never the wire" stance and the
# chat-path gate that the same digest already sits behind.
#
# The router endpoint is a plain async function, so the tests call it directly
# with the FastAPI ``Depends`` values (a ``RequestContext``) supplied by
# keyword. The digest service is patched to a spy so no Gmail/Calendar/OAuth
# I/O happens and the test asserts WHICH member id the router threaded in.

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind  # noqa: E402
from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.cloud.member_day_digest import router as digest_router  # noqa: E402
from pocketpaw_ee.cloud.member_day_digest.dto import (  # noqa: E402
    DigestEvent,
    DigestMail,
    MemberDayDigest,
)

pytestmark = pytest.mark.asyncio

WORKSPACE = "w1"
CALLER = "member-alice-objid"
OTHER = "member-bob-objid"


def _ctx(user_id: str = CALLER, workspace_id: str | None = WORKSPACE) -> RequestContext:
    """Build an authed RequestContext the way ``request_context`` would."""
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="req-test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _sample_digest(member_id: str = CALLER, workspace_id: str = WORKSPACE) -> MemberDayDigest:
    """A populated digest the spy returns — every field the board renders."""
    return MemberDayDigest(
        workspace_id=workspace_id,
        member_id=member_id,
        events=[
            DigestEvent(
                summary="Standup",
                start="2026-06-10T09:00:00Z",
                end="2026-06-10T09:15:00Z",
                location="Room A",
            )
        ],
        unread_mail_count=3,
        top_mail=[DigestMail(subject="Invoice", sender="ap@acme.test", date="")],
        errors=[],
    )


def _patch_service(spy):
    """Patch the digest service the router delegates to."""
    return patch.object(digest_router, "member_day_digest", spy)


# ===========================================================================
# Happy path — GET returns the CALLER's OWN digest.
# ===========================================================================


async def test_get_returns_callers_own_digest():
    """The endpoint returns the digest for the authenticated principal, with
    every field the intent board renders (events + unread count + top mail)."""
    spy = AsyncMock(return_value=_sample_digest())
    with _patch_service(spy):
        result = await digest_router.get_member_day_digest(ctx=_ctx())

    assert isinstance(result, MemberDayDigest)
    assert result.member_id == CALLER
    assert result.workspace_id == WORKSPACE
    assert len(result.events) == 1
    assert result.events[0].summary == "Standup"
    assert result.unread_mail_count == 3
    assert len(result.top_mail) == 1
    assert result.top_mail[0].subject == "Invoice"
    assert result.empty is False


async def test_empty_digest_passes_through():
    """A member with nothing on / no connected accounts gets an EMPTY digest,
    not an error — the same graceful shape the briefing relies on."""
    empty = MemberDayDigest(workspace_id=WORKSPACE, member_id=CALLER)
    spy = AsyncMock(return_value=empty)
    with _patch_service(spy):
        result = await digest_router.get_member_day_digest(ctx=_ctx())

    assert result.member_id == CALLER
    assert result.empty is True
    assert result.events == []
    assert result.unread_mail_count == 0
    assert result.top_mail == []


# ===========================================================================
# THE ISOLATION INVARIANT — member_id is the AUTHENTICATED principal, never
# the wire. The caller can only ever get their OWN digest.
# ===========================================================================


async def test_member_id_is_the_authenticated_principal():
    """The service is called with ``member_id == ctx.user_id`` and
    ``workspace_id == ctx.workspace_id`` — both taken from auth, never a
    param. A second principal would resolve a different member id."""
    spy = AsyncMock(return_value=_sample_digest())
    with _patch_service(spy):
        await digest_router.get_member_day_digest(ctx=_ctx(user_id=CALLER))

    spy.assert_awaited_once()
    # Tolerate positional or keyword delegation — assert by value, not arity.
    bound = inspect.signature(_dummy_service).bind(*spy.await_args.args, **spy.await_args.kwargs)
    bound.apply_defaults()
    assert bound.arguments["member_id"] == CALLER
    assert bound.arguments["workspace_id"] == WORKSPACE


async def test_different_principal_gets_their_own_digest():
    """A different authenticated caller resolves to THEIR id — the router
    never reuses one member's id for another (no shared/cached id surface)."""
    spy = AsyncMock(side_effect=lambda workspace_id, member_id, **_: _sample_digest(member_id))
    with _patch_service(spy):
        a = await digest_router.get_member_day_digest(ctx=_ctx(user_id=CALLER))
        b = await digest_router.get_member_day_digest(ctx=_ctx(user_id=OTHER))

    assert a.member_id == CALLER
    assert b.member_id == OTHER


async def test_no_member_id_param_on_endpoint():
    """STRUCTURAL guarantee: the endpoint exposes NO ``member_id`` (nor any
    user-id / override) parameter. The only inputs are FastAPI deps, so a
    caller CANNOT request another member's digest via query or body. This is
    the leak-prevention contract, asserted on the signature itself."""
    params = set(inspect.signature(digest_router.get_member_day_digest).parameters)
    forbidden = {"member_id", "user_id", "member", "for_member", "principal", "target"}
    assert params.isdisjoint(forbidden), (
        f"endpoint must not accept a member-id override; found {params & forbidden}"
    )


async def test_missing_active_workspace_is_rejected():
    """No active workspace → a 400 CloudError (setup error), never a
    cross-tenant collapse and never an HTTPException leaking past _core.http.
    The service must not be called when tenancy is missing."""
    spy = AsyncMock(return_value=_sample_digest())
    with _patch_service(spy):
        with pytest.raises(CloudError) as exc:
            await digest_router.get_member_day_digest(ctx=_ctx(workspace_id=None))

    assert exc.value.status_code == 400
    spy.assert_not_awaited()


# ===========================================================================
# Mount — the router carries the canonical route and is registered where the
# other cloud routers are (asserted on source, since building the full app
# pulls Mongo/realtime init that a unit test should not require).
# ===========================================================================


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_router_exposes_the_canonical_get_route():
    """The router defines exactly the gated GET at ``/member-day-digest``
    (``/api/v1`` is prepended at mount). A single read route, no override."""
    routes = [
        r for r in digest_router.router.routes if getattr(r, "path", None) == "/member-day-digest"
    ]
    assert len(routes) == 1, "router must expose exactly the /member-day-digest route"
    assert routes[0].methods == {"GET"}


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_router_is_included_in_cloud_mount_block():
    """The intent board can reach the endpoint: the router is wired into the
    cloud app's include_router block under the ``/api/v1`` prefix."""
    import pocketpaw_ee.cloud as cloud_pkg

    src = inspect.getsource(cloud_pkg)
    assert "member_day_digest_router" in src, (
        "member_day_digest router is not included in cloud __init__ mount block"
    )


# A reference signature mirroring the digest service so the happy-path test can
# bind the spy's call args by name regardless of positional/keyword delegation.
def _dummy_service(workspace_id, member_id, **_kwargs):  # pragma: no cover - shape only
    ...
