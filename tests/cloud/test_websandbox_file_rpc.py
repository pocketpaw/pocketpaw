# test_websandbox_file_rpc.py — unit tests for the Web Cursor file read/write/list
# RPC slice (WC-4a). Created 2026-07-15 (feat/websandbox-file-rpc).
#
# Two tiers, no real Daytona:
#   1. FileRpc helper driven directly with a FAKE DaytonaClient (records
#      upload_bytes, returns canned download_file bytes + list_files). Covers the
#      list/read/write happy paths, the path-jail (the security core), honest
#      missing-file errors, the size cap, and malformed/unknown frames.
#   2. The ws.py frame dispatch driven with the WC-3 FakeWebSocket harness on REAL
#      Beanie over mongomock (the ``mongo_db`` fixture) with a real seeded owner,
#      proving a file.write frame reaches upload_bytes with the JAILED path and
#      the socket gets file.write.ok — and a traversal frame returns file.error
#      and never writes.
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import pytest
from fastapi import WebSocketDisconnect
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox import ws as terminal_ws
from pocketpaw_ee.cloud.websandbox.dto import CreateSandboxRequest
from pocketpaw_ee.cloud.websandbox.files import FileRpc, FileRpcError
from pocketpaw_ee.cloud.websandbox.ws import terminal_websocket_endpoint

# The pinned in-VM workspace dir (WEBSANDBOX_WORKDIR). ws.py builds FileRpc
# without an explicit project_dir, so it uses this; Tier-1 tests inject it too.
PROJECT_DIR = "/home/daytona"


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeFileInfo:
    name: str
    is_dir: bool = False
    size: int = 0


@dataclass
class _FakeFsClient:
    """Fake DaytonaClient exposing only the file methods FileRpc uses.

    Records every upload and every path it was asked to list/download so a test
    can assert the JAILED absolute path — and that a rejected traversal never
    reaches download/upload at all.
    """

    project_dir: str = PROJECT_DIR
    files: dict[str, bytes] = field(default_factory=dict)  # abs path -> bytes
    listing: list[_FakeFileInfo] = field(default_factory=list)
    uploads: list[tuple[str, bytes]] = field(default_factory=list)
    list_paths: list[str] = field(default_factory=list)
    download_paths: list[str] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)
    deletes: list[tuple[str, bool]] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)

    async def get_project_dir(self, sandbox_id):  # noqa: ANN001
        return self.project_dir

    async def list_files(self, sandbox_id, path="."):  # noqa: ANN001
        self.list_paths.append(path)
        return list(self.listing)

    async def download_file(self, sandbox_id, remote_path):  # noqa: ANN001
        self.download_paths.append(remote_path)
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        return self.files[remote_path]

    async def upload_bytes(self, sandbox_id, data, remote_path):  # noqa: ANN001
        self.uploads.append((remote_path, data))
        self.files[remote_path] = data

    async def create_folder(self, sandbox_id, path, mode="755"):  # noqa: ANN001
        self.created_dirs.append(path)

    async def delete_file(self, sandbox_id, path, recursive=False):  # noqa: ANN001
        self.deletes.append((path, recursive))
        self.files.pop(path, None)

    async def move_file(self, sandbox_id, src, dst):  # noqa: ANN001
        self.moves.append((src, dst))
        if src in self.files:
            self.files[dst] = self.files.pop(src)


def _rpc(client: _FakeFsClient) -> FileRpc:
    # Inject the fake's project dir so path assertions are independent of the
    # WEBSANDBOX_WORKDIR default (covered separately below).
    return FileRpc(client, "dtn-1", project_dir=client.project_dir)


async def test_root_defaults_to_websandbox_workdir() -> None:
    # With no injected project_dir, the jail root is the pinned in-VM workspace
    # (/home/daytona) — NOT the SDK's get_project_dir() (which returns /root).
    from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

    client = _FakeFsClient(listing=[_FakeFileInfo("README.md", size=1)])
    rpc = FileRpc(client, "dtn-1")
    await rpc.list_dir("")
    assert client.list_paths == [WEBSANDBOX_WORKDIR]


