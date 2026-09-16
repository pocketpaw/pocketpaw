"""The boundary around the cross-tenant read helpers.

Static analysis, no database. Split out of test_tenant_directory.py because
that module marks everything asyncio for its mongomock fixtures, and this is a
plain synchronous check.
"""

from __future__ import annotations

import pathlib

_CROSS_TENANT_HELPERS = (
    "platform_search_workspaces",
    "platform_get_workspace",
    "platform_list_members",
    "platform_find_users",
    # Added 2026-09-16 (Paw Admin chunk 7): the entitlement-override writer —
    # no membership check, workspace_id is a caller-supplied path parameter,
    # same shape as the four above.
    "platform_set_workspace_overrides",
)


def _ee_root() -> pathlib.Path:
    # tests/cloud/platform/<file> -> repo root -> ee/pocketpaw_ee
    return pathlib.Path(__file__).resolve().parents[3] / "ee" / "pocketpaw_ee"


def test_only_the_platform_package_uses_the_cross_tenant_helpers() -> None:
    """Nothing outside ee/pocketpaw_ee/cloud/platform/ may call a platform_* helper.

    These helpers ignore tenancy by design and sit in the same module as the
    tenant-scoped ones. A tenant route that reached for one would serve another
    customer's data while looking entirely ordinary at the call site, so the
    boundary is asserted here rather than trusted to the naming convention.

    If this fails, the fix is almost never to widen the exemption list.
    """
    ee_root = _ee_root()
    assert ee_root.is_dir(), f"expected {ee_root} to exist"

    platform_pkg = ee_root / "cloud" / "platform"
    # The helpers are DEFINED here, so this file naturally mentions them.
    definition_site = ee_root / "cloud" / "workspace" / "service.py"

    offenders: list[str] = []
    for path in ee_root.rglob("*.py"):
        if platform_pkg in path.parents or path == definition_site:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(helper in text for helper in _CROSS_TENANT_HELPERS):
            offenders.append(str(path.relative_to(ee_root)))

    assert offenders == [], (
        "These modules reach for a cross-tenant helper but are not part of the "
        f"platform package: {offenders}"
    )


def test_the_boundary_check_can_actually_fail() -> None:
    """Guards against the test above passing because it scanned nothing."""
    ee_root = _ee_root()
    scanned = sum(1 for _ in ee_root.rglob("*.py"))
    assert scanned > 100, f"only scanned {scanned} files; the glob is probably wrong"
