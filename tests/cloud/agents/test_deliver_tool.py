# test_deliver_tool.py — ART-4 deliver_artifact in-process MCP tool.
#
# 2026-06-29: deliver now returns the Files module's STABLE download link
# (``/api/v1/uploads/{file_id}``) instead of a self-minted, expiring presign — the
# delivered file is already a first-class /files row. Assertions updated to expect
# the stable url and no ``expires_in_seconds``; the upload + scoping behavior is
# unchanged.
#
# Locks the deliver path's behavior end-to-end against the real EEUploadService +
# a mongomock-backed Mongo store and a local StorageAdapter rooted in a tmp home:
#   * single file  → workspace-scoped upload, stable /files link, guessed mime,
#                    shows in the tenant's file list;
#   * directory    → zipped (application/zip) and the zip round-trips;
#   * two tenants  → each delivers, ZERO cross-read (Mongo scope + presign gate);
#   * renderable   → HTML/SVG comes back as the stable link with an attachment-only
#                    mime (outside INLINE_MIMES), so the download route never
#                    renders it inline;
#   * path safety  → ``..`` / absolute / symlink-out / cross-tenant all rejected;
#   * guards       → missing identity, missing file, and the size cap.
"""Tests for the deliver_artifact agent tool."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def beanie_db():
    """Beanie initialized on a fresh mongomock client with the upload models, so
    the MongoFileStore the deliver tool builds internally persists for real."""
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    client = AsyncMongoMockClient()
    db = client[f"test_deliver_{uuid.uuid4().hex[:8]}"]

    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    from pocketpaw_ee.cloud.uploads.models import FileFolder, FileUpload

    await init_beanie(database=db, document_models=[FileUpload, FileFolder])
    yield db


@pytest.fixture(autouse=True)
def hermetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin Path.home() into tmp so BOTH the workspace jail root
    (~/.pocketpaw/workspaces) and the upload adapter root (~/.pocketpaw/uploads)
    stay off the real home dir. Force the local upload adapter."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("POCKETPAW_UPLOAD_ADAPTER", raising=False)
    monkeypatch.delenv("POCKETPAW_WORKSPACE_JAIL_ROOT", raising=False)
    monkeypatch.delenv("POCKETPAW_DELIVER_MAX_MB", raising=False)
    return home


def _bind(workspace_id: str, user_id: str, session: str):
    from pocketpaw_ee.cloud.chat.agent_service import attach_agent_identity

    return attach_agent_identity(
        workspace_id=workspace_id, user_id=user_id, session_mongo_id=session
    )


def _detach(tokens) -> None:
    from pocketpaw_ee.cloud.chat.agent_service import detach_agent_identity

    detach_agent_identity(tokens)


def _agent_cwd() -> Path:
    """The jail dir the bound run's agent works in (where relative paths land)."""
    from pocketpaw_ee.cloud.agent_jail import resolve_agent_cwd

    cwd = resolve_agent_cwd()
    assert cwd is not None
    return Path(cwd)


def _body(resp: dict) -> dict:
    assert "is_error" not in resp, resp
    return json.loads(resp["content"][0]["text"])


async def _read_blob(storage_key: str) -> bytes:
    """Read a stored blob back through the same local adapter the tool wrote it
    with, so a directory delivery can be unzipped and asserted."""
    from pocketpaw.uploads.factory import build_adapter

    adapter = build_adapter(Path.home() / ".pocketpaw" / "uploads")
    return b"".join([c async for c in adapter.open(storage_key)])


async def test_deliver_single_file(beanie_db) -> None:
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    tokens = _bind("wsA", "uA", "sessA")
    try:
        (_agent_cwd() / "report.txt").write_text("hello report")
        resp = await _deliver_handler({"path": "report.txt"})
    finally:
        _detach(tokens)

    body = _body(resp)
    assert body["ok"] is True
    assert body["filename"] == "report.txt"
    assert body["mime"] == "text/plain"
    assert body["file_id"]
    # Stable Files-module link, not a self-minted expiring presign.
    assert body["url"] == f"/api/v1/uploads/{body['file_id']}"
    assert "expires_in_seconds" not in body

    # Visible in the tenant's file list (workspace-scoped Mongo row).
    rec = await MongoFileStore().get_scoped(body["file_id"], workspace="wsA")
    assert rec is not None
    assert rec.filename == "report.txt"
    assert rec.mime == "text/plain"