# ---------------------------------------------------------------------------
# Tier 1 — FileRpc helper (happy paths).
# ---------------------------------------------------------------------------


async def test_list_returns_entries_with_relative_paths() -> None:
    client = _FakeFsClient(
        listing=[
            _FakeFileInfo("app.py", is_dir=False, size=42),
            _FakeFileInfo("lib", is_dir=True, size=0),
        ]
    )
    entries = await _rpc(client).list_dir("src")

    # Listed the JAILED absolute path.
    assert client.list_paths == [f"{PROJECT_DIR}/src"]
    assert entries == [
        {"name": "app.py", "path": "src/app.py", "isDir": False, "size": 42},
        {"name": "lib", "path": "src/lib", "isDir": True, "size": 0},
    ]


async def test_list_root_uses_project_dir() -> None:
    client = _FakeFsClient(listing=[_FakeFileInfo("README.md", size=1)])
    entries = await _rpc(client).list_dir(".")
    assert client.list_paths == [PROJECT_DIR]
    assert entries[0]["path"] == "README.md"


async def test_list_empty_path_lists_project_root() -> None:
    # Regression: the editor's file tree lists the repo root by sending path ''
    # (frontend loadTree -> listDir('')). An empty path MUST resolve to the
    # project dir, not raise — otherwise the tree comes back empty even though
    # the repo cloned fine. (Diagnosed live 2026-07-15: clone lands in the
    # project dir, but list('') was rejected.)
    client = _FakeFsClient(listing=[_FakeFileInfo("README.md", size=1)])
    entries = await _rpc(client).list_dir("")
    assert client.list_paths == [PROJECT_DIR]
    assert entries[0]["path"] == "README.md"


async def test_read_returns_file_content() -> None:
    client = _FakeFsClient(files={f"{PROJECT_DIR}/src/app.py": b"print('hi')\n"})
    content = await _rpc(client).read_file("src/app.py")
    assert content == "print('hi')\n"
    assert client.download_paths == [f"{PROJECT_DIR}/src/app.py"]


async def test_write_calls_upload_with_jailed_path() -> None:
    client = _FakeFsClient()
    await _rpc(client).write_file("src/new.py", "x = 1\n")
    assert client.uploads == [(f"{PROJECT_DIR}/src/new.py", b"x = 1\n")]


async def test_write_then_read_round_trips() -> None:
    client = _FakeFsClient()
    rpc = _rpc(client)
    await rpc.write_file("notes.txt", "hello world")
    assert await rpc.read_file("notes.txt") == "hello world"


# ---------------------------------------------------------------------------
# Tier 1 — path jail (the must-pass security core).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "../../etc/passwd",
        "../secret.txt",
        "/etc/passwd",
        "src/../../../../etc/shadow",
        "..",
    ],
)
async def test_read_traversal_is_rejected_and_never_downloads(bad_path) -> None:  # noqa: ANN001
    client = _FakeFsClient()
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).read_file(bad_path)
    assert ei.value.op == "read"
    # The jail rejected the path BEFORE any download was attempted.
    assert client.download_paths == []


@pytest.mark.parametrize(
    "bad_path",
    ["../../etc/passwd", "../escape.txt", "/tmp/evil", ".."],
)
async def test_write_traversal_is_rejected_and_never_uploads(bad_path) -> None:  # noqa: ANN001
    client = _FakeFsClient()
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).write_file(bad_path, "pwned")
    assert ei.value.op == "write"
    assert client.uploads == []


async def test_list_traversal_is_rejected() -> None:
    client = _FakeFsClient(listing=[_FakeFileInfo("x")])
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).list_dir("../..")
    assert ei.value.op == "list"
    assert client.list_paths == []


async def test_benign_dotdot_that_stays_inside_is_allowed() -> None:
    # foo/../bar normalizes to bar — inside the jail, so it's allowed.
    client = _FakeFsClient(files={f"{PROJECT_DIR}/bar.txt": b"ok"})
    assert await _rpc(client).read_file("foo/../bar.txt") == "ok"


