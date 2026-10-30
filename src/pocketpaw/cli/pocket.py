# src/pocketpaw/cli/pocket.py
# Created: 2026-06-13 (feat/pocket-template-reconcile, P2.4) — the CLI adapter
# for the Template Reconcile primitive. THIN: it is one of the three thin
# adapters over the single ``pockets.reconcile`` service (the others being the
# REST routes and the in-process API). The CLI does NOT re-implement reconcile
# logic — it calls the running dashboard's REST endpoints
# (POST /api/v1/pockets/{id}/reconcile/{preview,apply}) over loopback, the
# same pattern the `status` / `channels` CLI commands use to reach the server.
#
# `pocketpaw pocket reconcile <id>`          -> dry-run diff (DEFAULT)
# `pocketpaw pocket reconcile <id> --apply`  -> re-apply template-owned regions
#
# Auth: the loopback internal-token bypass (same as the pocket-specialist
# skill). The dashboard exports POCKETPAW_INTERNAL_TOKEN on boot; workspace +
# user come from --workspace/--user flags or the POCKETPAW_WORKSPACE_ID /
# POCKETPAW_USER_ID env vars. The server re-checks access on the resolved
# identity, so the CLI cannot escalate.
"""`pocketpaw pocket` CLI command family — reconcile a pocket against its
source template.

Reconcile re-applies the template-owned regions (ui / actions / sources /
shape) of the template a pocket was installed from, while preserving the
instance-owned regions (state rows, selection, pending proposals, the pocket
name / sharing). Preview is a dry run; ``--apply`` writes.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _server_base(port: int) -> str:
    return f"http://localhost:{port}"


def _bypass_headers(workspace_id: str, user_id: str) -> dict[str, str]:
    """Build the loopback internal-bypass headers for a reconcile call.

    Mirrors the pocket-specialist skill's contract: the magic header, the
    process-local token, and the workspace + user ids. All four are required
    by the server or it returns a clean 401.
    """
    token = os.environ.get("POCKETPAW_INTERNAL_TOKEN", "")
    return {
        "Content-Type": "application/json",
        "X-PocketPaw-Internal": "true",
        "X-PocketPaw-Internal-Token": token,
        "X-PocketPaw-Workspace-Id": workspace_id,
        "X-PocketPaw-User-Id": user_id,
    }


def _resolve_identity(
    workspace: str | None, user: str | None
) -> tuple[str | None, str | None, str | None]:
    """Resolve (workspace_id, user_id, error). Flags win over env vars."""
    workspace_id = workspace or os.environ.get("POCKETPAW_WORKSPACE_ID")
    user_id = user or os.environ.get("POCKETPAW_USER_ID")
    if not workspace_id:
        return None, None, ("No workspace id. Pass --workspace <id> or set POCKETPAW_WORKSPACE_ID.")
    if not user_id:
        return None, None, "No user id. Pass --user <id> or set POCKETPAW_USER_ID."
    return workspace_id, user_id, None


def _call(
    *, method_path: str, port: int, headers: dict[str, str]
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """POST to the reconcile endpoint. Returns (status_code, json, error).

    A connection failure (dashboard not running) returns
    ``(None, None, message)`` so the caller can print a friendly hint and
    exit non-zero rather than dumping a traceback.
    """
    import httpx

    url = f"{_server_base(port)}{method_path}"
    try:
        resp = httpx.post(url, headers=headers, timeout=30.0)
    except httpx.ConnectError:
        return (
            None,
            None,
            (
                f"PocketPaw is not running (could not connect to localhost:{port}). "
                "Start the dashboard first, or pass --port."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — surface any transport error cleanly
        return None, None, f"Request failed: {exc}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON error page
        body = None
    return resp.status_code, body, None


def _print_diff_table(diff: dict[str, Any]) -> None:
    """Human-readable reconcile diff."""
    print()
    print(f"Reconcile preview — pocket {diff.get('pocket_id')}")
    print(f"  Template:  {diff.get('template_slug')}")
    changed = diff.get("changed_regions") or []
    unchanged = diff.get("unchanged_regions") or []
    preserved = diff.get("preserved_regions") or []
    if changed:
        print(f"  Would refresh (template-owned): {', '.join(changed)}")
    else:
        print("  Would refresh (template-owned): nothing — already in sync")
    if unchanged:
        print(f"  Already matches:                {', '.join(unchanged)}")
    print(f"  Preserved (instance-owned):     {', '.join(preserved)}")
    print()


def _error_message(body: dict[str, Any] | None, status: int | None) -> str:
    """Pull a readable message out of the cloud error envelope
    ``{"error": {"code", "message"}}``, falling back to the status code."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            code = err.get("code", "")
            return f"{err['message']} ({code})" if code else str(err["message"])
    return f"reconcile failed (HTTP {status})"


def run_pocket_cmd(
    *,
    subaction: str | None,
    pocket_id: str | None,
    apply: bool = False,
    workspace: str | None = None,
    user: str | None = None,
    port: int = 8888,
    as_json: bool = False,
) -> int:
    """Entry point for ``pocketpaw pocket <subaction> ...``. Returns exit code.

    Only ``reconcile`` is implemented today. ``--apply`` switches the default
    dry-run preview into a real write.
    """
    if subaction != "reconcile":
        print(
            "Usage: pocketpaw pocket reconcile <pocket_id> [--apply] "
            "[--workspace <id>] [--user <id>]",
            file=sys.stderr,
        )
        return 2
    if not pocket_id:
        print(
            "Error: missing pocket id. Usage: pocketpaw pocket reconcile <pocket_id>",
            file=sys.stderr,
        )
        return 2

    workspace_id, user_id, err = _resolve_identity(workspace, user)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    headers = _bypass_headers(workspace_id, user_id)  # type: ignore[arg-type]
    if not headers["X-PocketPaw-Internal-Token"]:
        print(
            "Error: POCKETPAW_INTERNAL_TOKEN is not set. Run this on the same "
            "host as a running PocketPaw dashboard (it exports the token on boot).",
            file=sys.stderr,
        )
        return 2

    verb = "apply" if apply else "preview"
    status, body, conn_err = _call(
        method_path=f"/api/v1/pockets/{pocket_id}/reconcile/{verb}",
        port=port,
        headers=headers,
    )
    if conn_err:
        print(conn_err, file=sys.stderr)
        return 1
    if status is None or status >= 400:
        print(f"Error: {_error_message(body, status)}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(body, indent=2))
        return 0

    # Human output.
    if apply:
        result = body or {}
        if result.get("skipped"):
            print(f"\nPocket {pocket_id} already matches its template — nothing to apply.\n")
        else:
            diff = result.get("diff") or {}
            changed = diff.get("changed_regions") or []
            print(f"\nReconciled pocket {pocket_id}.")
            print(f"  Refreshed (template-owned): {', '.join(changed) or 'none'}")
            print("  Preserved (instance-owned): state\n")
    else:
        _print_diff_table(body or {})
    return 0
