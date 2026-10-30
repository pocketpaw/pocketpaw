# websandbox_edit_smoke.py — ONE live smoke for the Web Cursor AI edit agent (WC-5a).
# Created 2026-07-15 (feat/websandbox-edit-agent).
#
# Out of CI. Exercises the REAL Daytona runtime + the REAL Anthropic model:
#   1. init the cloud registry (localhost mongo) so the tenancy guards work.
#   2. provision a VM and clone a SMALL public repo (octocat/Hello-World).
#   3. confirm the auto-created ``paw/edit-<hex>`` branch is checked out IN the VM
#      (git branch --show-current == the row's stored branch).
#   4. call ``edit.propose_edit`` with the REAL model on a real file and assert the
#      proposed content differs from the original and is non-empty.
#   5. ALWAYS delete the VM in ``finally`` (mandatory cleanup).
#
# Run:  .venv/Scripts/python.exe scripts/websandbox_edit_smoke.py
# Requires live Daytona + Anthropic keys in .env. If the Anthropic key is
# missing/invalid the script reports it honestly rather than faking a pass.
from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv(".env")  # load creds BEFORE importing config-reading modules

WORKSPACE = "smoke-ws"
USER = "smoke-user"
REPO = "https://github.com/octocat/Hello-World.git"
EDIT_PATH = "README"  # Hello-World ships a single README file
INSTRUCTION = "Add a new trailing line that is a comment saying 'edited by paw'."


async def main() -> int:
    from pocketpaw_ee.cloud import init_realtime
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.daytona.client import get_daytona_client
    from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db
    from pocketpaw_ee.cloud.websandbox import edit, provision
    from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

    daytona = get_daytona_client()
    if daytona is None:
        print("BLOCKED: Daytona is not configured (get_daytona_client() -> None). Check .env.")
        return 1

    await init_cloud_db()
    init_realtime()  # the registry emits events on every write
    sandbox_id: str | None = None
    try:
        print("[1/4] Provisioning VM + cloning", REPO, "...")
        view = await provision.open_sandbox(WORKSPACE, USER, {"repo": REPO}, client=daytona)
        sandbox_id = view.sandbox_id
        print(f"      ready: row={view.id} sandbox={sandbox_id} branch={view.branch}")

        print("[2/4] Confirming the auto-branch is checked out IN the VM ...")
        resp = await daytona.execute_command(
            sandbox_id, "git branch --show-current", cwd=WEBSANDBOX_WORKDIR, timeout=30
        )
        current = (getattr(resp, "result", "") or "").strip()
        print(f"      git branch --show-current -> {current!r}  (stored: {view.branch!r})")
        assert view.branch, "no branch stored on the row"
        assert current == view.branch, f"VM branch {current!r} != stored {view.branch!r}"
        assert current.startswith("paw/edit-"), f"unexpected branch name {current!r}"
        print("      OK: branch created + checked out in the VM")

        print("[3/4] Calling propose_edit with the REAL model on", EDIT_PATH, "...")
        try:
            result = await edit.propose_edit(
                WORKSPACE,
                USER,
                view.id,
                {"path": EDIT_PATH, "instruction": INSTRUCTION},
                daytona=daytona,  # client=None -> builds the real AsyncAnthropic
            )
        except CloudError as exc:
            if exc.code == "websandbox.edit_unavailable":
                # Honest report (task contract): no direct Anthropic key in this
                # env. The branch half is proven; the model half can't run here.
                print(f"      MODEL HALF BLOCKED: {exc.code}: {exc.message}")
                print("      (this deployment has no anthropic_api_key — Claude is")
                print("       routed via the claude_code OAuth provider, which the")
                print("       direct AsyncAnthropic seam cannot use)")
                print("\nSMOKE PARTIAL: auto-branch PASS; model half BLOCKED (no key)")
                return 2
            raise
        print("      --- original ---")
        print(result.originalContent)
        print("      --- proposed ---")
        print(result.proposedContent)
        assert result.proposedContent.strip(), "proposed content is empty"
        assert result.proposedContent != result.originalContent, "model returned no change"
        print("[4/4] OK: model returned a real, non-empty diff")
        print("\nSMOKE PASS")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nSMOKE FAIL: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        if sandbox_id is not None:
            try:
                await daytona.delete_sandbox(sandbox_id)
                print(f"[cleanup] deleted VM {sandbox_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] WARNING: failed to delete VM {sandbox_id}: {exc}")
        await close_cloud_db()
        await daytona.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