# ---------------------------------------------------------------------------
# Tier 1 — honest errors + size cap.
# ---------------------------------------------------------------------------


async def test_read_missing_file_is_honest_error_not_empty() -> None:
    client = _FakeFsClient()  # no files
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).read_file("does/not/exist.py")
    assert ei.value.op == "read"
    assert "no such file" in ei.value.message


async def test_read_binary_file_is_rejected() -> None:
    client = _FakeFsClient(files={f"{PROJECT_DIR}/img.png": b"\xff\xfe\x00\x01\x80"})
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).read_file("img.png")
    assert "UTF-8" in ei.value.message


async def test_write_over_cap_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_MAX_FILE_KB", "1")
    client = _FakeFsClient()
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).write_file("big.txt", "A" * 2048)  # 2 KB > 1 KB cap
    assert ei.value.op == "write"
    assert client.uploads == []


async def test_read_over_cap_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_MAX_FILE_KB", "1")
    client = _FakeFsClient(files={f"{PROJECT_DIR}/big.txt": b"B" * 2048})
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).read_file("big.txt")
    assert ei.value.op == "read"


# ---------------------------------------------------------------------------
# Tier 1 — dispatch: frame shaping + malformed frames don't tear down.
# ---------------------------------------------------------------------------


async def test_dispatch_list_ok_frame() -> None:
    client = _FakeFsClient(listing=[_FakeFileInfo("a.py", size=3)])
    resp = await _rpc(client).dispatch({"type": "file.list", "reqId": "r1", "path": "."})
    assert resp == {
        "type": "file.list.ok",
        "reqId": "r1",
        "path": ".",
        "entries": [{"name": "a.py", "path": "a.py", "isDir": False, "size": 3}],
    }


async def test_dispatch_read_ok_frame() -> None:
    client = _FakeFsClient(files={f"{PROJECT_DIR}/a.py": b"hi"})
    resp = await _rpc(client).dispatch({"type": "file.read", "reqId": "r2", "path": "a.py"})
    assert resp == {"type": "file.read.ok", "reqId": "r2", "path": "a.py", "content": "hi"}


async def test_dispatch_write_ok_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch(
        {"type": "file.write", "reqId": "r3", "path": "a.py", "content": "z = 2\n"}
    )
    assert resp == {"type": "file.write.ok", "reqId": "r3", "path": "a.py"}
    assert client.uploads == [(f"{PROJECT_DIR}/a.py", b"z = 2\n")]


async def test_dispatch_traversal_returns_error_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch(
        {"type": "file.write", "reqId": "r4", "path": "../../etc/passwd", "content": "x"}
    )
    assert resp["type"] == "file.error"
    assert resp["reqId"] == "r4"
    assert resp["op"] == "write"
    assert client.uploads == []


async def test_dispatch_unknown_file_op_returns_error_not_raise() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch({"type": "file.rename", "reqId": "r5", "path": "a"})
    assert resp["type"] == "file.error"
    assert resp["op"] == "rename"


async def test_dispatch_non_file_frame_returns_none() -> None:
    client = _FakeFsClient()
    assert await _rpc(client).dispatch({"type": "input", "data": "ls\n"}) is None
    assert await _rpc(client).dispatch({"type": "ping"}) is None


async def test_dispatch_missing_path_is_error_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch({"type": "file.read", "reqId": "r6"})
    assert resp["type"] == "file.error"
    assert resp["op"] == "read"


# ---------------------------------------------------------------------------
# Write-through mirror hook (CM-2a′).
# ---------------------------------------------------------------------------