async def test_deliver_directory_zips_and_roundtrips(beanie_db) -> None:
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    tokens = _bind("wsA", "uA", "sessA")
    try:
        cwd = _agent_cwd()
        bundle = cwd / "bundle"
        (bundle / "sub").mkdir(parents=True)
        (bundle / "index.html").write_text("<html>hi</html>")
        (bundle / "sub" / "app.js").write_text("console.log(1)")
        resp = await _deliver_handler({"path": "bundle"})
    finally:
        _detach(tokens)

    body = _body(resp)
    assert body["ok"] is True
    assert body["filename"] == "bundle.zip"
    assert body["mime"] == "application/zip"

    rec = await MongoFileStore().get_scoped(body["file_id"], workspace="wsA")
    assert rec is not None and rec.mime == "application/zip"

    # The stored blob is a real zip carrying the directory tree.
    data = await _read_blob(rec.storage_key)
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert "bundle/index.html" in names
    assert "bundle/sub/app.js" in names


async def test_two_tenants_no_cross_read(beanie_db) -> None:
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    from pocketpaw.uploads.config import UploadSettings
    from pocketpaw.uploads.errors import NotFound
    from pocketpaw.uploads.factory import build_adapter

    ta = _bind("wsA", "uA", "sessA")
    try:
        (_agent_cwd() / "secretA.txt").write_text("alpha")
        file_a = _body(await _deliver_handler({"path": "secretA.txt"}))["file_id"]
    finally:
        _detach(ta)

    tb = _bind("wsB", "uB", "sessB")
    try:
        (_agent_cwd() / "secretB.txt").write_text("bravo")
        file_b = _body(await _deliver_handler({"path": "secretB.txt"}))["file_id"]
    finally:
        _detach(tb)

    store = MongoFileStore()
    # Each file is invisible from the OTHER tenant's workspace scope.
    assert await store.get_scoped(file_b, workspace="wsA") is None
    assert await store.get_scoped(file_a, workspace="wsB") is None

    # And the presign read-gate refuses a cross-tenant grant.
    root = Path.home() / ".pocketpaw" / "uploads"
    svc = EEUploadService(
        adapter=build_adapter(root), meta=store, cfg=UploadSettings(local_root=root)
    )
    with pytest.raises(NotFound):
        await svc.presigned_get(file_a, "uB", "wsB", 300)


async def test_path_escapes_rejected(beanie_db) -> None:
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler
    from pocketpaw_ee.cloud.agent_jail import workspace_jail_root

    # A real secret OUTSIDE the jail, and a sibling tenant's jail file.
    outside = Path.home() / "outside_secret.txt"
    outside.write_text("TOPSECRET")
    above_jail = workspace_jail_root() / "evil.txt"
    above_jail.parent.mkdir(parents=True, exist_ok=True)
    above_jail.write_text("nope")

    tb = _bind("wsB", "uB", "sessB")
    try:
        victim = _agent_cwd() / "victimB.txt"
        victim.write_text("tenant-B-data")
    finally:
        _detach(tb)

    tokens = _bind("wsA", "uA", "sessA")
    try:
        cwd = _agent_cwd()
        link = cwd / "escape_link"
        link.symlink_to(outside)

        responses = [
            await _deliver_handler({"path": "../../../evil.txt"}),  # relative climb
            await _deliver_handler({"path": str(outside)}),  # absolute outside
            await _deliver_handler({"path": "/etc/passwd"}),  # host file
            await _deliver_handler({"path": "escape_link"}),  # symlink out
            await _deliver_handler({"path": str(victim)}),  # cross-tenant jail
        ]
    finally:
        _detach(tokens)

    for resp in responses:
        assert resp.get("is_error") is True, resp


async def test_missing_identity_errors(beanie_db) -> None:
    """Called with no run identity bound (not a cloud chat stream)."""
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler

    resp = await _deliver_handler({"path": "anything.txt"})
    assert resp["is_error"] is True
    assert "workspace and user context" in resp["content"][0]["text"]


async def test_missing_file_errors(beanie_db) -> None:
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler

    tokens = _bind("wsA", "uA", "sessA")
    try:
        resp = await _deliver_handler({"path": "does_not_exist.txt"})
    finally:
        _detach(tokens)
    assert resp["is_error"] is True
    assert "no such file" in resp["content"][0]["text"]


