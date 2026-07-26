# websandbox_live_smoke.py — ONE live Daytona smoke for the Web Cursor
# cold-provision slice (WC-2). NOT a pytest test — kept out of CI on purpose so
# a real VM (which costs money) is only ever provisioned when a human runs this.
# Created 2026-07-15 (feat/websandbox-vm-provision).
#
# What it does: loads .env, provisions a REAL Daytona VM (auto_stop_interval=30
# min), clones the public octocat/Hello-World repo into the project dir, lists +
# prints the tree, and ALWAYS deletes the VM in a finally block — cleanup is
# non-negotiable even on exception, because a leaked VM keeps billing.
#
# Run: .venv/Scripts/python.exe scripts/websandbox_live_smoke.py
from __future__ import annotations

import asyncio

from dotenv import load_dotenv

REPO_URL = "https://github.com/octocat/Hello-World.git"
AUTO_STOP_MINUTES = 30
BOOT_TIMEOUT_SECONDS = 180.0


async def main() -> int:
    load_dotenv(".env")

    from pocketpaw_ee.cloud.daytona.client import get_daytona_client

    client = get_daytona_client()
    if client is None:
        print("FAIL: Daytona is not configured (get_daytona_client() returned None).")
        print("      Set the Daytona keys in .env and retry.")
        return 1

    sandbox_id: str | None = None
    try:
        print(f"Provisioning a Daytona VM (auto_stop_interval={AUTO_STOP_MINUTES} min)...")
        info = await client.create_sandbox(
            name="websandbox-live-smoke",
            auto_stop_interval=AUTO_STOP_MINUTES,
        )
        sandbox_id = info.id
        print(f"  created: id={sandbox_id} name={info.name} state={info.state}")

        print("Waiting for the VM to boot...")
        await client.wait_for_sandbox(
            sandbox_id, target_state="started", timeout=BOOT_TIMEOUT_SECONDS
        )
        print("  booted.")

        project_dir = await client.get_project_dir(sandbox_id)
        print(f"Cloning {REPO_URL} into {project_dir} (no credentials — public repo)...")
        await client.git_clone(sandbox_id, REPO_URL, project_dir)
        print("  cloned.")

        print("Listing the file tree:")
        files = await client.list_files(sandbox_id, project_dir)
        for f in files:
            kind = "dir " if getattr(f, "is_dir", False) else "file"
            print(f"  [{kind}] {f.name}  ({getattr(f, 'size', 0)} bytes)")

        print("\nPASS: provisioned, cloned, and listed a real Daytona sandbox.")
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
