"""The media gallery was one global namespace with no auth on list or upload.

``ee/pocketpaw_ee/cloud/media/router.py`` declared
``APIRouter(prefix="/media", ...)`` with no dependencies, and ``media_key``
was ``f"generated/{name}"`` — no tenant anywhere. So:

  * ``GET /api/v1/media`` returned every workspace's filenames and URLs to
    anyone on the internet, and every listed URL then served;
  * ``POST /api/v1/media`` let an anonymous caller store arbitrary image or
    video bytes on this infrastructure and have them served from this origin.

THE ASYMMETRY IS THE DESIGN, NOT AN OVERSIGHT

``serve_media`` is still reachable without a session, and that is deliberate.
Gallery URLs are embedded in ripple specs, and a pocket published as a site is
served from the edge with no cookie — gating the read would blank every
published page that shows a generated image. Reads stay capability-based on an
unguessable name, exactly like /uploads. A /browser capture is the exception
already in the code: private to its workspace, never embedded, and serve_media
does enforce its owner token.

So the fix is: authenticate the two routes that do not need to be public, and
make files ATTRIBUTABLE so the listing can be scoped. The key cannot carry a
workspace segment — serve_media refuses any name containing a slash — so the
owner travels in the filename, reusing the mechanism captures already use.

LEGACY FILES STAY VISIBLE, ON PURPOSE

A file written before this carries no token and appears in every workspace's
listing. That matches what ``studio.service.list_generations`` already does with
untagged history records (``_workspace is None`` is shown to everyone), and
hiding them instead would empty existing galleries. It is a bounded residue that
stops growing, not a hole that stays open.

Mutations that must fail these tests: dropping either dependency, dropping the
owner prefix from an upload, and making ``visible_to`` return True for another
workspace's file.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.routing import APIRoute
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.media import storage

ROUTER = "pocketpaw_ee.cloud.media.router"

WS_A = "ws-alpha"
WS_B = "ws-beta"


@pytest.fixture
def router_module():
    return importlib.import_module(ROUTER)


def _route(module, method: str, path: str) -> APIRoute:
    for r in module.router.routes:
        if isinstance(r, APIRoute) and r.path == path and method in (r.methods or set()):
            return r
    raise AssertionError(f"no {method} {path} on the media router")


# ---------------------------------------------------------------------------
# Auth, per route, because they do not all want the same answer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), [("GET", "/media"), ("POST", "/media")])
def test_list_and_upload_require_a_session(router_module, method, path):
    route = _route(router_module, method, path)
    assert any(d.call is current_workspace_id for d in route.dependant.dependencies), (
        f"{method} {path} does not resolve a workspace from the caller; it "
        "listed and accepted writes for one global namespace"
    )


def test_serving_stays_reachable_without_a_session(router_module):
    """Asserted so nobody 'finishes the job' and breaks every published site.

    If this ever needs to change, the ripple specs that embed
    ``/api/v1/media/<name>`` have to change with it.
    """
    route = _route(router_module, "GET", "/media/{name:path}")
    assert not any(d.call is current_workspace_id for d in route.dependant.dependencies)


# ---------------------------------------------------------------------------
# Attribution.
# ---------------------------------------------------------------------------


def test_an_owned_name_round_trips_to_its_workspace():
    name = storage.owned_name_prefix(WS_A) + "edit.png"
    assert storage.owner_of(name) == storage.capture_owner_token(WS_A)
    assert storage.visible_to(name, WS_A) is True
    assert storage.visible_to(name, WS_B) is False


def test_the_workspace_id_is_not_recoverable_from_the_name():
    """The token is a digest, so a URL in an image widget does not leak a tenant id."""
    name = storage.owned_name_prefix(WS_A) + "edit.png"
    assert WS_A not in name


def test_a_file_merely_named_like_an_owned_one_is_not_treated_as_owned():
    """Shape-checked, not prefix-checked.

    A user's own upload called "ws-holiday.png" is not an owned file, and
    treating it as one would hide it from every listing including its owner's.
    """
    assert storage.owner_of("ws-holiday.png") is None
    assert storage.visible_to("ws-holiday.png", WS_A) is True


def test_an_untagged_legacy_file_stays_visible():
    """Deliberate, and it matches studio.service.list_generations.

    Change this and existing galleries empty out; that is a product decision,
    not a refactor.
    """
    assert storage.owner_of("1700000000000-abc123.png") is None
    assert storage.visible_to("1700000000000-abc123.png", WS_A) is True
    assert storage.visible_to("1700000000000-abc123.png", None) is True


def test_a_capture_is_not_mistaken_for_an_owned_upload():
    """The two mechanisms share a token but not a prefix, and must not collide.

    A capture is enforced on READ; an owned upload is not. Confusing them
    either exposes a private capture or 404s a published site's image.
    """
    capture = storage.capture_name_prefix(WS_A) + "1700000000000-abc.png"
    assert storage.capture_owner_of(capture) == storage.capture_owner_token(WS_A)
    assert storage.owner_of(capture) is None


# ---------------------------------------------------------------------------
# The listing actually applies it.
# ---------------------------------------------------------------------------


def test_the_local_listing_excludes_another_workspaces_file(tmp_path, router_module):
    mine = storage.owned_name_prefix(WS_A) + "mine.png"
    theirs = storage.owned_name_prefix(WS_B) + "theirs.png"
    legacy = "1700000000000-legacy.png"
    for name in (mine, theirs, legacy):
        (tmp_path / name).write_bytes(b"x")

    names = {e["name"] for e in router_module._local_entries(tmp_path, "newest", set(), WS_A)}
    assert mine in names
    assert legacy in names
    assert theirs not in names, "another workspace's file appeared in the listing"


async def test_an_upload_is_stamped_with_the_callers_workspace(router_module, monkeypatch):
    """The route must APPLY the prefix, not merely have one available.

    This test exists because a mutation escaped without it. Dropping the stamp
    leaves uploads untagged, untagged files are visible to everyone by the
    legacy rule above, and every other test in this file still passed — the
    listing filter was correct and had nothing to filter on.
    """
    written: list[str] = []

    class _Stored:
        size = 1

    class _Adapter:
        async def exists(self, key: str) -> bool:
            return False

        async def put(self, key: str, stream, mime: str):
            written.append(key)
            return _Stored()

    monkeypatch.setattr(storage, "get_adapter", lambda: _Adapter())

    class _File:
        filename = "edit.png"

        async def read(self) -> bytes:
            return b"x"

    await router_module.upload_media(file=_File(), workspace_id=WS_A)

    assert written, "the upload never reached the adapter"
    name = storage.name_from_key(written[0])
    assert storage.owner_of(name) == storage.capture_owner_token(WS_A), (
        f"the stored name carries no owner token: {name!r}"
    )
    assert storage.visible_to(name, WS_B) is False
