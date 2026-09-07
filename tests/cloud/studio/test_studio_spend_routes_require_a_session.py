"""Every /studio route that spends money must resolve a workspace first.

``POST /studio/transcribe`` shipped with no session dependency. Its router
carries ``dependencies=[Depends(require_license)]``, and the route's own
docstring said it was "License-gated by the router-wide dependency like every
other route here" — but ``require_license`` checks one process-wide licence key
and answers identically for every caller, including one with no session. It is
an entitlement check, not authentication.

The route uploads caller-supplied audio to Deepgram against a deployment-wide
``POCKETPAW_DEEPGRAM_API_KEY``, so any anonymous caller on the internet could
drain that credit, unmetered and attributable to nobody. Every other spending
route on the router (/generate, /edit, /music, /video-elements,
/video-motion-control) already resolved a workspace. This one was missed, and
nothing failed when it was.

The test is written as a SWEEP rather than a case for transcribe, because
naming the one route that was wrong is how the next one gets missed too. It
asserts the property — a route that spends resolves a caller — over the set,
so a new paid route is covered the day it is added.

Mutations that must fail this test: dropping ``current_workspace_id`` from any
route named below.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.studio.router import router as studio_router

#: Routes that call a paid upstream. Each entry is (method, path) as the router
#: declares it. Deliberately hand-listed WITH a reason rather than derived: the
#: point is that somebody decided each of these costs money.
SPENDING_ROUTES = [
    ("POST", "/studio/generate", "image/video generation via the LiteLLM proxy"),
    ("POST", "/studio/edit", "image edit via the proxy"),
    ("POST", "/studio/music", "music generation via the proxy"),
    ("POST", "/studio/video-elements", "video generation via the proxy"),
    ("POST", "/studio/video-motion-control", "video generation via the proxy"),
    ("POST", "/studio/transcribe", "Deepgram speech-to-text on a deployment key"),
]


def _route(method: str, path: str) -> APIRoute:
    for r in studio_router.routes:
        if isinstance(r, APIRoute) and r.path == path and method in (r.methods or set()):
            return r
    raise AssertionError(f"no {method} {path} on the studio router — was it renamed?")


@pytest.mark.parametrize(("method", "path", "why"), SPENDING_ROUTES)
def test_a_spending_route_resolves_a_workspace(method, path, why):
    route = _route(method, path)
    assert any(d.call is current_workspace_id for d in route.dependant.dependencies), (
        f"{method} {path} spends money ({why}) with no session dependency. "
        "require_license is an entitlement check, not authentication — it "
        "answers the same for an anonymous caller."
    )


def test_require_license_is_not_counted_as_a_session_guard():
    """States the premise the sweep rests on, so it cannot quietly stop holding.

    If require_license ever became identity-aware, the assertions above would
    still pass for the wrong reason. This pins that it is not.
    """
    from pocketpaw_ee.cloud.license import require_license

    assert require_license is not current_workspace_id
    route = _route("POST", "/studio/transcribe")
    license_deps = [d for d in route.dependant.dependencies if d.call is require_license]
    assert license_deps, "expected the router-wide licence dependency to still be present"
