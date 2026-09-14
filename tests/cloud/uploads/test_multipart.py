# tests/cloud/uploads/test_multipart.py — the resumable upload endpoints.
# Created: 2026-09-14 (feat/uploads-multipart-endpoints).
#
# One file for all five routes, covering the things that would actually hurt:
#
#   * a multipart upload is INDISTINGUISHABLE from a simple one downstream.
#     ``complete`` cannot call ``EEUploadService.upload_many`` (that takes
#     UploadFile objects and does the write itself; here the bytes are already
#     in the bucket), so it replicates its post-write sequence. Replication
#     drifts — miss the emit and the file never reaches the KB indexer, miss
#     save_scoped and it never appears in the library.
#   * BOTH ceilings, at BOTH ends. At init against the declared size, which is
#     the whole point of the feature. At complete against the size storage
#     reports, because the declared size is client-supplied and a client that
#     under-declares must still be caught and its object deleted.
#   * an upload session is a tenant boundary. Another workspace's session is a
#     404, never a 403 that would confirm the id exists.
#   * the daily budget claim is reserved at init and given back on abort and
#     expiry, charged to the day it was CLAIMED on.
#
# Mutations: tests/mutations/uploads_multipart_api.json.

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from pocketpaw.uploads.adapter import StoredObject
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound as UploadNotFound
from pocketpaw.uploads.errors import StorageFailure

MIB = 1024 * 1024
WS = "w1"
USER = "u1"


class FakeAdapter:
    """In-memory storage adapter with a steerable multipart surface.

    A fake rather than ``LocalStorageAdapter`` because local disk answers
    ``supports_presigned_parts() -> False`` by design, which makes the
    presigned mode — the cloud path, and the default — unreachable through it.
    The three ``fail_*`` / ``report_size`` switches reproduce the provider
    behaviours the service has to survive.
    """

    def __init__(self, *, presigned: bool = True) -> None:
        self.presigned = presigned
        self.objects: dict[str, bytes] = {}
        self.parts: dict[tuple[str, str], dict[int, bytes]] = {}
        self.aborted: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        #: Raise the post-complete HEAD failure: the object completed and then
        #: could not be described.
        self.fail_head_after_complete = False
        #: Bytes ``complete_multipart`` reports. Stands in for a client that
        #: under- or over-declared its size at init.
        self.report_size: int | None = None

    def supports_presigned_parts(self) -> bool:
        return self.presigned

    async def create_multipart(self, key: str, mime: str) -> str:
        upload_id = f"provider-{len(self.parts)}"
        self.parts[(key, upload_id)] = {}
        return upload_id

    async def sign_part(self, key: str, upload_id: str, part_number: int, ttl: int) -> str | None:
        return f"https://bucket.example/{key}?part={part_number}" if self.presigned else None

    async def put_part(self, key: str, upload_id: str, part_number: int, body: bytes) -> str:
        self.parts.setdefault((key, upload_id), {})[part_number] = body
        return hashlib.md5(body, usedforsecurity=False).hexdigest()

    async def list_parts(self, key: str, upload_id: str) -> list[tuple[int, str]]:
        if (key, upload_id) not in self.parts:
            raise UploadNotFound(f"unknown multipart upload: {upload_id}")
        stored = self.parts[(key, upload_id)]
        # Quoted, like S3 — so the etag comparison is exercised against the
        # decoration a real provider adds rather than a bare hex string.
        return sorted(
            (n, f'"{hashlib.md5(b, usedforsecurity=False).hexdigest()}"') for n, b in stored.items()
        )

    def seed_part(self, key: str, upload_id: str, number: int, body: bytes) -> None:
        """Put a part into storage WITHOUT the service observing it.

        Stands in for a presigned PUT that went browser→bucket, and for a part
        written by a session whose client has since been discarded.
        """
        self.parts.setdefault((key, upload_id), {})[number] = body

    async def complete_multipart(self, key, upload_id, parts) -> StoredObject:
        stored = self.parts.get((key, upload_id), {})
        self.objects[key] = b"".join(stored[n] for n in sorted(stored))
        if self.fail_head_after_complete:
            # Byte-for-byte the shape S3StorageAdapter raises.
            raise StorageFailure("head_object after complete failed: 403")
        size = self.report_size if self.report_size is not None else len(self.objects[key])
        return StoredObject(key=key, size=size, mime="video/quicktime")

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))
        self.parts.pop((key, upload_id), None)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self.objects

    def local_path(self, key: str) -> Path | None:
        return None