async def test_write_invokes_on_write_mirror_with_relpath_and_bytes() -> None:
    client = _FakeFsClient()
    mirrored: list[tuple[str, bytes]] = []

    async def _spy(rel_path, data):  # noqa: ANN001
        mirrored.append((rel_path, data))

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_write=_spy)
    await rpc.write_file("src/app.ts", "hello")

    # The VM write landed, and the mirror got the project-relative path + bytes.
    assert client.uploads == [(f"{PROJECT_DIR}/src/app.ts", b"hello")]
    assert mirrored == [("src/app.ts", b"hello")]


async def test_write_mirror_failure_does_not_fail_the_save() -> None:
    client = _FakeFsClient()

    async def _boom(rel_path, data):  # noqa: ANN001
        raise RuntimeError("boom: blob storage down")

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_write=_boom)
    # Must NOT raise — the VM write already succeeded before the mirror ran.
    await rpc.write_file("a.ts", "x")
    assert client.uploads == [(f"{PROJECT_DIR}/a.ts", b"x")]


# ---------------------------------------------------------------------------
# Tier 1 — create / delete / move ops (WC-4c: file CRUD verbs).
# ---------------------------------------------------------------------------


async def test_create_file_uploads_empty_at_jailed_path() -> None:
    client = _FakeFsClient()  # empty listing -> target does not exist
    await _rpc(client).create("src/new.py", False)
    assert client.uploads == [(f"{PROJECT_DIR}/src/new.py", b"")]
    assert client.created_dirs == []


async def test_create_dir_makes_folder_at_jailed_path() -> None:
    client = _FakeFsClient()
    await _rpc(client).create("src/newdir", True)
    assert client.created_dirs == [f"{PROJECT_DIR}/src/newdir"]
    assert client.uploads == []


async def test_create_refuses_existing_target() -> None:
    # The parent listing already carries the target name -> refuse (no clobber).
    client = _FakeFsClient(listing=[_FakeFileInfo("app.py", size=1)])
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).create("app.py", False)
    assert ei.value.op == "create"
    assert client.uploads == []


async def test_create_root_is_refused() -> None:
    client = _FakeFsClient()
    for bad in ("", ".", "./"):
        with pytest.raises(FileRpcError) as ei:
            await _rpc(client).create(bad, True)
        assert ei.value.op == "create"
    assert client.created_dirs == []
    assert client.uploads == []


async def test_delete_file_calls_delete_recursive() -> None:
    client = _FakeFsClient(files={f"{PROJECT_DIR}/old.py": b"x"})
    await _rpc(client).delete("old.py")
    assert client.deletes == [(f"{PROJECT_DIR}/old.py", True)]


async def test_delete_dir_calls_delete_recursive() -> None:
    client = _FakeFsClient()
    await _rpc(client).delete("src/pkg")
    assert client.deletes == [(f"{PROJECT_DIR}/src/pkg", True)]


async def test_delete_root_is_refused() -> None:
    client = _FakeFsClient()
    for bad in ("", ".", "./"):
        with pytest.raises(FileRpcError) as ei:
            await _rpc(client).delete(bad)
        assert ei.value.op == "delete"
    assert client.deletes == []


async def test_move_renames_at_jailed_paths() -> None:
    client = _FakeFsClient()
    await _rpc(client).move("a.py", "b.py")
    assert client.moves == [(f"{PROJECT_DIR}/a.py", f"{PROJECT_DIR}/b.py")]


async def test_move_refuses_existing_destination() -> None:
    client = _FakeFsClient(listing=[_FakeFileInfo("b.py", size=1)])
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).move("a.py", "b.py")
    assert ei.value.op == "move"
    assert client.moves == []


async def test_move_root_source_is_refused() -> None:
    client = _FakeFsClient()
    for bad in ("", ".", "./"):
        with pytest.raises(FileRpcError) as ei:
            await _rpc(client).move(bad, "b.py")
        assert ei.value.op == "move"
    assert client.moves == []


@pytest.mark.parametrize("bad_src", ["../../etc/passwd", "/etc/passwd", "src/../../evil"])
async def test_move_jails_source_path(bad_src) -> None:  # noqa: ANN001
    client = _FakeFsClient()
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).move(bad_src, "ok.py")
    assert ei.value.op == "move"
    assert client.moves == []


