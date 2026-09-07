# tests/cloud/other_hand/test_snapshot_router.py — the snapshot endpoint.
#
# Created: 2026-08-25 (feat/other-hand-surface, Otherhand v1).
#
# This endpoint is a FILESYSTEM WRITE whose filename comes from the URL, so the
# guard tests are the point of the file, not an afterthought. Covers:
#   * happy path — a real PNG lands in the workspace's snapshot dir, and the
#     returned path is absolute, points at the bytes sent, and echoes free_y;
#   * path traversal — ``..``, separators (raw and percent-encoded), a trailing
#     newline, absolute paths — none of them write outside the directory;
#   * tenant scoping — two workspaces sending the same page_id do not collide;
#   * oversize — rejected, and rejected on the base64 string before the decode;
#   * non-PNG — a payload that decodes fine but is not an image;
#   * overwrite — one live snapshot per page, no history.
#
# ``POCKETPAW_WORKSPACE_JAIL_ROOT`` is redirected at a tmp dir so nothing lands
# in the developer's real ``~/.pocketpaw``.

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.other_hand import service as other_hand_service
from pocketpaw_ee.cloud.other_hand.router import router as other_hand_router

WORKSPACE = "ws-otherhand"


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    """A minimal but genuinely valid PNG, built rather than vendored."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def jail_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "workspaces"
    monkeypatch.setenv("POCKETPAW_WORKSPACE_JAIL_ROOT", str(root))
    return root


@pytest.fixture
def client(jail_root: Path) -> TestClient:
    app = FastAPI()
    app.include_router(other_hand_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: WORKSPACE

    @app.exception_handler(CloudError)
    async def _cloud_error(_request, exc: CloudError):  # noqa: ANN202
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, page_id: str, png_base64: str, free_y: int = 820):
    return client.post(
        f"/api/v1/other-hand/pages/{page_id}/snapshot",
        json={"png_base64": png_base64, "free_y": free_y},
    )


# --- happy path -------------------------------------------------------------


def test_snapshot_writes_png_and_returns_absolute_path(client, jail_root):
    png = _png_bytes()
    resp = _post(client, "page-1", _b64(png))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["free_y"] == 820

    path = Path(body["path"])
    assert path.is_absolute()
    assert path.read_bytes() == png
    # Inside this workspace's snapshot dir, and NOT inside the agent-cwd tree
    # (which the jail GC sweeps on an idle TTL).
    assert path.parent == (jail_root / WORKSPACE / "other_hand").resolve()


def test_snapshot_accepts_a_data_uri_prefix(client):
    png = _png_bytes()
    resp = _post(client, "page-2", "data:image/png;base64," + _b64(png))
    assert resp.status_code == 200, resp.text
    assert Path(resp.json()["path"]).read_bytes() == png


def test_snapshot_overwrites_previous_page_state(client):
    """One live snapshot per page — v1 keeps no history."""
    first = _png_bytes(4, 4)
    second = _png_bytes(8, 8)

    path_a = Path(_post(client, "page-3", _b64(first)).json()["path"])
    path_b = Path(_post(client, "page-3", _b64(second)).json()["path"])

    assert path_a == path_b
    assert path_b.read_bytes() == second
    # No stray temp files left behind by the atomic replace.
    assert [p.name for p in path_b.parent.iterdir()] == ["page-3.png"]


def test_snapshots_are_workspace_scoped(client, jail_root, monkeypatch):
    """The same page_id from two workspaces must not collide."""
    _post(client, "shared-id", _b64(_png_bytes()))

    other = other_hand_service.write_snapshot("ws-other", "shared-id", _b64(_png_bytes(8, 8)))
    assert Path(other).parent == (jail_root / "ws-other" / "other_hand").resolve()
    assert Path(other) != jail_root / WORKSPACE / "other_hand" / "shared-id.png"


# --- traversal --------------------------------------------------------------


@pytest.mark.parametrize(
    "page_id",
    [
        "..",
        ".",
        "../escape",
        "..%2Fescape",
        "%2e%2e%2fescape",
        "sub/dir",
        "a\\b",
        "",
        "x" * 129,
    ],
)
def test_snapshot_rejects_traversal_and_unsafe_page_ids(page_id, client, jail_root):
    """A hostile page_id must never write outside the workspace's own dir.

    Some of these are refused by the URL router before the handler is reached
    (a raw separator makes a different route; an empty id fails the path
    constraint). What matters is that NONE of them produce a file, anywhere.
    """
    resp = _post(client, page_id, _b64(_png_bytes()))
    assert resp.status_code in {400, 404, 405, 422}, resp.text

    written = [p for p in jail_root.rglob("*") if p.is_file()] if jail_root.exists() else []
    assert written == [], f"{page_id!r} wrote {written}"


@pytest.mark.parametrize(
    "page_id",
    [
        "..",
        ".",
        "../escape",
        "sub/dir",
        "a\\b",
        "/abs/path",
        # Control characters cannot be expressed in a URL at all — httpx refuses
        # to build the request — so the HTTP test above cannot reach them. They
        # are exercised here because the service is callable without a URL, and
        # because a trailing newline is the specific case a ``$``-anchored
        # pattern (or a ``.strip()`` before validating) would wave through.
        "page\n",
        "page\x00",
        "",
        "x" * 129,
    ],
)
def test_service_rejects_unsafe_page_ids_directly(page_id):
    """The guard lives in the service, so it holds for any future caller that
    does not come through the URL router."""
    with pytest.raises(other_hand_service.SnapshotError) as exc:
        other_hand_service.write_snapshot(WORKSPACE, page_id, _b64(_png_bytes()))
    assert exc.value.code == "other_hand.invalid_page_id"


# --- size + content-type ----------------------------------------------------


def test_snapshot_rejects_oversize_payload(client, jail_root):
    oversize = "A" * (other_hand_service.MAX_SNAPSHOT_B64_CHARS + 1)
    resp = _post(client, "big-page", oversize)

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "other_hand.snapshot_too_large"
    assert [p for p in jail_root.rglob("*") if p.is_file()] == []


def test_oversize_is_rejected_before_decoding(monkeypatch):
    """The cap is checked on the base64 STRING, so a huge payload is never
    materialized as bytes just to be thrown away."""

    def _never(*_args, **_kwargs):
        raise AssertionError("b64decode must not run on an oversize payload")

    monkeypatch.setattr(other_hand_service.base64, "b64decode", _never)
    with pytest.raises(other_hand_service.SnapshotError) as exc:
        other_hand_service.write_snapshot(
            WORKSPACE, "big", "A" * (other_hand_service.MAX_SNAPSHOT_B64_CHARS + 1)
        )
    assert exc.value.status_code == 413


def test_snapshot_rejects_non_png(client, jail_root):
    """Decodes cleanly, is not an image. The endpoint promises the agent a PNG."""
    resp = _post(client, "not-a-png", _b64(b"GIF89a" + b"\x00" * 32))

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "other_hand.not_a_png"
    assert [p for p in jail_root.rglob("*") if p.is_file()] == []


def test_snapshot_rejects_invalid_base64(client):
    resp = _post(client, "junk", "!!!! not base64 !!!!")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "other_hand.invalid_base64"


def test_snapshot_rejects_out_of_page_free_y(client):
    """free_y is bounded to the page at the wire so a nonsense value never
    reaches the agent as a coordinate it would draw at."""
    assert _post(client, "page-9", _b64(_png_bytes()), free_y=99999).status_code == 422
    assert _post(client, "page-9", _b64(_png_bytes()), free_y=-1).status_code == 422


def test_router_is_license_and_workspace_gated():
    """Auth is structural, so assert it structurally.

    A behavioural test cannot see this: the test environment carries a dev
    license (``_dev_license_key``), so ``require_license`` passes even with no
    override, and ``current_workspace_id`` is always overridden above because it
    needs a real authenticated user to resolve. What must hold is that BOTH
    dependencies are actually wired — ``current_workspace_id`` depends on
    ``current_active_user``, so it is the authentication gate as well as the
    tenant scope, and an unauthenticated caller never reaches the handler.
    """
    assert any(dep.dependency is require_license for dep in other_hand_router.dependencies)

    route = next(
        r for r in other_hand_router.routes if r.path == "/other-hand/pages/{page_id}/snapshot"
    )
    assert "POST" in route.methods
    # Router-level deps are ``Depends`` (``.dependency``); the solved per-route
    # sub-dependencies are ``Dependant`` (``.call``).
    assert any(dep.call is current_workspace_id for dep in route.dependant.dependencies)
