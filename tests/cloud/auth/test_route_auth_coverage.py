"""The auth audit must cover every router that is actually mounted.

WHY THIS FILE EXISTS

``test_route_auth_audit.py`` asserts the invariant that matters — every cloud
route authenticates for itself, because the global AuthMiddleware deliberately
does not gate ``/api/v1/``. It has been green throughout. Its ``ROUTER_MODULES``
is a **hand-typed list of 24 routers**, and ``cloud/__init__.py`` mounts 75.

So the assertion was true of a third of the surface and silent about the rest.
Four criticals shipped through the gap, every one of them on a router nobody had
added to the list:

  * the Daytona VM control plane — 13 routes with no session guard, tenant read
    from an ``X-Workspace-Id`` header, including provision, destroy, a terminal
    into the VM, and file write;
  * ``/api/v1/cloud_projects`` — the same shape, on tenant project storage;
  * ``/api/v1/media`` — one global gallery, anonymous list and upload;
  * ``POST /api/v1/studio/transcribe`` — anonymous spend on a deployment-wide
    Deepgram key.

A list you have to remember to update is not a gate. This file makes the covered
set DERIVED and asserts it is complete, so a new router is in scope the day it is
mounted and the only way out is to say so out loud.

HOW THE DERIVATION WORKS, AND WHY NOT A REGEX

``mount_cloud`` is parsed with ``ast``. Every ``app.include_router(<name>)`` is
resolved back through the module's imports to ``(module, attribute)``.

The first version of this used a regex over the source and silently resolved 67
of 75 — it could not see a parenthesised multi-line ``from x import (...)``.
Under-counting was the original defect, so a derivation that under-counts
quietly is not an improvement. ``ast`` sees every import form, and anything it
still cannot resolve must be named in ``INLINE_ROUTERS`` with a reason.

WHAT A DEPENDENCY WALK CANNOT SEE

Plenty of routes here authenticate correctly INSIDE the handler rather than
through a FastAPI dependency, and that is legitimate. ``POST /paw-bar/chat``
resolves a signed site key, enforces a fail-closed origin allowlist, binds the
widget to the key's workspace AND pocket, and checks quota — none of it visible
to a walk over ``route.dependant``.

That is exactly why every allowlist entry has to NAME the check it relies on. An
entry that says "public by design" and stops is worthless: the next reader
cannot confirm it without re-deriving the whole thing, which is how
``test_webhook_skips_verification_without_secret`` ended up asserting a
fail-open as intended behaviour for months.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pocketpaw_ee.cloud._core.context import loopback_or_request_context, request_context
from pocketpaw_ee.cloud.auth.core import current_active_user

CLOUD_INIT = Path(__file__).resolve().parents[3] / "ee" / "pocketpaw_ee" / "cloud" / "__init__.py"

#: Same identity-based walk the sibling audit uses. Copied rather than imported
#: so this file does not go quiet if that one is refactored.
SESSION_GUARDS = frozenset(
    {id(current_active_user), id(request_context), id(loopback_or_request_context)}
)
GUARD_QUALNAMES = frozenset({"require_scope.<locals>._check"})

#: Routers built inline inside ``mount_cloud`` rather than imported, so no
#: module-based enumeration can reach them. Each must say what guards it.
INLINE_ROUTERS: dict[str, str] = {
    "_files_v2": (
        "APIRouter built in mount_cloud; both routes (/files/tree, /files/browse) "
        "take Depends(current_active_user) and reject a workspace_id query param "
        "that disagrees with the session (files.workspace_mismatch)"
    ),
}

#: The measured gap. See test_the_coverage_gap_is_measured_and_does_not_grow.
KNOWN_UNAUDITED_ROUTER_COUNT = 48


def _resolve_mounted() -> tuple[set[tuple[str, str]], list[tuple[int, str]]]:
    """Every ``(module, attr)`` mounted by ``mount_cloud``, plus what did not resolve."""
    tree = ast.parse(CLOUD_INIT.read_text(encoding="utf-8"))

    alias: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                alias[a.asname or a.name] = (node.module, a.name)

    mounted: set[tuple[str, str]] = set()
    unresolved: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and node.args
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name) and arg.id in alias:
            mounted.add(alias[arg.id])
        else:
            unresolved.append((node.lineno, getattr(arg, "id", ast.dump(arg)[:60])))
    return mounted, unresolved


def _requires_auth(dependant, seen: set[int] | None = None) -> bool:
    seen = seen if seen is not None else set()
    if id(dependant) in seen:
        return False
    seen.add(id(dependant))
    if id(dependant.call) in SESSION_GUARDS:
        return True
    if getattr(dependant.call, "__qualname__", "") in GUARD_QUALNAMES:
        return True
    return any(_requires_auth(d, seen) for d in dependant.dependencies)


def test_every_mounted_router_resolves_or_is_named_inline():
    """An include_router the derivation cannot follow must be acknowledged.

    This is the assertion that keeps the derivation honest. Without it a
    refactor that changes how a router is imported would silently drop it from
    coverage, which is the exact failure this whole file exists to end.
    """
    _, unresolved = _resolve_mounted()
    unnamed = [(line, name) for line, name in unresolved if name not in INLINE_ROUTERS]
    assert unnamed == [], (
        "include_router calls that could not be resolved to a module and are not "
        "in INLINE_ROUTERS. Coverage silently excludes these:\n  "
        + "\n  ".join(f"{CLOUD_INIT.name}:{line} {name}" for line, name in unnamed)
    )


def test_the_hand_typed_audit_list_names_only_routers_that_are_mounted():
    """The sibling audit may lag, but it must never name a router that is gone.

    A stale entry means the audit asserts against something the app no longer
    mounts, which reads as coverage and is not.
    """
    audit = importlib.import_module("tests.cloud.auth.test_route_auth_audit")
    listed = {module for _name, module in audit.ROUTER_MODULES}
    mounted = {module for module, _attr in _resolve_mounted()[0]}
    stale = sorted(listed - mounted)
    assert stale == [], (
        "these routers are audited but no longer mounted by mount_cloud:\n  " + "\n  ".join(stale)
    )


def test_the_coverage_gap_is_measured_and_does_not_grow():
    """The gap itself, pinned as a number so it can only shrink.

    Deliberately a measurement rather than a demand that the gap be zero today.
    Closing it means classifying every route on 48 more routers, and a bulk
    import of confident-sounding allowlist entries written without reading the
    handlers would be worse than the gap — that is precisely the failure mode
    this file's docstring describes. Each router moved onto the audited list is
    a real review.

    Lower the number when you move one. It must never go up: a PR that mounts a
    new router without adding it to ROUTER_MODULES fails here.
    """
    audit = importlib.import_module("tests.cloud.auth.test_route_auth_audit")
    listed = {module for _name, module in audit.ROUTER_MODULES}
    mounted = {module for module, _attr in _resolve_mounted()[0]}
    unaudited = mounted - listed

    assert len(unaudited) <= KNOWN_UNAUDITED_ROUTER_COUNT, (
        f"{len(unaudited)} mounted routers are outside the auth audit, up from "
        f"{KNOWN_UNAUDITED_ROUTER_COUNT}. Add a new router to ROUTER_MODULES in "
        "the same PR that mounts it:\n  " + "\n  ".join(sorted(unaudited))
    )


@pytest.mark.parametrize(("module", "attr"), sorted(_resolve_mounted()[0]))
def test_every_mounted_router_imports(module, attr):
    """A router that fails to import is invisible to every assertion above.

    Its own parametrised case, so an import error is a named failure rather than
    a quietly smaller set.
    """
    mod = importlib.import_module(module)
    assert hasattr(mod, attr), f"{module} has no attribute {attr}"


def test_the_unguarded_surface_is_enumerated(capsys):
    """Report every unguarded route across the WHOLE mounted surface.

    A report rather than an assertion, so the number stays visible while the
    per-route classification is done. ``pytest -s`` to read it.
    """
    mounted, _ = _resolve_mounted()
    rows: list[str] = []
    for module, attr in sorted(mounted):
        router = getattr(importlib.import_module(module), attr)
        for route in router.routes:
            if isinstance(route, APIRoute) and not _requires_auth(route.dependant):
                method = sorted(route.methods or {"ANY"})[0]
                rows.append(f"{method:7} {route.path:55} {module}")

    with capsys.disabled():
        print(f"\n\n{len(rows)} routes reachable without a route-level session guard.")
        print("Each is public by design or a hole. See ALLOWED_WITHOUT_ROUTE_GUARD")
        print("in test_route_auth_audit.py for the ones already classified.\n")
        for row in rows:
            print("   ", row)

    assert rows, "expected some public routes (login, webhooks); an empty list means the walk broke"


def test_require_license_does_not_count_as_authentication():
    """The single most load-bearing assumption in this whole audit.

    ``require_license`` checks one process-wide licence key. It is identical for
    every caller, including one with no session, so a router whose only
    dependency is ``Depends(require_license)`` is anonymous-reachable. Several
    routers carry exactly that and nothing else — it is what made
    ``POST /studio/transcribe`` spend Deepgram credit for strangers, and the
    route's own docstring described it as "License-gated ... like every other
    route here".

    Add it to the guard sets above and the enumeration below goes quiet across
    a large part of the surface while looking more correct than before. This
    asserts the premise directly, so that cannot happen silently.
    """
    from fastapi import APIRouter, Depends
    from pocketpaw_ee.cloud.license import require_license

    probe = APIRouter()

    @probe.get("/probe", dependencies=[Depends(require_license)])
    async def _probe() -> dict:
        return {}

    route = next(r for r in probe.routes if isinstance(r, APIRoute))
    assert not _requires_auth(route.dependant), (
        "require_license is being treated as a session guard. It is an "
        "entitlement check and answers the same for an anonymous caller."
    )