def _build(monkeypatch, adapter, *, cfg=None, root=None) -> TestClient:
    """App with the EE uploads router and ``adapter`` installed over ``_MPU``.

    ``_SVC`` is deliberately left alone: nothing in the multipart path calls
    it, and replacing it would hide a mistake if something did.
    """
    import pocketpaw_ee.cloud.uploads.router as uploads_module
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.multipart_service import EEMultipartService

    from tests.cloud.uploads.conftest import install_workspace_caller

    test_cfg = cfg or UploadSettings(local_root=root or Path("/tmp/pocketpaw-test"))
    monkeypatch.setattr(
        uploads_module,
        "_MPU",
        EEMultipartService(adapter=adapter, meta=MongoFileStore(), cfg=test_cfg),
    )

    app = FastAPI()
    app.dependency_overrides[require_license] = lambda: None

    async def _user(x_user: str = Header(default=USER)) -> str:
        return x_user

    async def _ws(x_workspace: str = Header(default=WS)) -> str:
        return x_workspace

    app.dependency_overrides[current_user_id] = _user
    app.dependency_overrides[current_workspace_id] = _ws
    install_workspace_caller(app)
    app.include_router(uploads_module.router, prefix="/api/v1")
    return TestClient(app)


def _pin_cloud_gates(monkeypatch, *, storage_limit=None) -> list:
    """Pin the plan cap and the daily budget; record budget traffic.

    Both reach Mongo collections these tests do not register, so without this
    every request dies in a collaborator rather than in the code under test.
    """
    from pocketpaw_ee.cloud.storage import service as storage_service
    from pocketpaw_ee.cloud.uploads import upload_budget

    calls: list = []

    async def _cap(workspace_id, incoming_bytes):
        if storage_limit is None:
            return (False, 0, None)
        return (incoming_bytes > storage_limit, 0, storage_limit)

    async def _spend(workspace_id, files, size_bytes):
        calls.append(("spend", workspace_id, files, size_bytes))
        return (True, "")

    async def _release(workspace_id, day, files, size_bytes):
        calls.append(("release", workspace_id, day, files, size_bytes))

    monkeypatch.setattr(storage_service, "storage_cap_exceeded", _cap)
    monkeypatch.setattr(upload_budget, "try_spend", _spend)
    monkeypatch.setattr(upload_budget, "release", _release)
    return calls


def _init(client, *, size, **extra):
    body = {"filename": "raw.mov", "size": size, "mime": "video/quicktime", **extra}
    return client.post("/api/v1/uploads/multipart", json=body)


def _complete(client, upload_id, parts=None):
    """Complete. Sends no ``parts`` by default — the manifest comes from storage,
    and the common client (a resumed one) has no etags to offer."""
    body = {} if parts is None else {"parts": parts}
    return client.post(f"/api/v1/uploads/multipart/{upload_id}/complete", json=body)


@pytest.fixture()
def presigned(monkeypatch, beanie_upload_db, tmp_path):
    """Cloud-shaped app: the adapter can presign, so bytes never pass through."""
    adapter = FakeAdapter(presigned=True)
    calls = _pin_cloud_gates(monkeypatch)
    return _build(monkeypatch, adapter, root=tmp_path), adapter, calls


@pytest.fixture()
def relay(monkeypatch, beanie_upload_db, tmp_path):
    """Desktop/dev-shaped app: parts relay through the API."""
    adapter = FakeAdapter(presigned=False)
    calls = _pin_cloud_gates(monkeypatch)
    return _build(monkeypatch, adapter, root=tmp_path), adapter, calls


def _upload_one_part(client, *, declared, body: bytes, **extra) -> str:
    """Init a one-part session and relay its single part."""
    upload_id = _init(client, size=declared, **extra).json()["upload_id"]
    client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/1", content=body)
    return upload_id


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_init_returns_the_contract_shape(presigned):
    client, adapter, _ = presigned
    r = _init(client, size=64 * MIB)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["upload_id"].startswith("mpu_")
    assert body["mode"] == "presigned"
    assert body["part_size"] == 8 * MIB
    assert body["part_count"] == 8
    assert [p["part_number"] for p in body["parts"]] == list(range(1, 9))
    # The id we hand back is OURS. A client that ever echoes the provider's id
    # must not be able to address anything.
    assert body["upload_id"] not in {u for _k, u in adapter.parts}


