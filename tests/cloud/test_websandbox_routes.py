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
