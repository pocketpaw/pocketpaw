# websandbox_terminal_smoke.py — ONE live Daytona smoke for the Web Cursor
# terminal / PTY-over-WS slice (WC-3). NOT a pytest test — kept out of CI on
# purpose so a real VM (which costs money) is only ever provisioned when a human
# runs this. Created 2026-07-15 (feat/websandbox-terminal-ws).
#
# What it does: loads .env, provisions a REAL Daytona VM, opens a bash PTY via
# the SAME PtyBridge the WebSocket endpoint uses (minus the socket — the sink is
# an in-memory buffer), sends ``echo paw-smoke-ok``, collects the shell output
# for a couple of seconds, asserts the echo came back, resizes once, then ALWAYS
# deletes the VM in a finally block — cleanup is non-negotiable even on
# exception, because a leaked VM keeps billing.
#
# Run: .venv/Scripts/python.exe scripts/websandbox_terminal_smoke.py
from __future__ import annotations

import asyncio

from dotenv import load_dotenv

AUTO_STOP_MINUTES = 30
BOOT_TIMEOUT_SECONDS = 180.0
MARKER = "paw-smoke-ok"
COLLECT_SECONDS = 4.0


async def main() -> int:
    load_dotenv(".env")

    from pocketpaw_ee.cloud.daytona.client import get_daytona_client
    from pocketpaw_ee.cloud.websandbox.terminal import PtyBridge

    client = get_daytona_client()
    if client is None:
        print("FAIL: Daytona is not configured (get_daytona_client() returned None).")
        print("      Set the Daytona keys in .env and retry.")
        return 1

    sandbox_id: str | None = None
    bridge: PtyBridge | None = None
    output = bytearray()

    async def sink(data: bytes) -> None:
        # The exact hook the WS handler wires to websocket.send_bytes.
        output.extend(data)

    try:
        print(f"Provisioning a Daytona VM (auto_stop_interval={AUTO_STOP_MINUTES} min)...")
        info = await client.create_sandbox(
            name="websandbox-terminal-smoke",
            auto_stop_interval=AUTO_STOP_MINUTES,
        )
        sandbox_id = info.id
        print(f"  created: id={sandbox_id} name={info.name} state={info.state}")

        print("Waiting for the VM to boot...")
        await client.wait_for_sandbox(
            sandbox_id, target_state="started", timeout=BOOT_TIMEOUT_SECONDS
        )
        print("  booted.")

        print("Opening a bash PTY through PtyBridge (same code path as the WS)...")
        bridge = PtyBridge(client, sandbox_id, "term-smoke", sink)
        await bridge.start(cols=100, rows=30)
        print("  pty open.")

        print(f"Sending: echo {MARKER}")
        await bridge.send_input(f"echo {MARKER}\n")

        # Give the shell a moment to echo the command + print the result.
        await asyncio.sleep(COLLECT_SECONDS)

        print("Resizing the pty to 120x40...")
        await bridge.resize(120, 40)

        text = output.decode(errors="replace")
        print("--- collected terminal output ---")
        print(text.strip() or "<empty>")
        print("---------------------------------")

        if MARKER in text:
            print(f"\nPASS: the real shell echoed back '{MARKER}'.")
            return 0
        print(f"\nFAIL: marker '{MARKER}' not found in terminal output.")
        return 1
    except Exception as exc:  # noqa: BLE001 — smoke: report and still clean up
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if bridge is not None:
            try:
                await bridge.close()
                print("  pty session killed.")
            except Exception as close_exc:  # noqa: BLE001
                print(f"  WARNING: pty close failed: {close_exc}")
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