@pytest.mark.parametrize("bad_dst", ["../../etc/passwd", "/etc/passwd", "src/../../evil"])
async def test_move_jails_destination_path(bad_dst) -> None:  # noqa: ANN001
    client = _FakeFsClient()
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).move("ok.py", bad_dst)
    assert ei.value.op == "move"
    assert client.moves == []


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", ".."])
async def test_create_jails_path(bad) -> None:  # noqa: ANN001
    client = _FakeFsClient()
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).create(bad, False)
    assert ei.value.op == "create"
    assert client.uploads == [] and client.created_dirs == []


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", ".."])
async def test_delete_jails_path(bad) -> None:  # noqa: ANN001
    client = _FakeFsClient()
    with pytest.raises(FileRpcError) as ei:
        await _rpc(client).delete(bad)
    assert ei.value.op == "delete"
    assert client.deletes == []


# ---------------------------------------------------------------------------
# Tier 1 — durability hooks (on_delete / on_move) fire with the right rel paths.
# ---------------------------------------------------------------------------


async def test_create_file_mirrors_empty_via_on_write() -> None:
    client = _FakeFsClient()
    mirrored: list[tuple[str, bytes]] = []

    async def _spy(rel_path, data):  # noqa: ANN001
        mirrored.append((rel_path, data))

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_write=_spy)
    await rpc.create("src/new.ts", False)
    # An empty file lands in the overlay just like a write of "".
    assert mirrored == [("src/new.ts", b"")]


async def test_create_dir_does_not_mirror() -> None:
    client = _FakeFsClient()
    mirrored: list = []

    async def _spy(rel_path, data):  # noqa: ANN001
        mirrored.append((rel_path, data))

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_write=_spy)
    await rpc.create("newdir", True)
    assert mirrored == []


async def test_delete_calls_on_delete_with_relpath() -> None:
    client = _FakeFsClient()
    dropped: list[str] = []

    async def _drop(rel_path):  # noqa: ANN001
        dropped.append(rel_path)

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_delete=_drop)
    await rpc.delete("src/old.ts")
    assert dropped == ["src/old.ts"]


async def test_move_calls_on_move_with_rel_paths() -> None:
    client = _FakeFsClient()
    moved: list[tuple[str, str]] = []

    async def _move(src_rel, dst_rel):  # noqa: ANN001
        moved.append((src_rel, dst_rel))

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_move=_move)
    await rpc.move("a.ts", "b.ts")
    assert moved == [("a.ts", "b.ts")]


async def test_delete_hook_failure_does_not_fail_delete() -> None:
    client = _FakeFsClient(files={f"{PROJECT_DIR}/a.ts": b"x"})

    async def _boom(rel_path):  # noqa: ANN001
        raise RuntimeError("overlay drop failed")

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_delete=_boom)
    await rpc.delete("a.ts")  # must not raise — the VM delete already landed
    assert client.deletes == [(f"{PROJECT_DIR}/a.ts", True)]


async def test_move_hook_failure_does_not_fail_move() -> None:
    client = _FakeFsClient()

    async def _boom(src_rel, dst_rel):  # noqa: ANN001
        raise RuntimeError("overlay re-key failed")

    rpc = FileRpc(client, "dtn-1", project_dir=client.project_dir, on_move=_boom)
    await rpc.move("a.ts", "b.ts")  # must not raise
    assert client.moves == [(f"{PROJECT_DIR}/a.ts", f"{PROJECT_DIR}/b.ts")]


# ---------------------------------------------------------------------------
# Tier 1 — dispatch frames for the new verbs.
# ---------------------------------------------------------------------------


async def test_dispatch_create_ok_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch(
        {"type": "file.create", "reqId": "c1", "path": "new.py", "isDir": False}
    )
    assert resp == {"type": "file.create.ok", "reqId": "c1", "path": "new.py", "isDir": False}
    assert client.uploads == [(f"{PROJECT_DIR}/new.py", b"")]


