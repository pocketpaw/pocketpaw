# ee/pocketpaw_ee/sites/site_upgrade.py — the owner's opt-in "Upgrade this site":
# turn an ``html`` pocket (hand-written or imported) into a ``project`` pocket on the
# ``static`` starter, in place, and undo it while nothing has been edited since.
#
# The conversion is mechanical. The html source map already is a static tree, so it
# becomes the project file map unchanged, plus the static starter's
# ``paw.template.json`` (``"build": "none"``, the no-build lane) and ``AGENTS.md``,
# read through ``project_tools.copy_template`` like ``start_site_from_template``.
# ``data-uid`` needs no stripping: the html lane stamps it only on the served preview
# (``html_uid_stamp`` / ``arm-html``) and never writes it into the source map.
# ``pattern`` is left as is: no publish gate reads ``"imported"`` any more.
#
# Invariants: an owner file is never overwritten (a source that already has either
# starter file is a 409); the result must be a no-build tree with a root index.html
# (422 otherwise), so the first draft preview stages in-process with no sandbox. The
# Site doc (id, domains, signed key, integrations, leads) is not touched: it is the
# same site, and the live deploy keeps serving until the next publish. Undo needs
# the snapshot ``pockets.service.convert_html_to_project`` writes as a draft version
# and a source whose digest still matches the one stamped at upgrade time.
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, NotFound, ValidationError
from pocketpaw_ee.sites.engines import is_upgradable, safe_rel

#: The starter an upgraded site lands on, and the two files it contributes.
UPGRADE_TEMPLATE = "static"
STARTER_FILES = ("paw.template.json", "AGENTS.md")


def source_digest(source: dict[str, Any]) -> str:
    """A stable digest of a source map. Deliberately not ``project_content_hash``,
    which also folds in the generator version: a deploy between upgrade and undo
    must not read as an edit."""
    body = json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _owned_pocket(workspace_id: str, user_id: str, pocket_id: str) -> dict[str, Any]:
    """The pocket the caller can read in THIS workspace (another workspace's is a 404).
    Edit access is checked again by the pockets writer that persists the change."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(pocket_id, user_id)
    if pocket.get("workspace") != workspace_id:
        raise NotFound("pocket", pocket_id)
    return pocket


async def _starter_files() -> dict[str, str]:
    """The static starter's manifest and AGENTS.md, from the vendored generator."""
    from pocketpaw_ee.sites import project_tools

    try:
        starter = await project_tools.copy_template(UPGRADE_TEMPLATE)
    except CloudError:
        raise
    except Exception as exc:  # ProjectCliError, a missing CLI, a timeout
        raise CloudError(
            503,
            "sites.upgrade_starter_unavailable",
            "The static starter could not be read right now. Try again in a moment.",
        ) from exc
    files = {name: starter.get(name) for name in STARTER_FILES}
    missing = [name for name, text in files.items() if not isinstance(text, str)]
    if missing:
        raise CloudError(
            503,
            "sites.upgrade_starter_unavailable",
            f"The static starter is missing {', '.join(missing)}.",
        )
    return {name: str(text) for name, text in files.items()}


def _refuse_unless_upgradable(pocket: dict[str, Any]) -> dict[str, Any]:
    from pocketpaw_ee.sites import project_build

    engine = pocket.get("engine")
    source = pocket.get("source")
    if not is_upgradable(engine) or not isinstance(source, dict):
        raise ValidationError(
            "sites.upgrade_unsupported",
            "Only an html site can be upgraded to a static project for now.",
        )
    paths = {safe_rel(raw) for raw in source}
    taken = [name for name in STARTER_FILES if name in paths]
    if taken:
        raise ConflictError(
            "sites.upgrade_file_exists",
            f"This site already has {' and '.join(taken)}, which the upgrade would add. "
            "Rename or remove it first; the upgrade never overwrites your files.",
        )
    if "index.html" not in paths:
        raise ValidationError(
            "sites.upgrade_no_index",
            "This site has no index.html at its root, so a static project would have no "
            "home page. Add one first.",
        )
    # Paths, text-only contents and the project size ceilings, before anything changes.
    project_build.project_files(source)
    return source


