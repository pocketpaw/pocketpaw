"""The platform guard: who may reach the cross-tenant axis, and how.

Two separate properties are asserted here.

1. PRINCIPAL. Holding the operator rung is not enough — the request must also
   be a human at an interactive console. This is the defence against the
   failure mode the rung check structurally cannot see: an operator's own AI
   agent, steered by a prompt injection in tenant content, making requests with
   the operator's credential. Every check based on "what role does this user
   hold" passes in that scenario, because the caller really is an operator.

2. COVERAGE. Every route mounted under the platform prefix carries the guard.
   The namespace is the audit boundary, and a boundary maintained by review
   alone is one that holds until the first hurried merge.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.platform.router import router
from starlette.datastructures import Headers
from starlette.requests import Request


class _FakeUser:
    """Minimal stand-in for the User document the guard reads."""

    def __init__(self, platform_role: str | None) -> None:
        self.id = "operator-1"
        self.email = "ops@example.com"
        self.platform_role = platform_role


def _request(
    *,
    cookie: bool = True,
    bearer: bool = False,
    api_key: object | None = None,
    oauth_token: object | None = None,
) -> Request:
    raw: list[tuple[bytes, bytes]] = []
    if cookie:
        raw.append((b"cookie", b"paw_auth=a-real-session-token"))
    if bearer:
        raw.append((b"authorization", b"Bearer a-copyable-token"))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/platform/audit",
        "headers": Headers(raw=raw).raw,
        "query_string": b"",
        "client": ("10.0.0.5", 1234),
    }
    request = Request(scope)
    if api_key is not None:
        request.state.api_key = api_key
    if oauth_token is not None:
        request.state.oauth_token = oauth_token
    return request


async def _run(guard, request: Request, user: _FakeUser):
    """Invoke the inner guard callable directly, bypassing FastAPI's injection."""
    return await guard(request=request, user=user)


# ---------------------------------------------------------------------------
# 1. Principal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_with_session_cookie_is_allowed() -> None:
    guard = require_platform("platform.audit.read")
    user = _FakeUser("operator")
    assert await _run(guard, _request(), user) is user


@pytest.mark.asyncio
async def test_bearer_credential_is_refused_even_for_a_real_operator() -> None:
    """The agent case.

    A bearer token is the credential an automated caller is most likely to hold:
    copyable, long-lived, and passed around in config. The user here is a
    genuine operator and the rung check would pass; the request is refused
    anyway, on the credential.
    """
    guard = require_platform("platform.audit.read")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(cookie=False, bearer=True), _FakeUser("operator"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "platform.non_interactive_principal"


@pytest.mark.asyncio
async def test_bearer_alongside_a_cookie_is_still_refused() -> None:
    """A request carrying both is ambiguous about which credential authenticated
    it. The safe reading is the weaker one."""
    guard = require_platform("platform.audit.read")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(cookie=True, bearer=True), _FakeUser("operator"))
    assert exc.value.detail == "platform.non_interactive_principal"


@pytest.mark.asyncio
async def test_api_key_principal_is_refused() -> None:
    """An API key minted by an operator must not inherit their platform rung.

    Worth stating why this is not theoretical: API-key CRUD is guarded by bare
    workspace membership, so any member of any workspace an operator belongs to
    can mint keys. If a key carried the minter's platform rung, workspace
    membership would become a path to cross-tenant access.
    """
    guard = require_platform("platform.audit.read")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(api_key={"scopes": ["audit.read"]}), _FakeUser("operator"))
    assert exc.value.detail == "platform.non_interactive_principal"


@pytest.mark.asyncio
async def test_oauth_token_principal_is_refused() -> None:
    guard = require_platform("platform.audit.read")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(oauth_token={"sub": "app"}), _FakeUser("operator"))
    assert exc.value.detail == "platform.non_interactive_principal"


@pytest.mark.asyncio
async def test_missing_session_cookie_is_refused() -> None:
    guard = require_platform("platform.audit.read")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(cookie=False), _FakeUser("operator"))
    assert exc.value.detail == "platform.non_interactive_principal"