async def test_dispatch_create_dir_ok_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch(
        {"type": "file.create", "reqId": "c2", "path": "pkg", "isDir": True}
    )
    assert resp == {"type": "file.create.ok", "reqId": "c2", "path": "pkg", "isDir": True}
    assert client.created_dirs == [f"{PROJECT_DIR}/pkg"]


async def test_dispatch_delete_ok_frame() -> None:
    client = _FakeFsClient(files={f"{PROJECT_DIR}/old.py": b"x"})
    resp = await _rpc(client).dispatch({"type": "file.delete", "reqId": "d1", "path": "old.py"})
    assert resp == {"type": "file.delete.ok", "reqId": "d1", "path": "old.py"}
    assert client.deletes == [(f"{PROJECT_DIR}/old.py", True)]


async def test_dispatch_move_ok_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch(
        {"type": "file.move", "reqId": "m1", "path": "a.py", "toPath": "b.py"}
    )
    assert resp == {"type": "file.move.ok", "reqId": "m1", "path": "a.py", "toPath": "b.py"}
    assert client.moves == [(f"{PROJECT_DIR}/a.py", f"{PROJECT_DIR}/b.py")]


async def test_dispatch_create_traversal_returns_error_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch(
        {"type": "file.create", "reqId": "c9", "path": "../../etc/x", "isDir": False}
    )
    assert resp["type"] == "file.error"
    assert resp["op"] == "create" and resp["reqId"] == "c9"
    assert client.uploads == []


async def test_dispatch_move_traversal_returns_error_frame() -> None:
    client = _FakeFsClient()
    resp = await _rpc(client).dispatch(
        {"type": "file.move", "reqId": "m9", "path": "a.py", "toPath": "../../etc/x"}
    )
    assert resp["type"] == "file.error"
    assert resp["op"] == "move" and resp["reqId"] == "m9"
    assert client.moves == []


# ---------------------------------------------------------------------------
# Tier 2 — ws.py frame dispatch over the real auth path (mongomock).
#
# Reuses the WC-3 fakes/harness (a pty must still open so the receive loop runs),
# extended with the file methods FileRpc needs.
# ---------------------------------------------------------------------------

pytestmark_mongo = pytest.mark.usefixtures("mongo_db")


class _FakeLicense:
    expired = False


class _FakeHandle:
    def __init__(self, on_data) -> None:  # noqa: ANN001
        self._on_data = on_data
        self.sent: list = []

    async def wait_for_connection(self) -> None:
        return None

    async def send_input(self, data) -> None:  # noqa: ANN001
        self.sent.append(data)

    async def disconnect(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.handle = None

    async def create_pty_session(self, session_id, on_data, cwd=None, envs=None, pty_size=None):  # noqa: ANN001
        self.handle = _FakeHandle(on_data)
        return self.handle


class _FakeSandbox:
    def __init__(self) -> None:
        self.process = _FakeProcess()


@dataclass
class _FakeDaytonaClient:
    """Terminal fake (pty) + the file methods FileRpc calls, in one object."""

    sandbox: _FakeSandbox = field(default_factory=_FakeSandbox)
    kill_calls: list = field(default_factory=list)
    project_dir: str = PROJECT_DIR
    files: dict[str, bytes] = field(default_factory=dict)
    uploads: list[tuple[str, bytes]] = field(default_factory=list)
    listing: list[_FakeFileInfo] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)
    deletes: list[tuple[str, bool]] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)

    async def get_sandbox_instance(self, sandbox_id):  # noqa: ANN001
        return self.sandbox

    async def resize_pty(self, sandbox_id, session_id, cols, rows):  # noqa: ANN001
        return None

    async def kill_pty(self, sandbox_id, session_id):  # noqa: ANN001
        self.kill_calls.append(session_id)

    async def get_project_dir(self, sandbox_id):  # noqa: ANN001
        return self.project_dir

    async def list_files(self, sandbox_id, path="."):  # noqa: ANN001
        # Empty listing by default so create/move existence probes see a clean
        # tree; the read/list round-trip test seeds ``listing`` explicitly.
        if self.listing:
            return list(self.listing)
        return [_FakeFileInfo("app.py", is_dir=False, size=5)]

    async def download_file(self, sandbox_id, remote_path):  # noqa: ANN001
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        return self.files[remote_path]

    async def upload_bytes(self, sandbox_id, data, remote_path):  # noqa: ANN001
        self.uploads.append((remote_path, data))
        self.files[remote_path] = data

    async def create_folder(self, sandbox_id, path, mode="755"):  # noqa: ANN001
        self.created_dirs.append(path)

    async def delete_file(self, sandbox_id, path, recursive=False):  # noqa: ANN001
        self.deletes.append((path, recursive))
        self.files.pop(path, None)

    async def move_file(self, sandbox_id, src, dst):  # noqa: ANN001
        self.moves.append((src, dst))
        if src in self.files:
            self.files[dst] = self.files.pop(src)