def test_relay_mode_is_reported_when_the_adapter_cannot_presign(relay):
    """The client does not choose — init reports what storage can actually do."""
    client, _adapter, _ = relay
    body = _init(client, size=16 * MIB).json()
    assert body["mode"] == "relay"
    assert body["parts"] == []


async def test_round_trip_lands_a_row_and_a_file_ready_event(relay, recording_bus):
    """init → part → complete produces exactly what a simple upload produces.

    The FileReady key set is asserted whole, not sampled: this is the payload
    the KB indexer and the timeline broadcast read, and a key missing here is
    a file that silently never becomes searchable.
    """
    client, _adapter, _ = relay
    body = b"x" * 4096
    upload_id = _upload_one_part(client, declared=len(body), body=body, chat_id="g1")

    r = _complete(client, upload_id)
    assert r.status_code == 200, r.text
    out = r.json()

    # Exactly the shape ``POST /uploads`` puts in ``uploaded[]`` — no more keys
    # and no fewer, so ``rowToUnifiedFile`` works on both without a branch.
    assert set(out) == {"id", "filename", "mime", "size", "url", "created"}
    assert out["filename"] == "raw.mov"
    assert out["size"] == len(body)

    from pocketpaw_ee.cloud.uploads.models import FileUpload

    row = await FileUpload.find_one(FileUpload.file_id == out["id"])
    assert row is not None, "save_scoped was skipped — the file is invisible in the library"
    assert row.workspace == WS

    ready = [e for e in recording_bus.events if e.type == "file.ready"]
    assert ready, "no FileReady — the file never reaches the KB indexer"
    data = ready[-1].data
    assert set(data) == {
        "workspace_id",
        "file_id",
        "filename",
        "mime",
        "size",
        "storage_key",
        "url",
        "group_id",
    }
    assert data["file_id"] == out["id"]
    # ``group_id`` only when chat-scoped, like upload_many.
    assert data["group_id"] == "g1"


async def test_file_ready_omits_group_id_when_not_chat_scoped(relay, recording_bus):
    """A workspace-only upload (avatars, KB files) carries no ``group_id``.

    The conditional is what keeps the timeline broadcast from firing for a file
    that belongs to no chat. Without this case the test above passes against a
    version that sets the key unconditionally, because its own upload IS
    chat-scoped.
    """
    client, _adapter, _ = relay
    upload_id = _upload_one_part(client, declared=16, body=b"0123456789abcdef")
    _complete(client, upload_id)

    data = [e for e in recording_bus.events if e.type == "file.ready"][-1].data
    assert "group_id" not in data
    assert "pocket_id" not in data


def test_completing_twice_is_a_404(relay):
    """A replayed complete must not mint a second row for one upload."""
    client, _adapter, _ = relay
    upload_id = _upload_one_part(client, declared=16, body=b"0123456789abcdef")
    assert _complete(client, upload_id).status_code == 200
    second = _complete(client, upload_id)
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "multipart.not_found"


# ---------------------------------------------------------------------------
# The manifest — read from storage, not from the client
# ---------------------------------------------------------------------------


