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

UNTAGGED FILES ARE LISTED TO NOBODY

The "bounded residue" did not stop growing: ``save_generated`` wrote untagged
names by default, so every agent-generated file (media MCP) was listed to every
workspace and a brand-new workspace's gallery showed ~50 files it never made
(UX-6). ``save_generated`` now requires the workspace, and an untagged name is
hidden from every listing. It still serves by URL.

Mutations that must fail these tests: dropping either dependency, dropping the
owner prefix from an upload or from ``save_generated``, making ``visible_to``
return True for another workspace's file, and making it return True for an
untagged one.
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
    # Untagged, so listed to nobody. An UPLOAD of "ws-holiday.png" is stored as
    # "ws-<token>-ws-holiday.png" and belongs to its uploader.
    assert storage.visible_to("ws-holiday.png", WS_A) is False
    uploaded = storage.owned_name_prefix(WS_A) + "ws-holiday.png"
    assert storage.owner_of(uploaded) == storage.capture_owner_token(WS_A)


def test_an_untagged_legacy_file_is_listed_to_nobody():
    """It cannot be attributed, so showing it to everyone leaks it to everyone.

    There is no single-tenant mode to keep it for: GET /media needs an
    authenticated cloud workspace to run at all.
    """
    assert storage.owner_of("1700000000000-abc123.png") is None
    assert storage.visible_to("1700000000000-abc123.png", WS_A) is False
    assert storage.visible_to("1700000000000-abc123.png", None) is False
    assert storage.visible_to(storage.owned_name_prefix(WS_A) + "x.png", None) is False


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
    assert legacy not in names, "an untagged legacy file appeared in the listing"
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


async def test_a_replacing_upload_removes_only_its_own_workspaces_old_file(
    router_module, monkeypatch
):
    """The editor's Save re-renders the same timeline into the gallery. ``replaces``
    deletes the previous render so the gallery holds one copy, and must never reach
    a file another workspace owns. Mutation that must fail this: dropping the owner
    check before the delete."""
    deleted: list[str] = []

    class _Stored:
        size = 1

    mine = f"{storage.owned_name_prefix(WS_A)}timeline.mp4"
    theirs = f"{storage.owned_name_prefix(WS_B)}timeline.mp4"

    class _Adapter:
        async def exists(self, key: str) -> bool:
            return storage.name_from_key(key) in (mine, theirs)

        async def put(self, key: str, stream, mime: str):
            return _Stored()

        async def delete(self, key: str) -> None:
            deleted.append(storage.name_from_key(key))

    monkeypatch.setattr(storage, "get_adapter", lambda: _Adapter())

    class _File:
        filename = "timeline.mp4"

        async def read(self) -> bytes:
            return b"x"

    await router_module.upload_media(file=_File(), workspace_id=WS_A, replaces=mine)
    await router_module.upload_media(file=_File(), workspace_id=WS_A, replaces=theirs)

    assert deleted == [mine]


# ---------------------------------------------------------------------------
# UX-6: an agent-generated file leaked into every workspace's gallery.
# ---------------------------------------------------------------------------


async def test_an_agent_generated_file_does_not_appear_in_another_workspaces_listing(
    tmp_path, router_module, monkeypatch
):
    """A brand-new workspace's Studio gallery showed ~50 files it never made.

    The media MCP saved through ``save_generated`` with no owner prefix, and the
    listing showed every untagged file to every workspace. Drive the real MCP
    handler as workspace A, then list as workspace B.
    """
    import base64
    import json
    from unittest.mock import AsyncMock, patch

    import httpx
    import pocketpaw_ee.agent.mcp_servers.media as media

    from pocketpaw.uploads.local import LocalStorageAdapter

    media_root = tmp_path / "media-root"
    (media_root / "generated").mkdir(parents=True)
    monkeypatch.setattr(storage, "_ADAPTER", LocalStorageAdapter(root=media_root))
    monkeypatch.setattr(
        router_module, "tracked_generation_filenames", AsyncMock(return_value=set())
    )
    monkeypatch.setenv("POCKETPAW_LITELLM_API_BASE", "https://proxy.test:4000")
    monkeypatch.setenv("POCKETPAW_LITELLM_API_KEY", "sk-proxy")
    monkeypatch.setattr(media, "_GALLERY_STATE", {})
    b64 = base64.b64encode(b"png-bytes").decode()
    monkeypatch.setattr(
        media,
        "_PROXY_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"b64_json": b64}]})),
    )

    with (
        patch.object(media, "_identity", return_value=(WS_A, "u-a")),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-a",
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.agent_create",
            AsyncMock(return_value=({"x": 1}, "pkt-a", None)),
        ),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._image_generate_handler({"prompt": "a red bicycle"})
    assert result.get("is_error") is not True, result

    async def _list(workspace_id: str) -> list[str]:
        resp = await router_module.list_media(sort="newest", limit=50, workspace_id=workspace_id)
        return [m["name"] for m in json.loads(resp.body)["media"]]

    assert len(await _list(WS_A)) == 1, "the generating workspace lost its own file"
    assert await _list(WS_B) == [], "workspace A's agent-generated file is in workspace B's gallery"