@pytest.mark.asyncio
async def test_principal_is_checked_before_the_rung() -> None:
    """A non-interactive caller is refused on the credential, not the rung.

    Ordering matters for the error the caller sees: reporting "insufficient
    role" to an agent holding an operator token would send whoever is debugging
    it looking for a permissions problem that does not exist.
    """
    guard = require_platform("platform.settings.write")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(cookie=False, bearer=True), _FakeUser("support"))
    assert exc.value.detail == "platform.non_interactive_principal"


@pytest.mark.asyncio
async def test_the_same_token_resent_as_a_cookie_is_accepted_KNOWN_GAP() -> None:
    """DOCUMENTS A KNOWN GAP. This test passing is not good news.

    `auth/core.py` builds the cookie and bearer backends from the same
    `get_jwt_strategy()`, so the two tokens are interchangeable. A caller
    refused for `Authorization: Bearer <jwt>` can re-send the identical token as
    `Cookie: paw_auth=<jwt>` and pass — the header refusal costs one line to
    step around.

    This test exists so the gap is written down in the suite rather than only in
    a docstring, and so that whoever binds the token to its transport has a test
    to INVERT. When that lands, this should become an assertion that the
    re-sent token is refused, and the name should lose its suffix.

    The fixture here fakes the authenticated user, so it cannot prove the token
    is genuinely interchangeable — that claim rests on reading auth/core.py. It
    proves the narrower thing the guard controls: the guard looks at the header
    name, not at which backend authenticated the caller.
    """
    guard = require_platform("platform.audit.read")
    user = _FakeUser("operator")

    # Refused when the credential is presented as a bearer header.
    with pytest.raises(HTTPException):
        await _run(guard, _request(cookie=False, bearer=True), user)

    # Accepted when the very same credential arrives as a cookie.
    assert await _run(guard, _request(cookie=True, bearer=False), user) is user


# ---------------------------------------------------------------------------
# 2. Rung, once the principal is accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_platform_role_is_refused() -> None:
    guard = require_platform("platform.audit.read")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(), _FakeUser(None))
    assert exc.value.status_code == 403
    assert exc.value.detail == "platform.not_operator"


@pytest.mark.asyncio
async def test_workspace_owner_gets_nothing() -> None:
    """A workspace role is not a platform role, at the dependency level too."""
    guard = require_platform("platform.audit.read")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(), _FakeUser("owner"))
    assert exc.value.detail == "platform.not_operator"


@pytest.mark.asyncio
async def test_support_cannot_reach_an_operator_action() -> None:
    guard = require_platform("platform.settings.write")
    with pytest.raises(HTTPException) as exc:
        await _run(guard, _request(), _FakeUser("support"))
    assert exc.value.detail == "platform.insufficient_role"


def test_unregistered_action_fails_at_decoration_time() -> None:
    """Building a guard for an unknown action raises when the app is built.

    A typo takes the process down at startup rather than silently guarding a
    money route with a rule that does not exist.
    """
    with pytest.raises(KeyError):
        require_platform("platform.typo.read")


# ---------------------------------------------------------------------------
# 3. Coverage — the namespace is the boundary
# ---------------------------------------------------------------------------


def test_every_platform_route_carries_the_guard() -> None:
    """No route under the platform prefix may be reachable without the guard."""
    unguarded: list[str] = []

    for route in router.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        if not _has_platform_guard(dependant):
            unguarded.append(f"{sorted(getattr(route, 'methods', []))} {route.path}")

    assert unguarded == [], (
        f"Every route under /platform must depend on require_platform(...). Unguarded: {unguarded}"
    )


def _has_platform_guard(dependant) -> bool:
    """Depth-first search for a dependency built by ``require_platform``."""
    if getattr(dependant.call, "__platform_action__", None) is not None:
        return True
    return any(_has_platform_guard(sub) for sub in dependant.dependencies)


def test_platform_routes_exist_to_be_checked() -> None:
    """Guards against the coverage test passing vacuously on an empty router."""
    assert len(router.routes) >= 2