async def test_a_resumed_client_completes_with_no_parts_at_all(
    monkeypatch, beanie_upload_db, tmp_path
):
    """The case the first design could not express, end to end on real bytes.

    Init, upload the parts, throw away every scrap of client state, then
    complete with an empty body. A reloaded client has no etags for what its
    previous session sent — and on the presigned path this side never saw them
    either — so a complete that demanded them was unsatisfiable after exactly
    the event resume exists for.

    Uses the real ``LocalStorageAdapter`` rather than the fake: the assertion is
    that the assembled object is byte-identical to the original, which is only
    worth anything if something actually concatenated bytes.

    Mutation that breaks this: complete from the request body instead of from
    ``list_parts``.
    """
    from pocketpaw.uploads.local import LocalStorageAdapter

    root = tmp_path / "store"
    root.mkdir()
    adapter = LocalStorageAdapter(root=root)
    _pin_cloud_gates(monkeypatch)
    client = _build(monkeypatch, adapter, root=root)

    original = bytes(range(256)) * 40_000  # ~10 MiB, so it spans two parts
    opened = _init(client, size=len(original)).json()
    upload_id, part_size, key = opened["upload_id"], opened["part_size"], opened["key"]
    assert opened["part_count"] == 2

    for i in range(opened["part_count"]):
        chunk = original[i * part_size : (i + 1) * part_size]
        r = client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/{i + 1}", content=chunk)
        assert r.status_code == 200, r.text

    # Everything the client knew is gone except the upload id it persisted.
    del opened, part_size

    out = _complete(client, upload_id)
    assert out.status_code == 200, out.text
    assert out.json()["size"] == len(original)
    assert (root / key).read_bytes() == original, "the resumed upload did not round-trip"


def test_an_advisory_parts_list_that_agrees_with_storage_is_accepted(relay):
    """A client that DOES know its etags may send them, and they are checked.

    Out of order and repeated entries are both fine: a client collecting from
    parallel workers has no reason to have sorted, and a retried request may
    repeat one.
    """
    client, _adapter, _ = relay
    upload_id = _init(client, size=24 * MIB).json()["upload_id"]
    etags = {}
    for n, ch in ((1, b"a"), (2, b"b"), (3, b"c")):
        r = client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/{n}", content=ch * (8 * MIB))
        etags[n] = r.json()["etag"]

    r = _complete(
        client,
        upload_id,
        [
            {"part_number": 3, "etag": etags[3]},
            {"part_number": 1, "etag": etags[1]},
            {"part_number": 2, "etag": etags[2]},
            {"part_number": 1, "etag": etags[1]},
        ],
    )
    assert r.status_code == 200, r.text


