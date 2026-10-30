# test_websandbox_routes.py — guards against path-capture bugs in the websandbox
# router (BP-3b follow-up).
#
# Why this exists: the router registers ``GET /{row_id}`` early, and FastAPI
# matches routes in REGISTRATION ORDER. So any collection-level route added later
# with a single path segment — ``/repo-archive``, say — is silently swallowed by
# ``/{row_id}`` and resolves to "look up a sandbox with that id", which 404s. It
# is a confusing failure: the endpoint exists, the client calls the right URL,
# and the server answers 404 as though the route were never registered.
#
# That happened. The fix is structural (put such routes under a two-segment
# prefix), and this test keeps it fixed: every literal GET path registered after
# ``/{row_id}`` must have at least two segments.
#
# Changed 2026-07-20 (RR-2, feat/code-runtime-requirements): the generic rule
# above was already in place when the second occurrence happened, because a rule
# only fires for routes that exist — it cannot notice a route someone is ABOUT to
# add. So every individual runtime route is now pinned by name as well: the
# requirements probe, the generalized archive, the per-runtime credential broker,
# and the two deprecated aliases the shipped frontend still calls. Deleting an
# alias should be a deliberate act that breaks a named test, not a silent rename
# that 404s in production.
from __future__ import annotations

from fastapi.routing import APIRoute
from pocketpaw_ee.cloud.websandbox.router import router

# Paths include the router's own prefix.
_PREFIX = "/websandbox"
_ROW_ID_PATH = f"{_PREFIX}/{{row_id}}"


def _sub_segments(path: str) -> list[str]:
    """Segments BELOW the router prefix (what /{row_id} competes with)."""
    rest = path[len(_PREFIX) :] if path.startswith(_PREFIX) else path
    return [seg for seg in rest.strip("/").split("/") if seg]


def _routes() -> list[APIRoute]:
    return [r for r in router.routes if isinstance(r, APIRoute)]


def test_row_id_route_is_registered() -> None:
    # If this ever moves, the reasoning below needs revisiting.
    paths = [r.path for r in _routes()]
    assert _ROW_ID_PATH in paths


def test_no_single_segment_literal_get_is_shadowed_by_row_id() -> None:
    """A one-segment literal GET after /{row_id} can never be reached."""
    routes = _routes()
    row_id_index = next(i for i, r in enumerate(routes) if r.path == _ROW_ID_PATH)

    shadowed = []
    for route in routes[row_id_index + 1 :]:
        if "GET" not in route.methods:
            continue
        segments = _sub_segments(route.path)
        if len(segments) != 1:
            continue
        # A single segment that is a literal (not a path param) collides.
        if segments[0] and not segments[0].startswith("{"):
            shadowed.append(route.path)

    assert not shadowed, (
        f"These GET routes are unreachable — /{{row_id}} captures them first: {shadowed}. "
        "Give them a two-segment path (e.g. /browserpod/<name>) or register them "
        "before /{row_id}."
    )


def test_repo_archive_is_reachable() -> None:
    """The archive endpoint specifically — this is the one that regressed."""
    archive = next(
        (r for r in _routes() if r.path.endswith("repo-archive")),
        None,
    )
    assert archive is not None, "repo-archive route is missing"
    assert len(_sub_segments(archive.path)) >= 2, (
        f"{archive.path} has one literal segment and will be captured by /{{row_id}}"
    )


# ---------------------------------------------------------------------------
# RR-2 — every runtime route, pinned by name.
# ---------------------------------------------------------------------------


def _assert_reachable(path: str) -> APIRoute:
    """The route exists AND cannot be swallowed by an earlier path param.

    "Reachable" here means two things, and both have failed before: the route is
    registered at all, and nothing registered earlier matches the same shape. The
    second is the subtle one — a route can be present in the app and still answer
    404 because ``/{row_id}`` claimed the URL first.
    """
    routes = _routes()
    match = next((r for r in routes if r.path == path), None)
    assert match is not None, (
        f"{path} is not registered. Registered GET paths: "
        f"{sorted(r.path for r in routes if 'GET' in r.methods)}"
    )

    segments = _sub_segments(path)
    assert len(segments) >= 2, (
        f"{path} has one segment below the prefix and will be captured by /{{row_id}}"
    )

    # Nothing registered BEFORE it may match the same URL shape. A same-arity
    # earlier route whose leading segment is a path param (i.e. /{row_id}/…)
    # wins on registration order and this route becomes dead.
    own_index = routes.index(match)
    for earlier in routes[:own_index]:
        if "GET" not in earlier.methods:
            continue
        earlier_segments = _sub_segments(earlier.path)
        if len(earlier_segments) != len(segments):
            continue
        shadows = all(e.startswith("{") or e == mine for e, mine in zip(earlier_segments, segments))
        assert not shadows, (
            f"{path} is shadowed by the earlier route {earlier.path} — FastAPI "
            "matches in registration order, so this endpoint will 404."
        )
    return match


def test_runtime_requirements_route_is_reachable() -> None:
    """The pre-boot requirements probe — the whole point of RR-2."""
    _assert_reachable(f"{_PREFIX}/runtimes/requirements")


def test_runtime_archive_route_is_reachable() -> None:
    """The generalized archive path (seeds any in-tab runtime, not just BrowserPod)."""
    _assert_reachable(f"{_PREFIX}/runtimes/archive")


def test_runtime_credentials_route_is_reachable() -> None:
    """The per-runtime credential broker."""
    _assert_reachable(f"{_PREFIX}/runtimes/{{runtime_id}}/credentials")


def test_deprecated_browserpod_aliases_still_registered() -> None:
    """The shipped frontend still calls these — they must survive one release.

    Named explicitly so removing an alias is a deliberate act that fails a test
    with an obvious name, rather than a rename that quietly 404s in production.
    """
    _assert_reachable(f"{_PREFIX}/browserpod/repo-archive")
    _assert_reachable(f"{_PREFIX}/browserpod/credentials")
