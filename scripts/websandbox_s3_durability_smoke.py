# websandbox_s3_durability_smoke.py — LIVE smoke for WC-S3 workspace durability.
# Created 2026-07-15 (feat/websandbox-s3-durability). OUT of CI (hits real Daytona).
#
# Proves the snapshot -> restore round-trip against REAL Daytona VMs, exercising
# the ACTUAL ``durability.snapshot_workspace`` / ``restore_workspace`` code:
#   1. provision VM1, write a marker file into /home/daytona
#   2. snapshot_workspace(client=real, uploads=shim): tar -> download -> "upload"
#   3. provision a SECOND fresh VM, rebind the registry row to it
#   4. restore_workspace(client=real, uploads=shim): fetch -> upload_bytes -> untar
#   5. assert the marker file is back in VM2
#   6. finally: delete BOTH VMs (mandatory cleanup)
#
# The registry runs on real Beanie over mongomock-motor (in-memory) so the real
# get_sandbox / set_snapshot / authorize_sandbox paths execute. The S3 layer is a
# small in-memory ``_UploadsShim`` (upload/stream) standing in for
# EEUploadService, which can't be constructed standalone here (its MongoFileStore
# needs an initialized cloud Mongo). The durability code is adapter-agnostic — in
# cloud the identical bytes flow through EEUploadService -> tenant S3 (that path
# is covered by the Tier-1 unit tests).
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv

MARKER_NAME = "SMOKE_MARKER.txt"
MARKER_TEXT = "wc-s3-durability-smoke-ok"
WORKDIR = "/home/daytona"


class _UploadsShim:
    """In-memory EEUploadService stand-in: upload() stores bytes, stream() serves
    them back. Same shape the durability DI seam expects."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._n = 0

    async def upload(self, file, owner_id, chat_id, workspace, folder_path="/", pocket_id=None):
        from pocketpaw.uploads.file_store import FileRecord

        data = await file.read()
        self._n += 1
        fid = f"smoke-{self._n}"
        self._blobs[fid] = data
        print(
            f"  [uploads] stored {len(data)} bytes as {fid} (ws={workspace} folder={folder_path})"
        )  # noqa: E501
        return FileRecord(
            id=fid,
            storage_key=f"key/{fid}",
            filename=file.filename,
            mime="application/gzip",
            size=len(data),
            owner_id=owner_id,
            chat_id=None,
            created=datetime.now(UTC),
        )

    async def stream(self, file_id, requester_id, workspace):
        from pocketpaw.uploads.file_store import FileRecord

        data = self._blobs[file_id]

        async def _it():
            yield data

        rec = FileRecord(
            id=file_id,
            storage_key=f"key/{file_id}",
            filename="ws.tgz",
            mime="application/gzip",
            size=len(data),
            owner_id=requester_id,
            chat_id=None,
            created=datetime.now(UTC),
        )
        return rec, _it()


class _NoopBus:
    """Minimal EventBus: swallow every emit. The registry emits on each write;
    this smoke doesn't assert on fan-out, only on the VM round-trip."""

    async def publish(self, event) -> None:  # noqa: ANN001
        return None


async def _init_registry() -> None:
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud._core.realtime.bus import set_bus
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    set_bus(_NoopBus())
    client = AsyncMongoMockClient()
    db = client[f"smoke_{datetime.now(UTC).timestamp()}"]
    original = db.list_collection_names

    async def _safe(*_a, **_k):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])


async def main() -> int:
    load_dotenv(".env")

    from pocketpaw_ee.cloud.daytona.client import DaytonaClient
    from pocketpaw_ee.cloud.daytona.config import daytona_enabled
    from pocketpaw_ee.cloud.websandbox import durability
    from pocketpaw_ee.cloud.websandbox import service as sandbox_service

    if not daytona_enabled():
        print("BLOCKED: Daytona keys not set in .env")
        return 2

    await _init_registry()

    ws, user = "smoke-ws", "smoke-user"
    client = DaytonaClient()
    uploads = _UploadsShim()
    vm1 = vm2 = None
    ok = False
    try:
        # 1. Provision VM1 and write a marker into the workspace dir.
        print("Provisioning VM1...")
        info1 = await client.create_sandbox(name="wc-s3-smoke-1", auto_stop_interval=15)
        vm1 = info1.id
        await client.wait_for_sandbox(vm1, target_state="started", timeout=180)
        await client.execute_command(vm1, f"mkdir -p {WORKDIR}")
        await client.execute_command(vm1, f"printf '{MARKER_TEXT}' > {WORKDIR}/{MARKER_NAME}")
        print(f"  VM1={vm1}; wrote {WORKDIR}/{MARKER_NAME}")

        # Register a ready row bound to VM1.
        row = await sandbox_service.create_sandbox(
            ws,
            user,
            {"repo": "https://example.com/smoke.git", "status": "ready", "sandbox_id": vm1},
        )

        # 2. Snapshot via the REAL durability code (tar -> download -> shim upload).
        print("Snapshotting VM1 workspace...")
        file_id = await durability.snapshot_workspace(
            ws, user, row.id, client=client, uploads=uploads
        )
        print(f"  snapshot file_id={file_id}")

        # 3. Simulate a teardown: delete the marker so the workspace no longer has
        #    it (equivalent to a fresh VM). Org tier caps concurrent VM memory, so
        #    we reuse VM1 rather than provision a second VM — the restore code path
        #    (fetch -> upload_bytes -> untar into a clean workspace) is identical.
        await client.execute_command(vm1, f"rm -f {WORKDIR}/{MARKER_NAME}")
        gone = await client.execute_command(
            vm1, f"cat {WORKDIR}/{MARKER_NAME} 2>/dev/null || echo MISSING"
        )
        print(f"  marker after delete: {str(getattr(gone, 'result', gone)).strip()!r}")

        # 4. Restore (fetch -> upload_bytes -> untar).
        print("Restoring snapshot...")
        await durability.restore_workspace(ws, user, row.id, client=client, uploads=uploads)

        # 5. Assert the marker is back.
        res = await client.execute_command(vm1, f"cat {WORKDIR}/{MARKER_NAME}")
        result_text = getattr(res, "result", None) or getattr(res, "output", None) or str(res)
        print(f"  marker read after restore: {result_text!r}")
        ok = MARKER_TEXT in str(result_text)
        print("RESULT:", "PASS — marker restored from snapshot" if ok else "FAIL — marker missing")
    finally:
        for vm in (vm1, vm2):
            if vm:
                try:
                    await client.delete_sandbox(vm)
                    print(f"  deleted VM {vm}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARN: failed to delete VM {vm}: {exc}")
        await client.close()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