async def test_oversize_rejected(beanie_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler

    monkeypatch.setenv("POCKETPAW_DELIVER_MAX_MB", "0.00001")  # ~10 bytes
    tokens = _bind("wsA", "uA", "sessA")
    try:
        (_agent_cwd() / "big.txt").write_text("x" * 1000)
        resp = await _deliver_handler({"path": "big.txt"})
    finally:
        _detach(tokens)
    assert resp["is_error"] is True
    assert "deliver limit" in resp["content"][0]["text"]


@pytest.mark.parametrize(
    ("filename", "content", "expected_mime"),
    [
        ("evil.html", "<script>alert(1)</script>", "text/html"),
        ("evil.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "image/svg+xml"),
    ],
)
async def test_deliver_renderable_returns_stable_attachment_link(
    beanie_db, filename, content, expected_mime
) -> None:
    """A delivered HTML/SVG artifact comes back as the stable Files link, and its
    mime is one the download route serves as an attachment (outside INLINE_MIMES)
    — so it downloads, never renders inline on the app origin.

    deliver no longer mints a presign, so it can't render active content on a raw
    storage origin at all; the attachment policy is now the download route's job
    (router forces ``attachment`` for non-INLINE mimes). This locks that the
    renderable types deliver produces are exactly the ones that route downloads.
    """
    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler

    from pocketpaw.uploads.config import INLINE_MIMES

    tokens = _bind("wsA", "uA", "sessA")
    try:
        (_agent_cwd() / filename).write_text(content)
        resp = await _deliver_handler({"path": filename})
    finally:
        _detach(tokens)

    body = _body(resp)
    assert body["mime"] == expected_mime
    assert body["url"] == f"/api/v1/uploads/{body['file_id']}"
    assert "expires_in_seconds" not in body
    # Outside INLINE_MIMES → the stable download route serves it as an attachment.
    assert expected_mime not in INLINE_MIMES


async def test_symlink_inside_dir_not_archived(beanie_db) -> None:
    """A symlink INSIDE a delivered directory that points out of the jail is
    skipped — neither the link entry nor its target's bytes reach the zip."""
    import io
    import zipfile

    from pocketpaw_ee.agent.mcp_servers.deliver import _deliver_handler
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    tokens = _bind("wsA", "uA", "sessA")
    try:
        cwd = _agent_cwd()
        secret = Path.home() / "secret_outside.txt"
        secret.write_text("TOPSECRET-LEAK")
        bundle = cwd / "bundle"
        bundle.mkdir()
        (bundle / "ok.txt").write_text("safe content")
        (bundle / "link_to_secret").symlink_to(secret)
        resp = await _deliver_handler({"path": "bundle"})
    finally:
        _detach(tokens)

    body = _body(resp)
    rec = await MongoFileStore().get_scoped(body["file_id"], workspace="wsA")
    data = await _read_blob(rec.storage_key)
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert "bundle/ok.txt" in names
    assert "bundle/link_to_secret" not in names
    assert b"TOPSECRET-LEAK" not in data


async def test_deliver_excluded_from_restricted_surfaces() -> None:
    """The deliver tool id must NOT leak onto restricted surfaces whose profiles
    carry an explicit MCP allowlist (studio/sites/foresight/belt). Locks the
    exclusion so a future allowlist edit can't silently expose deliver there."""
    from pocketpaw_ee.agent.mcp_servers.deliver import DELIVER_ARTIFACT_TOOL_ID
    from pocketpaw_ee.cloud.surface.surface_registry import _mcp_tool_ids

    ids = _mcp_tool_ids()
    assert ids.loaded, "surface MCP tool-ids failed to load — test would be vacuous"
    for allow in (ids.studio_allow, ids.sites_allow, ids.foresight_allow, ids.belt_allow):
        assert allow is not None
        assert DELIVER_ARTIFACT_TOOL_ID not in allow


async def test_build_deliver_server_gated_on_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_deliver_server is cloud-only — None off multi-tenant cloud, a real
    server once the cloud DB signal is set."""
    import pocketpaw_ee.cloud.shared.db as db
    from pocketpaw_ee.agent.mcp_servers import deliver

    monkeypatch.setattr(db, "_client", None)
    assert deliver.build_deliver_server() is None

    monkeypatch.setattr(db, "_client", object())  # fake "cloud DB initialized"
    built = deliver.build_deliver_server()
    assert built is not None
    assert built[0] == "pocketpaw_deliver"