async def test_save_generated_refuses_a_write_with_no_owner(tmp_path, monkeypatch):
    """``workspace_id`` is required so a future caller cannot write an unowned file."""
    from pocketpaw.uploads.local import LocalStorageAdapter

    monkeypatch.setattr(storage, "_ADAPTER", LocalStorageAdapter(root=tmp_path))
    with pytest.raises(TypeError):
        await storage.save_generated(b"x", mime="image/png")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        await storage.save_generated(b"x", mime="image/png", workspace_id="")
    assert not (tmp_path / "generated").exists()


async def test_save_generated_stamps_the_owner_and_keeps_the_timestamp(tmp_path, monkeypatch):
    from pocketpaw.uploads.local import LocalStorageAdapter

    monkeypatch.setattr(storage, "_ADAPTER", LocalStorageAdapter(root=tmp_path))
    url = await storage.save_generated(b"x", mime="image/png", workspace_id=WS_A)
    name = url.rsplit("/", 1)[-1]
    assert storage.owner_of(name) == storage.capture_owner_token(WS_A)
    assert storage.visible_to(name, WS_A) is True
    assert storage.visible_to(name, WS_B) is False
    assert storage.modified_from_name(name) > 0, "the owner prefix hid the timestamp"

    capture = (
        await storage.save_generated(b"x", mime="image/png", workspace_id=WS_A, capture=True)
    ).rsplit("/", 1)[-1]
    assert storage.capture_owner_of(capture) == storage.capture_owner_token(WS_A)
    assert storage.owner_of(capture) is None
    assert storage.modified_from_name(capture) > 0


async def test_agent_generated_audio_is_stamped_with_its_workspace(tmp_path, monkeypatch):
    """The media MCP's second write path (TTS) carries the owner too."""
    from unittest.mock import AsyncMock, patch

    import httpx
    import pocketpaw_ee.agent.mcp_servers.media as media

    from pocketpaw.uploads.local import LocalStorageAdapter

    monkeypatch.setattr(storage, "_ADAPTER", LocalStorageAdapter(root=tmp_path))
    monkeypatch.setenv("POCKETPAW_LITELLM_API_BASE", "https://proxy.test:4000")
    monkeypatch.setenv("POCKETPAW_LITELLM_API_KEY", "sk-proxy")
    monkeypatch.setattr(media, "_GALLERY_STATE", {})
    monkeypatch.setattr(
        media,
        "_PROXY_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(200, content=b"mp3-bytes")),
    )
    with (
        patch.object(media, "_identity", return_value=(WS_A, "u-a")),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-a",
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.agent_create",
            AsyncMock(return_value=({"x": 1}, "pkt-a", None)),
        ),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._audio_generate_handler({"text": "hello"})
    assert result.get("is_error") is not True, result

    names = [p.name for p in (tmp_path / "generated").iterdir()]
    assert len(names) == 1
    assert storage.owner_of(names[0]) == storage.capture_owner_token(WS_A)
    assert storage.visible_to(names[0], WS_B) is False


async def test_a_studio_generation_is_stamped_with_its_workspace(tmp_path, monkeypatch):
    """Every studio generation path saves through ``_save_image_bytes``."""
    from pocketpaw_ee.cloud.studio import service as studio_service

    from pocketpaw.uploads.local import LocalStorageAdapter

    monkeypatch.setattr(storage, "_ADAPTER", LocalStorageAdapter(root=tmp_path))
    url = await studio_service._save_image_bytes(b"x", workspace_id=WS_A)
    name = url.rsplit("/", 1)[-1]
    assert storage.owner_of(name) == storage.capture_owner_token(WS_A)
