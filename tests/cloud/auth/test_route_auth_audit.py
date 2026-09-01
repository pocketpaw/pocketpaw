"""Every mounted HTTP route requires auth, or is on the public allowlist.

Created 2026-08-01, after a report that "I was logged out and the chat API
still worked". The API turned out to be guarded — the browser was still
holding a valid session cookie while the UI believed otherwise — but the only
reason that could be established quickly was by reading the routers by hand.
This makes it a standing assertion instead.

THE INVARIANT THIS FILE EXISTS FOR:

    **Cloud routes authenticate at the route level.** The global
    ``AuthMiddleware`` does not gate ``/api/v1/``, so every cloud route needs
    its own guard — either a session dependency, or an explicit in-handler
    check that this file's allowlist names.

That is by design, not an oversight. ``_auth_dispatch`` builds
``is_auth_optional`` from ``auth_optional_prefixes = ("/api/v1/",)``, and its
final gate reads

    if not is_valid and not is_auth_optional:
        return JSONResponse(status_code=401, ...)

The cascade still runs and still populates ``request.state`` — dashboard
session cookies, API keys, ``full_access`` — so routes mounted at the shared
prefix can read it. It simply does not do the rejecting, because a caller who
is mid-login has no session yet and ee routes resolve identity through
fastapi-users instead. ``test_route_level_guards_are_required_under_api_v1``
pins the behaviour so this stays a stated property rather than an assumption,
and checks it against a public source address so the localhost bypass cannot
make the result look better than it is.

The practical consequence, and the reason to state it plainly: a route-level
guard is **required**, not belt-and-braces. Anything on
ALLOWED_WITHOUT_ROUTE_GUARD is relying on something else entirely, and each
entry has to say what.

How the walk works: every route's dependency tree is searched for a transitive
dependency that rejects a caller with no session. There is more than one, which
is why this walks by identity rather than grepping for a name:
``current_active_user`` (fastapi-users, 401s without a session) and
``request_context`` (resolves an API key or a session, 401s with neither).
``current_user_id``, ``current_workspace_id`` and ``require_action`` all
resolve through one of them.

Adding a genuinely public endpoint means adding it to the allowlist with a
reason. That is the point: it forces the decision to be explicit and reviewed
rather than implicit in an omitted parameter.

2026-08-01 — the seven ``REVIEW`` markers are resolved. The four pockets
routes authenticate in-handler, and their entries below now name the specific
check instead of deferring it. The three ``/sessions`` routes were brought onto
route-level guards and have left the list; see
tests/cloud/sessions/test_runtime_route_auth.py.

The general lesson is about the category rather than those routes.
"Authorisation happens inside the handler" held for four of the seven and not
for the other three, and nothing distinguished them from the outside — which
is why an entry here has to name the check it relies on, so the next reader
can confirm it in one step instead of re-deriving it.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.routing import APIRoute
from pocketpaw_ee.cloud._core.context import loopback_or_request_context, request_context
from pocketpaw_ee.cloud.auth.core import current_active_user

#: Dependencies that REJECT an unauthenticated caller. There is more than one,
#: which is exactly why this audit walks for them by identity rather than
#: grepping for a single name:
#:   * current_active_user — fastapi-users' active-session guard (401s).
#:   * request_context     — resolves an API key OR a session, and raises 401
#:                           when it has neither. Most entity routers use this.
#:   * loopback_or_request_context — the same, plus a loopback-only internal
#:                           header path for the local agent. An external
#:                           caller still collapses to 403 auth.required.
#: require_license is NOT a session guard: it checks entitlement, not identity,
#: so a route carrying only that is still anonymous-reachable.
#:
#: A fourth family is matched by qualname rather than identity, because it is a
#: closure minted per call: ``require_scope("...")`` from the OSS dashboard-auth
#: cascade. It is fail-closed and accepts a master token, a dashboard session
#: cookie, a scoped API key, an OAuth token — OR genuine localhost, when
#: ``localhost_auth_bypass`` is on (it defaults to TRUE). See
#: test_localhost_bypass_is_on_by_default for why that matters when testing.
GUARD_QUALNAMES = frozenset({"require_scope.<locals>._check"})
#: NOTE current_optional_user is deliberately NOT here: it yields None for an
#: anonymous caller and leaves the decision to the handler, so a route that
#: depends only on it has made no decision at the routing layer.
SESSION_GUARDS = frozenset(
    {id(current_active_user), id(request_context), id(loopback_or_request_context)}
)

# Every router mounted by cloud/__init__.py. Kept as strings so a router that
# fails to import is a visible skip rather than a collection error.
ROUTER_MODULES = [
    ("agent_activity", "pocketpaw_ee.cloud.agent_activity.router"),
    ("agents", "pocketpaw_ee.cloud.agents.router"),
    ("audit", "pocketpaw_ee.cloud.audit.router"),
    ("auth", "pocketpaw_ee.cloud.auth.router"),
    ("billing", "pocketpaw_ee.cloud.billing.router"),
    ("chat", "pocketpaw_ee.cloud.chat.router"),
    ("chat_runs", "pocketpaw_ee.cloud.chat.runs.router"),
    ("codeagent", "pocketpaw_ee.cloud.codeagent.router"),
    ("codeconnect", "pocketpaw_ee.cloud.codeconnect.router"),
    ("codegit", "pocketpaw_ee.cloud.codegit.router"),
    ("codeproject", "pocketpaw_ee.cloud.codeproject.router"),
    ("connectors", "pocketpaw_ee.cloud.connectors.router"),
    ("credits", "pocketpaw_ee.cloud.credits.router"),
    ("decisions", "pocketpaw_ee.cloud.decisions.router"),
    ("discovery", "pocketpaw_ee.cloud.discovery.router"),
    ("entitlements", "pocketpaw_ee.cloud.entitlements.router"),
    ("files", "pocketpaw_ee.cloud.files.router"),
    ("foresight", "pocketpaw_ee.cloud.foresight.router"),
    ("meetings", "pocketpaw_ee.cloud.meetings.router"),
    ("pockets", "pocketpaw_ee.cloud.pockets.router"),
    ("pocket_chat", "pocketpaw_ee.cloud.pockets.chat_router"),
    ("projects", "pocketpaw_ee.cloud.projects.router"),
    ("sessions", "pocketpaw_ee.cloud.sessions.router"),
    ("workspace", "pocketpaw_ee.cloud.workspace.router"),
]

#: Routes that are public BY DESIGN, each with the reason it has to be.
#: Anything not here must require a session.
ALLOWED_WITHOUT_ROUTE_GUARD: dict[str, str] = {
    # You cannot hold a session before you have signed in.
    "POST /auth/login": "issues the session",
    # BYOK-first onboarding (2026-09-01): a guest has no account yet by
    # definition. Guarded by the per-IP mint rate limit + the provider key
    # validation (a caller must present a working credential to get anything).
    "POST /auth/guest": "mints the guest session; rate-limited per IP, key validated first",
    "POST /auth/bearer/login": "issues the session (native/API transport)",
    "POST /auth/register": "creates the account",
    "POST /auth/mfa/challenge": "second factor, carries its own short-lived mfa_token",
    "POST /auth/forgot-password": "user cannot sign in by definition",
    "POST /auth/reset-password": "authorised by the emailed token, not a session",
    "POST /auth/request-verify-token": "authorised by the emailed token",
    "POST /auth/verify": "authorised by the emailed token",
    "POST /auth/logout": "clearing a session must not require one",
    "POST /auth/bearer/logout": "same, bearer transport",
    # The OAuth dance: the browser arrives back from the provider with no
    # session yet. Guarded by the single-use state store instead.
    "GET /auth/social/providers": "which buttons to render, before sign-in",
    "GET /auth/social/{provider}/login": "starts consent; no session exists yet",
    "GET /auth/social/callback": "provider redirect; guarded by single-use state",
    "POST /auth/social/exchange": "how a desktop client gets its FIRST token",
    "GET /auth/sso/{workspace_slug}/login": "enterprise OIDC entry point",
    "GET /auth/sso/callback": "IdP redirect; guarded by single-use state",
    # Serves an image by opaque filename to an <img> tag, which cannot carry
    # bearer auth. Pre-existing; flagged in the report below rather than
    # silently blessed.
    "GET /auth/avatar/{filename}": "served to <img>; opaque filename",
    # --- token-authorised: the caller is logged out BY DEFINITION ---
    "GET /workspaces/invites/{token}": "invitee has no account yet; the token authorises",
    "GET /workspaces/invites/{token}/preview": "same, shown before accepting",
    "POST /workspaces/invites/{token}/decline": "same, declining must not need an account",
    "GET /pockets/shared/{token}": "public share link; the token IS the grant",
    "GET /codeconnect/github/callback": "GitHub redirect; carries a signed state",
    # --- static catalogues, no tenant data ---
    "GET /billing/plans": "public price list",
    "GET /billing/site-plans": "public price list",
    "GET /agents/backends": "static list of backend names",
    "GET /pockets/builtin-widgets": "static widget catalogue",
    "GET /decisions/_ping": "liveness probe",
    # --- git smart-HTTP: authenticates in-handler, not via a FastAPI dep ---
    "GET /codegit/{owner}/{repo}/info/refs": "git protocol; own token auth in-handler",
    "POST /codegit/{owner}/{repo}/git-upload-pack": "git protocol; own token auth in-handler",
    "POST /codegit/{owner}/{repo}/git-receive-pack": "git protocol; own token auth in-handler",
    # --- optional-user by design: the handler decides per resource ---
    # These take fastapi-users' OPTIONAL dependency, so a caller with no
    # session reaches the handler and authorisation happens inside it. Each
    # entry names the specific in-handler check, so nobody has to re-derive it.
    #
    # All four were read line by line on 2026-08-01, replacing the REVIEW
    # markers that stood here. Three /sessions routes carried the same marker
    # and were moved onto route-level guards instead, so they have left this
    # list; see tests/cloud/sessions/test_runtime_route_auth.py.
    "GET /pockets/{pocket_id}": "optional-user; handler allows public pockets",
    "POST /pockets/{pocket_id}/spec/merge": (
        "optional-user; router raises 401 auth.required when neither the "
        "loopback internal bypass nor a session yields an identity, then "
        "merge_spec runs _check_domain_edit_access on the resolved user"
    ),
    "POST /pockets/{pocket_id}/reconcile/preview": (
        "optional-user; _resolve_reconcile_identity raises 401 auth.required "
        "with no session and no bypass, then _load_for_reconcile gates read "
        "access"
    ),
    "POST /pockets/{pocket_id}/reconcile/apply": (
        "optional-user; same 401 from _resolve_reconcile_identity, then "
        "_load_for_reconcile(require_edit=True) gates edit access BEFORE the "
        "idempotent-skip path, so a no-op reconcile by a non-editor still 403s"
    ),
    "POST /pockets/{pocket_id}/sources/{source}/refresh": (
        "not a session route at all: authenticated by the per-pocket webhook "
        "secret via resolve_webhook_pocket (constant-time compare, uniform 403 "
        "for wrong/missing secret and for a missing pocket, so not an "
        "existence oracle); an upstream system has no PocketPaw session"
    ),
}


def _route_key(route: APIRoute) -> str:
    methods = sorted(route.methods or set())
    return f"{methods[0] if methods else 'ANY'} {route.path}"


def _requires_auth(dependant, seen: set[int] | None = None) -> bool:
    """Whether this route transitively depends on an active session."""
    seen = seen if seen is not None else set()
    if id(dependant) in seen:
        return False
    seen.add(id(dependant))
    if id(dependant.call) in SESSION_GUARDS:
        return True
    if getattr(dependant.call, "__qualname__", "") in GUARD_QUALNAMES:
        return True
    return any(_requires_auth(d, seen) for d in dependant.dependencies)


def _collect() -> list[tuple[str, str]]:
    """(router_name, route_key) for every HTTP route that lacks a session guard."""
    unguarded: list[tuple[str, str]] = []
    for name, module_path in ROUTER_MODULES:
        try:
            router = importlib.import_module(module_path).router
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            pytest.fail(f"router {name} ({module_path}) failed to import: {exc}")
        for route in router.routes:
            # WebSockets authenticate in-handler (ticket / JWT / cookie), since
            # a dependency cannot 401 a socket. Covered by the chat WS tests.
            if not isinstance(route, APIRoute):
                continue
            if not _requires_auth(route.dependant):
                unguarded.append((name, _route_key(route)))
    return unguarded


def test_no_http_route_ships_without_an_auth_decision():
    unguarded = _collect()
    surprises = [
        f"{name}: {key}" for name, key in unguarded if key not in ALLOWED_WITHOUT_ROUTE_GUARD
    ]
    assert not surprises, (
        "These routes require no session and are not on the public allowlist.\n"
        "Add a session dependency, or — if the route is public by design — add\n"
        "it to the allowlist with the reason:\n  " + "\n  ".join(sorted(surprises))
    )


def test_the_chat_api_specifically_requires_a_session():
    # The endpoint family behind the "I was logged out and chat still worked"
    # report. Named separately so a regression here is unmistakable.
    router = importlib.import_module("pocketpaw_ee.cloud.chat.router").router
    http_routes = [r for r in router.routes if isinstance(r, APIRoute)]
    assert http_routes, "expected the chat router to expose HTTP routes"

    unguarded = [_route_key(r) for r in http_routes if not _requires_auth(r.dependant)]
    assert unguarded == [], f"chat routes with no session guard: {unguarded}"


def test_the_allowlist_does_not_rot():
    # An entry that no longer matches a real route is dead weight, and dead
    # weight is how a genuinely public route later hides in the noise.
    live = {key for _, key in _collect()}
    stale = sorted(set(ALLOWED_WITHOUT_ROUTE_GUARD) - live)
    assert not stale, (
        "ALLOWED_WITHOUT_ROUTE_GUARD entries that no longer match a route "
        f"lacking a route-level guard — delete them: {stale}"
    )


async def test_route_level_guards_are_required_under_api_v1():
    """Cloud routes must carry their own guard: the middleware does not gate them.

    This is the invariant the rest of the file rests on, so it is measured
    rather than described. It is deliberate — ee routes resolve identity
    through fastapi-users, and gating the shared prefix globally would reject
    callers who are mid-login — but it is easy to assume the opposite, so the
    property is asserted here rather than left to a reader's memory.

    The addresses rule out the two ways a green result could be misleading.
    203.0.113.9 is TEST-NET-3, a public address, so no loopback rule can be
    what produces the outcome; and ``/internal/whatever`` from that same
    address confirms the dispatcher still rejects where it is meant to, so
    this cannot pass by the dispatcher silently no-opping.
    """
    from starlette.requests import Request

    from pocketpaw.dashboard_auth import _auth_dispatch

    def _anonymous(path: str, ip: str) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "client": (ip, 51234),
                "server": ("api.example.com", 443),
                "scheme": "https",
                "root_path": "",
            }
        )

    remote = "203.0.113.9"

    # The control: a non-/api/v1 path from the same anonymous remote caller IS
    # refused, so this test cannot pass by the dispatcher doing nothing.
    control = await _auth_dispatch(_anonymous("/internal/whatever", remote))
    assert control is not None and control.status_code == 401

    for path in ("/api/v1/sessions/runtime", "/api/v1/pockets/pk-1/spec/merge"):
        assert await _auth_dispatch(_anonymous(path, remote)) is None, (
            f"{path} was refused by the global middleware. If that now holds "
            "for all of /api/v1/, the invariant at the top of this file has "
            "changed and the docstring needs updating to match."
        )


def test_localhost_bypass_is_on_by_default():
    """The reason a signed-out browser can still reach the API locally.

    ``_is_genuine_localhost`` grants ``request.state.full_access`` to any
    request whose client address is loopback, which satisfies
    ``require_scope`` and every other dashboard-auth check. It is deliberate
    for self-hosted single-user installs, and it refuses spoofed
    ``X-Forwarded-For`` so a remote caller cannot claim it.

    The trap it creates: on localhost you CANNOT tell "this endpoint requires
    auth" from "this endpoint let me in because I am on localhost". Verifying
    an auth change locally means setting
    ``POCKETPAW_LOCALHOST_AUTH_BYPASS=false`` first, or the test proves
    nothing. Asserted here so the default is a known quantity rather than a
    surprise during the next auth investigation.
    """
    from pocketpaw.config import Settings

    field = Settings.model_fields["localhost_auth_bypass"]
    assert field.default is True, (
        "localhost_auth_bypass no longer defaults to True — update the note in "
        "the auth docs and in this test's docstring."
    )