def test_an_advisory_etag_that_disagrees_with_storage_is_a_409(relay):
    """The two sides are looking at different bytes. Completing anyway would
    assemble an object the client never intended."""
    client, _adapter, _ = relay
    upload_id = _upload_one_part(client, declared=16, body=b"0123456789abcdef")
    r = _complete(client, upload_id, [{"part_number": 1, "etag": "deadbeef"}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "multipart.invalid"


def test_a_partial_advisory_list_is_fine(relay):
    """Knowing SOME etags is the half-resumed case, not an error."""
    client, _adapter, _ = relay
    upload_id = _init(client, size=24 * MIB).json()["upload_id"]
    for n, ch in ((1, b"a"), (2, b"b"), (3, b"c")):
        r = client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/{n}", content=ch * (8 * MIB))
        last = r.json()["etag"]

    assert _complete(client, upload_id, [{"part_number": 3, "etag": last}]).status_code == 200


def test_a_quoted_etag_matches_an_unquoted_one(relay):
    """S3 wraps etags in literal quotes and clients echo them inconsistently.
    Losing a 5 GB upload over two quotation marks would be a poor trade."""
    client, _adapter, _ = relay
    upload_id = _upload_one_part(client, declared=16, body=b"0123456789abcdef")
    stored = client.get(f"/api/v1/uploads/multipart/{upload_id}").json()
    assert stored["received"] == [1]

    raw = hashlib.md5(b"0123456789abcdef", usedforsecurity=False).hexdigest()
    assert _complete(client, upload_id, [{"part_number": 1, "etag": f'"{raw}"'}]).status_code == 200


def test_a_self_contradicting_client_list_is_a_400(relay):
    """Two etags for one part number, before storage is even consulted."""
    client, _adapter, _ = relay
    upload_id = _upload_one_part(client, declared=16, body=b"0123456789abcdef")
    r = _complete(
        client,
        upload_id,
        [{"part_number": 1, "etag": "a"}, {"part_number": 1, "etag": "b"}],
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "multipart.invalid"


def test_a_part_missing_from_storage_is_a_409(relay):
    """The request is well-formed; the upload just is not finished.

    S3 would happily assemble what it has, which is how a short file lands in a
    library looking plausible. 409 rather than 400 because nothing is wrong with
    the request — the client should upload the rest and try again.

    Mutation that breaks this: drop the contiguity check in ``_resolve_parts``.
    """
    client, _adapter, _ = relay
    upload_id = _init(client, size=24 * MIB).json()["upload_id"]
    for n in (1, 3):
        client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/{n}", content=b"x" * (8 * MIB))

    r = _complete(client, upload_id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "multipart.invalid"
    assert "2" in r.json()["error"]["message"]


async def test_parts_the_service_never_saw_still_complete(presigned):
    """The presigned path: bytes go browser→bucket and we observe nothing.

    ``received`` is empty because no part passed through the relay route, and
    the upload completes anyway because storage was asked.
    """
    client, adapter, _ = presigned
    opened = _init(client, size=16 * MIB).json()
    upload_id, key = opened["upload_id"], opened["key"]
    provider_id = next(u for k, u in adapter.parts if k == key)

    assert client.get(f"/api/v1/uploads/multipart/{upload_id}").json()["received"] == []
    for n in (1, 2):
        adapter.seed_part(key, provider_id, n, b"p" * (8 * MIB))

    assert _complete(client, upload_id).status_code == 200


def test_an_upload_storage_has_lost_is_a_404(presigned):
    """Expired or aborted at the bucket, with our session still open."""
    client, adapter, _ = presigned
    opened = _init(client, size=16 * MIB).json()
    provider_id = next(u for k, u in adapter.parts if k == opened["key"])
    adapter.parts.pop((opened["key"], provider_id))

    r = _complete(client, opened["upload_id"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "multipart.not_found"


# ---------------------------------------------------------------------------
# Both ceilings, at both ends
# ---------------------------------------------------------------------------


def test_init_refuses_an_over_cap_size_before_any_part(monkeypatch, beanie_upload_db, tmp_path):
    """The bug this sprint exists to kill: refuse at init, not after 5 GB.

    Mutation that breaks this: move the ceiling check out of ``init`` — the
    request succeeds and the user transfers the file before being told no.
    """
    adapter = FakeAdapter()
    _pin_cloud_gates(monkeypatch)
    cfg = UploadSettings(local_root=tmp_path, max_large_file_bytes=100 * MIB)
    client = _build(monkeypatch, adapter, cfg=cfg)

    r = _init(client, size=200 * MIB)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "multipart.too_large"
    # Nothing was opened at the provider, so there is nothing to clean up.
    assert adapter.parts == {}


def test_init_refuses_when_the_plan_storage_cap_would_be_exceeded(
    monkeypatch, beanie_upload_db, tmp_path
):
    adapter = FakeAdapter()
    _pin_cloud_gates(monkeypatch, storage_limit=32 * MIB)
    client = _build(monkeypatch, adapter, root=tmp_path)

    r = _init(client, size=64 * MIB)
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "billing.storage_limit"
    assert adapter.parts == {}


async def test_an_under_declared_size_is_caught_at_complete_and_rolled_back(
    monkeypatch, beanie_upload_db, recording_bus, tmp_path
):
    """Declaring 1 MiB and uploading 200 MiB must not land the file.

    The declared size is client-supplied, so the init check alone is not a
    ceiling — this is the one that is. The object is DELETED, no row is
    written, no FileReady fires.

    Mutation that breaks this: check the ceiling only at init, or re-check it
    against ``doc.size`` (the declared figure) instead of what storage reports.
    """
    adapter = FakeAdapter(presigned=False)
    adapter.report_size = 200 * MIB
    _pin_cloud_gates(monkeypatch)
    cfg = UploadSettings(local_root=tmp_path, max_large_file_bytes=100 * MIB)
    client = _build(monkeypatch, adapter, cfg=cfg)

    upload_id = _upload_one_part(client, declared=MIB, body=b"z" * 1024)
    r = _complete(client, upload_id)

    assert r.status_code == 413
    assert r.json()["error"]["code"] == "multipart.too_large"
    assert adapter.deleted, "the over-cap object was left in the bucket"

    from pocketpaw_ee.cloud.uploads.models import FileUpload

    assert await FileUpload.find_one(FileUpload.workspace == WS) is None
    assert not [e for e in recording_bus.events if e.type == "file.ready"]


def test_the_size_storage_reports_is_what_lands_on_the_row(monkeypatch, beanie_upload_db, tmp_path):
    """A mis-declared size within the same part geometry is reconciled, not trusted."""
    adapter = FakeAdapter(presigned=False)
    adapter.report_size = MIB
    calls = _pin_cloud_gates(monkeypatch)
    client = _build(monkeypatch, adapter, root=tmp_path)

    upload_id = _upload_one_part(client, declared=8 * MIB, body=b"z" * 1024)
    out = _complete(client, upload_id).json()

    assert out["size"] == MIB
    # The 7 MiB the client claimed and did not use goes back to the budget.
    releases = [c for c in calls if c[0] == "release"]
    assert releases and releases[-1][4] == 7 * MIB


# ---------------------------------------------------------------------------
# The budget claim
# ---------------------------------------------------------------------------


def test_the_claim_is_reserved_at_init_and_released_on_abort(presigned):
    """Reserved before the transfer, given back when the session dies.

    Mutation that breaks this: claim at complete instead (ten parallel 5 GB
    inits then all see headroom only one can use), or never release (an
    abandoned upload eats the workspace's day).
    """
    client, adapter, calls = presigned
    upload_id = _init(client, size=64 * MIB).json()["upload_id"]
    assert ("spend", WS, 1, 64 * MIB) in calls

    assert client.delete(f"/api/v1/uploads/multipart/{upload_id}").status_code == 204
    assert adapter.aborted, "the provider upload was never aborted"
    op, workspace, _day, files, size = calls[-1]
    assert (op, workspace, files, size) == ("release", WS, 1, 64 * MIB)


def test_abort_is_idempotent_and_refunds_only_once(presigned):
    """A retried cancel settles the claim without giving it back twice.

    The retry is not a no-op: ``_discard`` refunds and THEN marks the session
    dead, so a process that died between those steps leaves an aborted session
    still holding its bytes. ``budget_held`` is what keeps the retry from
    minting free quota.
    """
    client, _adapter, calls = presigned
    upload_id = _init(client, size=64 * MIB).json()["upload_id"]

    assert client.delete(f"/api/v1/uploads/multipart/{upload_id}").status_code == 204
    assert client.delete(f"/api/v1/uploads/multipart/{upload_id}").status_code == 204
    assert len([c for c in calls if c[0] == "release"]) == 1


async def test_the_refund_is_charged_to_the_day_the_claim_was_made(presigned):
    """A session opened last week refunds to LAST WEEK's counter.

    The counter is keyed ``{workspace}:{utc_day}`` and a session lives seven
    days, so refunding against "today" would decrement a row the claim was
    never made against and hand out free quota on a day nothing was spent.

    Mutation that breaks this: pass ``upload_budget.today()`` in ``_refund``.
    """
    from pocketpaw_ee.cloud.uploads.multipart_models import MultipartUpload

    client, _adapter, calls = presigned
    upload_id = _init(client, size=64 * MIB).json()["upload_id"]
    doc = await MultipartUpload.find_one(MultipartUpload.upload_id == upload_id)
    doc.budget_day = "2020-01-01"
    await doc.save()

    client.delete(f"/api/v1/uploads/multipart/{upload_id}")
    assert [c for c in calls if c[0] == "release"][-1][2] == "2020-01-01"


# ---------------------------------------------------------------------------
# Gates — tenancy, guests, pockets, expiry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,suffix,kwargs",
    [
        ("get", "", {}),
        ("delete", "", {}),
        ("post", "/complete", {"json": {"parts": [{"part_number": 1, "etag": "a"}]}}),
        ("put", "/parts/1", {"content": b"x"}),
    ],
)
def test_another_workspaces_session_is_a_404(presigned, method, suffix, kwargs):
    """A session is a tenant boundary. Cross-tenant reads 404, never 403 —
    a 403 confirms the id exists, which is itself a disclosure.

    Mutation that breaks this: drop ``workspace`` from the filter in
    ``MultipartSessionStore.get``.
    """
    client, _adapter, _ = presigned
    upload_id = _init(client, size=16 * MIB).json()["upload_id"]

    r = getattr(client, method)(
        f"/api/v1/uploads/multipart/{upload_id}{suffix}",
        headers={"x-user": "intruder", "x-workspace": "w2"},
        **kwargs,
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "multipart.not_found"


def test_guest_is_refused_on_every_multipart_route(presigned, monkeypatch):
    """Guests cannot upload by ANY route — one that forgot the gate is a way
    to upload without an account."""
    from pocketpaw_ee.cloud.auth import guest_budget

    client, _adapter, _ = presigned
    upload_id = _init(client, size=16 * MIB).json()["upload_id"]

    async def _load_guest(user_id):
        return SimpleNamespace(id=user_id) if user_id == "guest1" else None

    monkeypatch.setattr(guest_budget, "load_guest", _load_guest)
    h = {"x-user": "guest1", "x-workspace": WS}

    responses = [
        client.post("/api/v1/uploads/multipart", json={"filename": "a", "size": 1}, headers=h),
        client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/1", content=b"x", headers=h),
        client.post(
            f"/api/v1/uploads/multipart/{upload_id}/complete", json={"parts": []}, headers=h
        ),
        client.get(f"/api/v1/uploads/multipart/{upload_id}", headers=h),
        client.delete(f"/api/v1/uploads/multipart/{upload_id}", headers=h),
    ]
    for r in responses:
        assert r.status_code == 403, r.text
        # Top-level ``code`` beside the envelope — the frozen contract the
        # frontend's signup prompt keys on.
        assert r.json()["code"] == "guest_upload_forbidden"


@pytest.mark.parametrize("outcome", ["denied", "raises"])
def test_pocket_access_is_enforced_at_init_and_rechecked_at_complete(
    monkeypatch, beanie_upload_db, tmp_path, outcome
):
    """Non-members are refused, and a lookup failure counts as refusal.

    Re-checked at complete, not only at init: a session lives seven days, and
    someone removed from a pocket in that window must not be able to land a
    file in it with a request they prepared while they still had access.

    Mutation that breaks this: default ``allowed = True`` on the lookup
    exception, or drop the complete-time re-check.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    adapter = FakeAdapter(presigned=False)
    _pin_cloud_gates(monkeypatch)
    client = _build(monkeypatch, adapter, root=tmp_path)

    async def _allow(**_kw):
        return True

    monkeypatch.setattr(pockets_service, "has_edit_access", _allow)
    upload_id = _upload_one_part(client, declared=16, body=b"0123456789abcdef", pocket_id="PA")

    async def _refuse(**_kw):
        if outcome == "raises":
            raise RuntimeError("Mongo unreachable")
        return False

    monkeypatch.setattr(pockets_service, "has_edit_access", _refuse)

    # Init is refused...
    assert _init(client, size=16, pocket_id="PA").status_code == 403
    # ...and so is the complete of a session opened while access was granted.
    r = _complete(client, upload_id)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "files.pocket_forbidden"


async def test_an_expired_session_is_409_and_gives_its_claim_back(presigned):
    """409 rather than 404: a client that reloaded a minute late should be told
    its upload timed out, not that it never existed. That is also why the TTL
    index carries a day of grace instead of reaping at ``expires_at``."""
    from pocketpaw_ee.cloud.uploads.multipart_models import MultipartUpload

    client, _adapter, calls = presigned
    upload_id = _init(client, size=64 * MIB).json()["upload_id"]
    doc = await MultipartUpload.find_one(MultipartUpload.upload_id == upload_id)
    doc.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await doc.save()

    r = client.get(f"/api/v1/uploads/multipart/{upload_id}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "multipart.expired"
    assert [c for c in calls if c[0] == "release"]


# ---------------------------------------------------------------------------
# Status / resume, relay parts, and the HEAD recovery
# ---------------------------------------------------------------------------


def test_status_reports_what_arrived_and_re_mints_only_the_gaps(relay):
    """Resume costs the missing parts, not the whole file."""
    client, _adapter, _ = relay
    upload_id = _init(client, size=24 * MIB).json()["upload_id"]
    client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/1", content=b"a" * (8 * MIB))
    client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/3", content=b"c" * (8 * MIB))

    body = client.get(f"/api/v1/uploads/multipart/{upload_id}").json()
    assert body["received"] == [1, 3]
    assert body["part_count"] == 3


def test_a_re_put_part_replaces_rather_than_duplicates(relay):
    """A retried part must not send storage two entries for one number."""
    client, _adapter, _ = relay
    upload_id = _init(client, size=16 * MIB).json()["upload_id"]
    client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/1", content=b"first")
    client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/1", content=b"second")

    assert client.get(f"/api/v1/uploads/multipart/{upload_id}").json()["received"] == [1]


async def test_an_oversize_part_is_refused_before_the_body_is_read(relay):
    """The contract says an over-sized part is rejected BEFORE the body is read.

    Asserted at the service seam rather than over HTTP, because that is the
    only place the claim is observable: the route hands the service a callable,
    and this proves it is never invoked. A test that only checked the status
    would pass against a version that buffers the whole body first — which is
    the class of bug ``body_limit`` exists to prevent.
    """
    from pocketpaw_ee.cloud._core.errors import PayloadTooLarge
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.multipart_service import EEMultipartService

    client, adapter, _ = relay
    upload_id = _init(client, size=16 * MIB).json()["upload_id"]

    svc = EEMultipartService(
        adapter=adapter, meta=MongoFileStore(), cfg=UploadSettings(local_root=Path("/tmp"))
    )

    async def _must_not_be_called() -> bytes:
        raise AssertionError("the body was read despite an over-sized Content-Length")

    with pytest.raises(PayloadTooLarge) as exc:
        await svc.put_part(
            upload_id=upload_id,
            workspace=WS,
            part_number=1,
            declared_length=8 * MIB + 1,
            read_body=_must_not_be_called,
        )
    assert exc.value.code == "multipart.too_large"


def test_the_relay_route_refuses_a_presigned_session(presigned):
    """A presigned session has URLs; relaying through us would bypass them."""
    client, _adapter, _ = presigned
    upload_id = _init(client, size=16 * MIB).json()["upload_id"]
    r = client.put(f"/api/v1/uploads/multipart/{upload_id}/parts/1", content=b"x")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "multipart.invalid"


async def test_a_failed_head_after_complete_recovers_rather_than_500ing(relay):
    """The object completed; only the description failed. Do not fail the upload.

    A 500 here would tell the user a successful multi-gigabyte upload failed,
    and a retry would 404 on the already-consumed id — unrecoverable despite
    having worked. The size comes from the parts we weighed on the way through,
    NOT from the client's declared figure, which is why the two differ here.

    Mutation that breaks this: re-raise instead of recovering, or fall back to
    ``doc.size`` when the measured parts are available.
    """
    client, adapter, _ = relay
    body = b"q" * 2048
    upload_id = _upload_one_part(client, declared=4096, body=body)
    adapter.fail_head_after_complete = True

    r = _complete(client, upload_id)
    assert r.status_code == 200, r.text
    assert r.json()["size"] == len(body), "recovered the declared size, not the measured one"
    assert r.json()["mime"] == "video/quicktime"


def test_the_head_failure_marker_still_matches_the_real_s3_adapter():
    """Guard the string coupling to ``S3StorageAdapter.complete_multipart``.

    The service tells "completed but undescribable" from "completion failed" by
    matching a substring of the adapter's message, because that is the only
    signal the adapter surface gives. That is fragile, so it is pinned: a
    reworded adapter fails this test instead of silently turning every
    recoverable HEAD failure back into a 500.
    """
    import inspect

    from pocketpaw_ee.cloud.uploads.multipart_service import _HEAD_FAILURE_MARKER

    from pocketpaw.uploads.s3 import S3StorageAdapter

    source = inspect.getsource(S3StorageAdapter.complete_multipart)
    assert _HEAD_FAILURE_MARKER in source, (
        "S3StorageAdapter.complete_multipart no longer raises a message containing "
        f"{_HEAD_FAILURE_MARKER!r} — multipart_service can no longer tell a "
        "recoverable HEAD failure from a real completion failure"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"filename": "", "size": 10, "mime": "video/quicktime"},
        {"filename": "a.mov", "size": -1, "mime": "video/quicktime"},
        {"filename": "a.mov", "size": "10", "mime": "video/quicktime"},
        # ``bool`` passes ``isinstance(int)``, so without an explicit rejection
        # ``true`` would open a one-byte session.
        {"filename": "a.mov", "size": True, "mime": "video/quicktime"},
    ],
)
def test_a_malformed_init_body_is_refused(presigned, body):
    client, _adapter, _ = presigned
    r = client.post("/api/v1/uploads/multipart", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "multipart.invalid"