class FakeWebSocket:
    def __init__(self, inbound=None) -> None:  # noqa: ANN001
        self._inbound: deque = deque(inbound or [])
        self.accepted = False
        self.closed = None
        self.sent_bytes: list = []
        self.sent_json: list = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code=1000, reason=None) -> None:  # noqa: ANN001
        self.closed = (code, reason)

    async def send_bytes(self, data) -> None:  # noqa: ANN001
        self.sent_bytes.append(data)

    async def send_json(self, data) -> None:  # noqa: ANN001
        self.sent_json.append(data)

    async def receive_text(self) -> str:
        if not self._inbound:
            raise WebSocketDisconnect(1000)
        return self._inbound.popleft()


async def _seed_user(active_workspace) -> str:  # noqa: ANN001
    from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
    from pocketpaw_ee.cloud.models.user import User as UserDoc

    email = f"wc4-{os.urandom(4).hex()}@example.com"
    async for db in get_user_db():
        manager = UserManager(db)
        user = await manager.create(UserCreate(email=email, password="StrongPass123!"))
        doc = await UserDoc.get(user.id)
        doc.active_workspace = active_workspace
        await doc.save()
        return str(user.id)
    raise RuntimeError("user db iterator exhausted")  # pragma: no cover


async def _seed_ready_sandbox(workspace_id, user_id) -> str:  # noqa: ANN001
    view = await sandbox_service.create_sandbox(
        workspace_id,
        user_id,
        CreateSandboxRequest(
            repo="https://github.com/acme/api.git", status="ready", sandbox_id="dtn-1"
        ),
    )
    return view.id


@pytest.fixture
def wire_terminal(monkeypatch):
    client = _FakeDaytonaClient()
    tokens: dict[str, str] = {}

    async def _fake_consume(token: str):
        return tokens.get(token)

    monkeypatch.setattr(terminal_ws, "get_license", lambda: _FakeLicense())
    monkeypatch.setattr(terminal_ws, "consume_ws_ticket", _fake_consume)
    monkeypatch.setattr(terminal_ws, "get_daytona_client", lambda: client)
    monkeypatch.setattr(terminal_ws, "manager", terminal_ws.TerminalConnectionManager())
    return client, tokens