async def upgrade_html_site(*, workspace_id: str, user_id: str, pocket_id: str) -> dict[str, Any]:
    """Convert the html pocket into a static project and queue its first draft build
    (in-process: the no-build lane). See the module header for the rules."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import project_build, project_tools

    pocket = await _owned_pocket(workspace_id, user_id, pocket_id)
    old_source = _refuse_unless_upgradable(pocket)
    added = await _starter_files()
    new_source = {**old_source, **added}
    if not project_build.is_no_build(new_source):
        raise CloudError(
            503,
            "sites.upgrade_starter_unavailable",
            "The static starter no longer builds without a build step.",
        )
    upgraded_from = {
        "engine": "html",
        "at": datetime.now(UTC).isoformat(),
        "source_hash": source_digest(new_source),
    }
    snapshot = await pockets_service.convert_html_to_project(
        pocket_id,
        user_id,
        expected_source=old_source,
        source=new_source,
        project={"template": UPGRADE_TEMPLATE, "framework": UPGRADE_TEMPLATE, "recipes": []},
        upgraded_from=upgraded_from,
    )
    return {
        "pocket_id": pocket_id,
        "engine": "project",
        "template": UPGRADE_TEMPLATE,
        "added": sorted(added),
        "upgraded_from": {**upgraded_from, **snapshot},
        "verification": await project_tools.build_verification(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        ),
    }


async def revert_html_upgrade(*, workspace_id: str, user_id: str, pocket_id: str) -> dict[str, Any]:
    """Put an upgraded pocket back on the html engine with its old source and
    site_meta. 422 ``sites.upgrade_not_upgraded`` when it was never upgraded; 409
    ``sites.upgrade_revert_stale`` once it was edited after the upgrade (the edits
    would be lost); 409 ``sites.upgrade_snapshot_missing`` when the snapshot version
    is gone."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.versions import service as versions_service

    pocket = await _owned_pocket(workspace_id, user_id, pocket_id)
    raw_meta = pocket.get("siteMeta") or pocket.get("site_meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    record = meta.get("upgraded_from")
    source = pocket.get("source")
    if pocket.get("engine") != "project" or not isinstance(record, dict):
        raise ValidationError(
            "sites.upgrade_not_upgraded", "This site was not upgraded, so there is nothing to undo."
        )
    if not isinstance(source, dict) or source_digest(source) != record.get("source_hash"):
        raise ConflictError(
            "sites.upgrade_revert_stale",
            "This site was edited after the upgrade, so undoing it would lose those edits. "
            "Restore an earlier version from its history instead.",
        )
    rows = await versions_service.list_versions(scope_type="pocket", scope_id=pocket_id)
    snapshot = next(
        (
            r
            for r in rows
            if str(r.id) == record.get("version_id") and r.workspace_id == workspace_id
        ),
        None,
    )
    if snapshot is None or not isinstance(snapshot.content, dict):
        raise ConflictError(
            "sites.upgrade_snapshot_missing",
            "The copy of the site from before the upgrade is gone, so it cannot be undone.",
        )
    prior_meta = record.get("site_meta") if isinstance(record.get("site_meta"), dict) else {}
    await pockets_service.restore_upgraded_html(
        pocket_id,
        user_id,
        expected_source=source,
        source=dict(snapshot.content),
        site_meta=prior_meta,
    )
    return {"pocket_id": pocket_id, "engine": "html", "version_no": snapshot.version_no}


__all__ = [
    "STARTER_FILES",
    "UPGRADE_TEMPLATE",
    "revert_html_upgrade",
    "source_digest",
    "upgrade_html_site",
]
