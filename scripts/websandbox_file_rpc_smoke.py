# websandbox_file_rpc_smoke.py — ONE live Daytona smoke for the Web Cursor file
# read/write/list RPC slice (WC-4a). NOT a pytest test — kept out of CI so a real
# VM (which costs money) is only ever provisioned when a human runs this.
# Created 2026-07-15 (feat/websandbox-file-rpc).
#
# What it does: loads .env, provisions a REAL Daytona VM, then drives the SAME
# FileRpc helper the WebSocket endpoint uses (minus the socket) to:
#   1. write ``paw_rpc_smoke.txt`` with known content,
#   2. read it back and assert it round-trips (proves real persistence to the VM
#      filesystem),
#   3. list the project dir and assert the file appears,
#   4. attempt a ``../escape.txt`` write and assert the jail REJECTS it.
# It ALWAYS deletes the VM in a finally block — cleanup is non-negotiable even on
# exception, because a leaked VM keeps billing.
#
# Run: .venv/Scripts/python.exe scripts/websandbox_file_rpc_smoke.py
from __future__ import annotations

import asyncio

from dotenv import load_dotenv

AUTO_STOP_MINUTES = 30
BOOT_TIMEOUT_SECONDS = 180.0
SMOKE_FILE = "paw_rpc_smoke.txt"
SMOKE_CONTENT = "web-cursor file rpc round-trip: paw-rpc-ok\n"


async def main() -> int:
    load_dotenv(".env")

    from pocketpaw_ee.cloud.daytona.client import get_daytona_client
    from pocketpaw_ee.cloud.websandbox.files import FileRpc, FileRpcError

    client = get_daytona_client()
    if client is None:
        print("FAIL: Daytona is not configured (get_daytona_client() returned None).")
        print("      Set the Daytona keys in .env and retry.")
        return 1

    sandbox_id: str | None = None
    failures: list[str] = []

    try:
        print(f"Provisioning a Daytona VM (auto_stop_interval={AUTO_STOP_MINUTES} min)...")
        info = await client.create_sandbox(
            name="websandbox-file-rpc-smoke",
            auto_stop_interval=AUTO_STOP_MINUTES,
        )
        sandbox_id = info.id
        print(f"  created: id={sandbox_id} name={info.name} state={info.state}")

        print("Waiting for the VM to boot...")
        await client.wait_for_sandbox(
            sandbox_id, target_state="started", timeout=BOOT_TIMEOUT_SECONDS
        )
        print("  booted.")

        # The exact helper the WS endpoint instantiates per session.
        rpc = FileRpc(client, sandbox_id)
        project_dir = await rpc._root()  # noqa: SLF001 — smoke wants the resolved jail root
        print(f"  project dir (jail root): {project_dir}")

        # 1. write
        print(f"Writing {SMOKE_FILE!r} via FileRpc.write_file...")
        await rpc.write_file(SMOKE_FILE, SMOKE_CONTENT)
        print("  write ok.")

        # 2. read back + assert round-trip
        print(f"Reading {SMOKE_FILE!r} back...")
        read_back = await rpc.read_file(SMOKE_FILE)
        if read_back == SMOKE_CONTENT:
            print("  PASS: read-back content matches (real persistence to VM fs).")
        else:
            failures.append("read-back content did not match written content")
            print(f"  FAIL: got {read_back!r}, expected {SMOKE_CONTENT!r}")

        # 3. list dir + assert file appears
        print("Listing the project dir via FileRpc.list_dir('.')...")
        entries = await rpc.list_dir(".")
        names = [e["name"] for e in entries]
        if SMOKE_FILE in names:
            print(f"  PASS: {SMOKE_FILE!r} appears in the listing.")
        else:
            failures.append(f"{SMOKE_FILE} not found in directory listing")
            print(f"  FAIL: {SMOKE_FILE!r} not in {names}")

        # 4. traversal rejection
        print("Attempting a ../escape.txt write (must be rejected by the jail)...")
        try:
            await rpc.write_file("../escape.txt", "should never be written")
            failures.append("traversal write was NOT rejected")
            print("  FAIL: traversal write was allowed!")
        except FileRpcError as exc:
            print(f"  PASS: traversal rejected -> {exc.op}: {exc.message}")

        if failures:
            print("\nFAIL: " + "; ".join(failures))
            return 1
        print("\nPASS: write -> read -> list round-trip persisted + traversal rejected.")
        return 0
    except Exception as exc:  # noqa: BLE001 — smoke: report and still clean up
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if sandbox_id is not None:
            try:
                print(f"Cleaning up: deleting VM {sandbox_id}...")
                await client.delete_sandbox(sandbox_id)
                print(f"  VM {sandbox_id} DELETED.")
            except Exception as cleanup_exc:  # noqa: BLE001
                print(f"  WARNING: cleanup delete failed: {cleanup_exc}")
                print(f"  MANUAL CLEANUP REQUIRED for sandbox {sandbox_id}")
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