@pytest.mark.usefixtures("mongo_db")
async def test_ws_file_write_frame_reaches_upload_and_acks(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id

    ws = FakeWebSocket(
        inbound=['{"type":"file.write","reqId":"w1","path":"hello.txt","content":"hi there"}']
    )
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    assert ws.accepted is True
    assert client.uploads == [(f"{PROJECT_DIR}/hello.txt", b"hi there")]
    assert {"type": "file.write.ok", "reqId": "w1", "path": "hello.txt"} in ws.sent_json


@pytest.mark.usefixtures("mongo_db")
async def test_ws_file_read_and_list_frames_round_trip(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id
    client.files[f"{PROJECT_DIR}/app.py"] = b"print(1)\n"

    ws = FakeWebSocket(
        inbound=[
            '{"type":"file.list","reqId":"l1","path":"."}',
            '{"type":"file.read","reqId":"r1","path":"app.py"}',
        ]
    )
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    list_ok = [m for m in ws.sent_json if m.get("type") == "file.list.ok"]
    read_ok = [m for m in ws.sent_json if m.get("type") == "file.read.ok"]
    assert list_ok and list_ok[0]["entries"][0]["name"] == "app.py"
    assert read_ok and read_ok[0]["content"] == "print(1)\n"


@pytest.mark.usefixtures("mongo_db")
async def test_ws_file_traversal_frame_is_rejected_and_never_writes(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id

    ws = FakeWebSocket(
        inbound=[
            '{"type":"file.write","reqId":"bad","path":"../../etc/passwd","content":"pwned"}'
        ]
    )
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    assert client.uploads == []
    errs = [m for m in ws.sent_json if m.get("type") == "file.error"]
    assert errs and errs[0]["op"] == "write" and errs[0]["reqId"] == "bad"


@pytest.mark.usefixtures("mongo_db")
async def test_ws_malformed_file_frame_does_not_tear_down_socket(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id
    client.files[f"{PROJECT_DIR}/app.py"] = b"ok"

    ws = FakeWebSocket(
        inbound=[
            "{not json",  # malformed — ignored, socket survives
            '{"type":"file.bogus","reqId":"b1","path":"app.py"}',  # unknown file op
            '{"type":"file.read","reqId":"r9","path":"app.py"}',  # still works after
        ]
    )
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    assert ws.closed is None  # never force-closed
    reads = [m for m in ws.sent_json if m.get("type") == "file.read.ok"]
    assert reads and reads[0]["content"] == "ok"


@pytest.mark.usefixtures("mongo_db")
async def test_ws_file_create_frame_lands_empty_and_acks(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id

    ws = FakeWebSocket(
        inbound=['{"type":"file.create","reqId":"c1","path":"fresh.txt","isDir":false}']
    )
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    # The create frame reached the VM as an empty upload and got its ack. The
    # empty-file overlay mirror is exercised at Tier 1 (on_write spy) — the ws
    # path's real build_uploads() blob write is out of scope here.
    assert {"type": "file.create.ok", "reqId": "c1", "path": "fresh.txt", "isDir": False} in (
        ws.sent_json
    )
    assert client.uploads == [(f"{PROJECT_DIR}/fresh.txt", b"")]


@pytest.mark.usefixtures("mongo_db")
async def test_ws_file_delete_frame_drops_overlay(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id
    # Seed an overlay entry the delete must drop so restore can't resurrect it.
    await sandbox_service.set_overlay_entry("w1", user_id, row_id, "gone.txt", "file-x")

    ws = FakeWebSocket(inbound=['{"type":"file.delete","reqId":"d1","path":"gone.txt"}'])
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    assert {"type": "file.delete.ok", "reqId": "d1", "path": "gone.txt"} in ws.sent_json
    assert client.deletes == [(f"{PROJECT_DIR}/gone.txt", True)]
    row = await sandbox_service.get_sandbox("w1", user_id, row_id)
    assert "gone.txt" not in row.overlay


@pytest.mark.usefixtures("mongo_db")
async def test_ws_file_move_frame_rekeys_overlay(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id
    await sandbox_service.set_overlay_entry("w1", user_id, row_id, "a.txt", "file-a")

    ws = FakeWebSocket(
        inbound=['{"type":"file.move","reqId":"m1","path":"a.txt","toPath":"b.txt"}']
    )
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    assert {
        "type": "file.move.ok",
        "reqId": "m1",
        "path": "a.txt",
        "toPath": "b.txt",
    } in ws.sent_json
    assert client.moves == [(f"{PROJECT_DIR}/a.txt", f"{PROJECT_DIR}/b.txt")]
    # Overlay re-keyed src -> dst so restore replays the file at its new path.
    row = await sandbox_service.get_sandbox("w1", user_id, row_id)
    assert row.overlay == {"b.txt": "file-a"}
