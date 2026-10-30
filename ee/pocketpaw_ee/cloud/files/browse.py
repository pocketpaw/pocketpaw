"""Per-mount paginated listing."""

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud.files.abac_config import AbacRuleSet
from pocketpaw_ee.cloud.files.dto import FileEntry, Page, RequestContext
from pocketpaw_ee.cloud.files.errors import MountNotFound
from pocketpaw_ee.cloud.files.permissions import apply_abac, derive_capabilities
from pocketpaw_ee.cloud.files.registry import ProviderRegistry


async def browse_mount(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    mount_path: str,
    variables: dict[str, str],
    cursor: str | None,
    limit: int,
    filters: dict[str, Any],
) -> Page[FileEntry]:
    mount = registry.resolve_mount(path=mount_path, variables=variables)
    # Mounts may reference providers declared in mounts.yaml but not yet
    # registered in code. Translate the KeyError into the typed error so the
    # router maps it to a 404 files.mount_not_found.
    try:
        provider = registry.get(mount.provider_id)
    except KeyError as exc:
        raise MountNotFound(mount_path) from exc
    raw = await provider.list_entries(ctx, mount_path, cursor, limit, filters)
    filtered = apply_abac(raw.items, ctx=ctx, rules=rules)

    out: list[FileEntry] = []
    for e in filtered:
        rbac = provider.baseline_rbac(ctx, e)
        # AbacRuleSet.allows is currently binary (entry either passes or is
        # filtered out entirely above). We still derive abac_allowed honestly
        # per-entry here so that when the ruleset gains per-capability
        # restrictions this computation stays correct without rework.
        abac_allowed = rules.allows(tags=e.tags, attributes=ctx.attributes)
        caps = derive_capabilities(
            entry=e, rbac=rbac, mount_writable=mount.writable, abac_allowed=abac_allowed
        )
        out.append(e.model_copy(update={"capabilities": caps}))

    return Page(items=out, next_cursor=raw.next_cursor)
