# ee/pocketpaw_ee/sites/durable_objects.py: vet the Durable Objects a project
# build declares, plan the migration its upload carries, and tear a script's DOs
# down. Pure except ``check_account_budget`` and ``teardown_script`` (Cloudflare).
#
# A build declares DOs in paw-build.json's ``durableObjects`` block (written by
# paw-sites' buildPawManifest from the site's wrangler.jsonc):
#   {"bindings": [{"name": "ROOM", "className": "Room"}],
#    "migrations": [{"tag": "v1", "new_sqlite_classes": ["Room"]}],
#    "exportedClasses": ["Room"]}
# Rules (design: docs/design/drafts/2026-10-08-sites-durable-objects.md):
#   * Off unless ``PAW_SITES_DURABLE_OBJECTS`` is truthy. With it off, a build that
#     declares DOs is REFUSED (``sites.do_disabled``), never deployed without them.
#   * SQLite classes only, same script only: a step may carry only
#     ``new_sqlite_classes`` / ``renamed_classes`` / ``deleted_classes``; a binding
#     carrying script_name / environment / namespace_id / dispatch_namespace is
#     refused, not dropped. Every bound and live class must be exported by the main
#     module (``exportedClasses``; a textual check, Cloudflare's upload is final).
#   * Class cap: 1 on a free site, ``PAW_SITES_DO_MAX_CLASSES`` (default 3, max 5)
#     on a paid one. Per-room participant caps belong to the recipe, not here.
#   * Migrations use Cloudflare's tagged form (old_tag / new_tag / steps), not the
#     declarative ``exports`` map. Tags are append-only; ``plan_migration`` decides
#     what to send from the tag last applied to the script. Destructive steps
#     (delete, rename) need an explicit per-class confirmation.
#   * On the ``account`` target DO namespaces share the account's limits, so a NEW
#     class is refused past ``PAW_SITES_DO_ACCOUNT_BUDGET`` namespaces, and the
#     check fails closed when the count cannot be read.
#   * State per script (Site / draft registry): the applied tag HISTORY and the live
#     classes, written only after a successful upload (``migration_tags_after``).
#   * ``teardown_script``: tombstone upload (``deleted_classes``), forced delete, then
#     a namespace-list check. Best effort, never raises; the caller retries.
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError

logger = logging.getLogger(__name__)

FLAG_ENV = "PAW_SITES_DURABLE_OBJECTS"
MAX_CLASSES_ENV = "PAW_SITES_DO_MAX_CLASSES"
DEFAULT_MAX_CLASSES = 3
MAX_CLASSES_CEILING = 5
FREE_MAX_CLASSES = 1
ACCOUNT_BUDGET_ENV = "PAW_SITES_DO_ACCOUNT_BUDGET"
DEFAULT_ACCOUNT_BUDGET = 300
ACCOUNT_TARGET = "account"  # cloudflare_client.ACCOUNT_TARGET; kept import-free
DISPATCH_TARGET = "dispatch"
#: The tag of the stub upload that deletes every class before a script goes.
TOMBSTONE_TAG = "paw-tombstone"
_TOMBSTONE_MODULE = 'export default { fetch() { return new Response("gone", { status: 410 }) } };'
_TOMBSTONE_COMPAT = "2026-09-01"

_TRUTHY = {"1", "true", "yes", "on"}
_TAG = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_BINDING_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CLASS_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{0,127}$")
_CROSS_SCRIPT = ("script_name", "scriptName", "environment", "namespace_id", "dispatch_namespace")
_STEP_KEYS = frozenset({"tag", "new_sqlite_classes", "renamed_classes", "deleted_classes"})


def enabled() -> bool:
    return (os.environ.get(FLAG_ENV) or "").strip().lower() in _TRUTHY


def _config_error(message: str) -> ValidationError:
    return ValidationError("sites.do_config", f"Durable Objects config: {message}")


@dataclass(frozen=True)
class Migration:
    tag: str
    new_sqlite_classes: tuple[str, ...] = ()
    renamed_classes: tuple[tuple[str, str], ...] = ()
    deleted_classes: tuple[str, ...] = ()

    def step(self) -> dict:
        """The step in Cloudflare's upload ``migrations.steps`` shape."""
        out: dict[str, Any] = {}
        if self.new_sqlite_classes:
            out["new_sqlite_classes"] = list(self.new_sqlite_classes)
        if self.renamed_classes:
            out["renamed_classes"] = [{"from": a, "to": b} for a, b in self.renamed_classes]
        if self.deleted_classes:
            out["deleted_classes"] = list(self.deleted_classes)
        return out

    @property
    def destroys(self) -> tuple[str, ...]:
        """Classes whose objects this step deletes or renames away."""
        return self.deleted_classes + tuple(a for a, _ in self.renamed_classes)


@dataclass(frozen=True)
class DurableObjectsConfig:
    bindings: dict[str, str] = field(default_factory=dict)  # binding name -> class
    migrations: tuple[Migration, ...] = ()
    exported: frozenset[str] = frozenset()
    live_classes: tuple[str, ...] = ()  # classes alive after every migration

    @property
    def empty(self) -> bool:
        return not (self.bindings or self.migrations)


@dataclass(frozen=True)
class DurableObjectState:
    """What is already applied to the script being deployed. ``applied_tags`` is the
    full tag history when known; without it a rollback cannot be told apart from a
    rewritten history, so both are refused."""

    migration_tag: str | None = None
    live_classes: tuple[str, ...] = ()
    applied_tags: tuple[str, ...] | None = None

    @classmethod
    def from_history(cls, tags: Iterable[str], classes: Iterable[str]) -> DurableObjectState:
        """The state a Site / draft row stores: tag history oldest first, live classes."""
        history = tuple(t for t in tags if t)
        return cls(history[-1] if history else None, tuple(classes), history)


@dataclass(frozen=True)
class MigrationPlan:
    migrations: dict | None  # the upload's ``migrations``, None = send nothing
    tag: str | None  # the script's tag once the upload succeeds
    new_classes: tuple[str, ...] = ()  # classes the pending steps create
    rollback: bool = False


@dataclass(frozen=True)
class VettedDurableObjects:
    bindings: dict[str, str]
    plan: MigrationPlan
    classes: tuple[str, ...]  # live after this deploy
    tags: tuple[str, ...] = ()  # the declared history
    previous: tuple[str, ...] = ()  # the applied history before this deploy

    def upload_bindings(self) -> list[dict]:
        return [
            {"type": "durable_object_namespace", "name": n, "class_name": c}
            for n, c in self.bindings.items()
        ]


# ------------------------------------------------------------------ parser


def _class_list(raw: Any, what: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _config_error(f"{what} must be a list of class names")
    for name in raw:
        if not isinstance(name, str) or not _CLASS_NAME.match(name):
            raise _config_error(f"{what} has {name!r}, which is not a class name")
    return tuple(raw)


def _parse_migration(raw: Any, live: list[str]) -> Migration:
    if not isinstance(raw, dict):
        raise _config_error("each migration must be an object with a tag")
    tag = raw.get("tag")
    if not isinstance(tag, str) or not _TAG.match(tag):
        raise _config_error(f"migration tag {tag!r} must match {_TAG.pattern}")
    if "new_classes" in raw:
        raise _config_error(
            f"migration {tag!r} uses new_classes (key-value storage). Paw Sites only runs "
            "SQLite-backed Durable Objects: use new_sqlite_classes."
        )
    if "transferred_classes" in raw:
        raise _config_error(
            f"migration {tag!r} uses transferred_classes. Moving a class from another "
            "script is not supported on Paw Sites."
        )
    unknown = sorted(set(raw) - _STEP_KEYS)
    if unknown:
        raise _config_error(f"migration {tag!r} has unsupported fields: {', '.join(unknown)}")
    new = _class_list(raw.get("new_sqlite_classes"), f"migration {tag!r} new_sqlite_classes")
    deleted = _class_list(raw.get("deleted_classes"), f"migration {tag!r} deleted_classes")
    renames_raw = raw.get("renamed_classes") or []
    if not isinstance(renames_raw, list):
        raise _config_error(f"migration {tag!r} renamed_classes must be a list")
    renamed: list[tuple[str, str]] = []
    for r in renames_raw:
        pair = (r.get("from"), r.get("to")) if isinstance(r, dict) else (None, None)
        _class_list(list(pair), f"migration {tag!r} renamed_classes")
        renamed.append((str(pair[0]), str(pair[1])))

    # Replay the step against the classes alive so far.
    for name in new:
        if name in live:
            raise _config_error(f"migration {tag!r} creates {name}, which already exists")
        live.append(name)
    for old, to in renamed:
        if old not in live or to in live:
            raise _config_error(f"migration {tag!r} cannot rename {old} to {to}")
        live[live.index(old)] = to
    for name in deleted:
        if name not in live:
            raise _config_error(f"migration {tag!r} deletes {name}, which does not exist")
        live.remove(name)
    return Migration(tag, new, tuple(renamed), deleted)


def parse_durable_objects(raw: Any) -> DurableObjectsConfig:
    """Parse and shape-check a ``durableObjects`` block. Raises ``sites.do_config``."""
    if raw is None:
        return DurableObjectsConfig()
    if not isinstance(raw, dict):
        raise _config_error("durableObjects must be an object")

    bindings: dict[str, str] = {}
    raw_bindings = raw.get("bindings") or []
    if not isinstance(raw_bindings, list):
        raise _config_error("bindings must be a list")
    for b in raw_bindings:
        if not isinstance(b, dict):
            raise _config_error("each binding must be an object with name and className")
        cross = [k for k in _CROSS_SCRIPT if b.get(k) not in (None, "")]
        if cross:
            raise _config_error(
                f"binding {b.get('name')!r} sets {', '.join(cross)}. Paw Sites only binds "
                "classes defined in the site's own Worker; remove that field."
            )
        name = b.get("name")
        cls = b.get("className", b.get("class_name"))
        if not isinstance(name, str) or not _BINDING_NAME.match(name):
            raise _config_error(f"binding name {name!r} is not a valid identifier")
        if not isinstance(cls, str) or not _CLASS_NAME.match(cls):
            raise _config_error(f"binding {name!r} has class {cls!r}, which is not a class name")
        if name in bindings:
            raise _config_error(f"binding name {name!r} is declared twice")
        bindings[name] = cls

    raw_migrations = raw.get("migrations") or []
    if not isinstance(raw_migrations, list):
        raise _config_error("migrations must be a list")
    live: list[str] = []
    migrations: list[Migration] = []
    for m in raw_migrations:
        migration = _parse_migration(m, live)
        if any(prev.tag == migration.tag for prev in migrations):
            raise _config_error(f"migration tag {migration.tag!r} is used twice")
        migrations.append(migration)

    exported = _class_list(raw.get("exportedClasses"), "exportedClasses")
    return DurableObjectsConfig(bindings, tuple(migrations), frozenset(exported), tuple(live))


def declares_durable_objects(manifest: Mapping[str, Any]) -> bool:
    block = manifest.get("durableObjects")
    if isinstance(block, dict) and (block.get("bindings") or block.get("migrations")):
        return True
    if block is not None and not isinstance(block, dict):
        return True  # malformed still counts: let the parser say why
    requests = manifest.get("bindingRequests")
    return any(
        isinstance(r, dict) and str(r.get("type", "")).strip().lower() == "do"
        for r in (requests if isinstance(requests, list) else [])
    )


def class_cap(*, paid: bool) -> int:
    if not paid:
        return FREE_MAX_CLASSES
    raw = (os.environ.get(MAX_CLASSES_ENV) or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_CLASSES
    except ValueError:
        logger.warning("%s=%r is not an int; using %d", MAX_CLASSES_ENV, raw, DEFAULT_MAX_CLASSES)
        value = DEFAULT_MAX_CLASSES
    return min(max(value, 0), MAX_CLASSES_CEILING)


# ------------------------------------------------------------------ planner


def _names(classes: Iterable[str]) -> str:
    return ", ".join(sorted(set(classes)))


def plan_migration(
    declared: Sequence[Migration],
    applied_tag: str | None,
    *,
    live_classes: Iterable[str] = (),
    exported: Iterable[str] = (),
    applied_tags: Sequence[str] | None = None,
    confirm: Iterable[str] = (),
    allow_data_loss: bool = False,
) -> MigrationPlan:
    """What the upload sends, given the declared history and the applied tag.

    1. nothing applied: every step, ``new_tag`` = the last tag.
    2. applied tag at i: ``old_tag`` = it, steps after i (Cloudflare rejects a stale
       old_tag, which also guards two concurrent publishes).
    3. applied tag is the last: send nothing.
    4. applied tag not declared: a rollback when ``applied_tags`` proves the declared
       list is a prefix of history AND the bundle still exports every live class (no
       migrations, tag kept, data kept); anything else is refused.
    5. pending deletes / renames of live classes need each class in ``confirm``
       (``allow_data_loss`` skips that, for drafts)."""
    tags = [m.tag for m in declared]
    live = set(live_classes)
    if applied_tag is not None and applied_tag not in tags:
        history = list(applied_tags) if applied_tags is not None else None
        is_prefix = (
            history is not None and history[: len(tags)] == tags and len(tags) < len(history)
        )
        if not is_prefix:
            raise ValidationError(
                "sites.do_history_diverged",
                f"The live site's Durable Objects are at migration {applied_tag!r}, which this "
                "build does not declare. Migrations are append-only: restore the earlier "
                "entries unchanged and add new ones after them. If this is an older version "
                f"of the site, it predates migration {applied_tag!r}.",
            )
        missing = live - set(exported)
        if missing:
            raise ValidationError(
                "sites.do_history_diverged",
                f"This version predates migration {applied_tag!r} and no longer exports "
                f"{_names(missing)}, which the live site still has. Deploy a version that "
                "exports every live Durable Object class.",
            )
        return MigrationPlan(None, applied_tag, rollback=True)

    start = tags.index(applied_tag) + 1 if applied_tag is not None else 0
    pending = list(declared[start:])
    if not pending:
        return MigrationPlan(None, applied_tag)

    at_risk = [c for m in pending for c in m.destroys if c in live]
    unconfirmed = set(at_risk) - set(confirm)
    if unconfirmed and not allow_data_loss:
        raise ValidationError(
            "sites.do_data_loss_unconfirmed",
            f"This publish deletes or renames Durable Object classes and their stored data "
            f"for good: {_names(unconfirmed)}. The site owner has to confirm that before it "
            "can go live (publish again with confirm_do_data_loss: "
            f"{sorted(unconfirmed)!r}, from the owner's publish dialog only).",
        )
    migrations: dict[str, Any] = {"new_tag": tags[-1], "steps": [m.step() for m in pending]}
    if applied_tag is not None:
        migrations = {"old_tag": applied_tag, **migrations}
    new = tuple(c for m in pending for c in m.new_sqlite_classes)
    return MigrationPlan(migrations, tags[-1], new_classes=new)


# -------------------------------------------------------------------- vet


def vet_durable_objects(
    manifest: Mapping[str, Any],
    *,
    paid: bool,
    state: DurableObjectState | None = None,
    confirm: Iterable[str] = (),
    allow_data_loss: bool = False,
) -> VettedDurableObjects | None:
    """Everything the deploy needs to know about the build's DOs, or None when it
    has none. Raises ``ValidationError`` on anything that must not deploy."""
    state = state or DurableObjectState()
    if not declares_durable_objects(manifest) and not state.live_classes:
        return None
    if not enabled():
        cfg_raw = manifest.get("durableObjects")
        named = ""
        if isinstance(cfg_raw, dict):
            classes = [
                str(b.get("className", b.get("class_name")))
                for b in cfg_raw.get("bindings") or []
                if isinstance(b, dict)
            ]
            named = f" ({_names(classes)})" if classes else ""
        if not declares_durable_objects(manifest):
            raise ValidationError(
                "sites.do_disabled",
                f"This site has live Durable Objects ({_names(state.live_classes)}), which "
                "are turned off on Paw Sites right now, so it cannot be redeployed yet.",
            )
        raise ValidationError(
            "sites.do_disabled",
            f"This build declares Durable Objects{named}, which are not enabled on Paw "
            "Sites yet. Remove the durable_objects bindings and migrations from the "
            "project's wrangler config to deploy it.",
        )
    cfg = parse_durable_objects(manifest.get("durableObjects"))

    cap = class_cap(paid=paid)
    if len(cfg.live_classes) > cap:
        plan_word = "a paid site" if paid else "a free site"
        raise ValidationError(
            "sites.do_class_cap",
            f"{plan_word[0].upper()}{plan_word[1:]} can have {cap} Durable Object "
            f"class{'es' if cap != 1 else ''}; this build declares {len(cfg.live_classes)} "
            f"({_names(cfg.live_classes)}). Merge them into fewer classes"
            + ("." if paid else ", or upgrade this site to the Site plan."),
        )
    for name, cls in cfg.bindings.items():
        if cls not in cfg.live_classes:
            raise _config_error(
                f"binding {name!r} points at class {cls}, which no migration creates. Add a "
                f"migration with new_sqlite_classes: [{cls!r}]."
            )
    not_exported = (set(cfg.bindings.values()) | set(cfg.live_classes)) - cfg.exported
    if not_exported:
        raise ValidationError(
            "sites.do_class_missing",
            f"The Worker's main module does not export {_names(not_exported)}. Export each "
            "Durable Object class from the entry file (export class Room ...).",
        )

    plan = plan_migration(
        cfg.migrations,
        state.migration_tag,
        live_classes=state.live_classes,
        exported=cfg.exported,
        applied_tags=state.applied_tags,
        confirm=confirm,
        allow_data_loss=allow_data_loss,
    )
    classes = tuple(state.live_classes) if plan.rollback else cfg.live_classes
    return VettedDurableObjects(
        dict(cfg.bindings),
        plan,
        classes,
        tags=tuple(m.tag for m in cfg.migrations),
        previous=tuple(state.applied_tags or ()),
    )


def migration_tags_after(vetted: VettedDurableObjects, reported: str | None) -> tuple[str, ...]:
    """The applied tag history once the upload succeeded. Cloudflare's reported tag
    wins: a tag outside the declared history is appended as-is, so the next plan sees
    a history it does not recognise and refuses rather than guesses."""
    plan = vetted.plan
    if plan.migrations is None:
        return vetted.previous or ((plan.tag,) if plan.tag else ())
    tag = reported or plan.tag
    if tag in vetted.tags:
        return vetted.tags[: vetted.tags.index(tag) + 1]
    logger.warning("sites: Cloudflare reports migration tag %r outside the declared history", tag)
    return (*vetted.tags, tag) if tag else vetted.tags


async def check_account_budget(
    cf: Any, vetted: VettedDurableObjects | None, *, target: str
) -> None:
    """Refuse NEW classes on the account target past ``PAW_SITES_DO_ACCOUNT_BUDGET``
    namespaces. Fails closed: an unreadable count refuses. WfP has no namespace
    limit, and a deploy that creates no class never reads the list."""
    if target != ACCOUNT_TARGET or vetted is None or not vetted.plan.new_classes:
        return
    raw = (os.environ.get(ACCOUNT_BUDGET_ENV) or "").strip()
    try:
        budget = int(raw) if raw else DEFAULT_ACCOUNT_BUDGET
    except ValueError:
        budget = DEFAULT_ACCOUNT_BUDGET
    try:
        count = len(await cf.list_durable_object_namespaces())
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown", refuse
        logger.warning("sites: could not count Durable Object namespaces: %s", exc)
        raise ValidationError(
            "sites.do_budget_unknown",
            "Could not check the account's Durable Object capacity, so this publish was "
            "stopped before anything changed. Try again in a minute.",
        ) from exc
    if count + len(vetted.plan.new_classes) > budget:
        raise ValidationError(
            "sites.do_account_budget",
            "Paw Sites has no room for another Durable Object class right now. This "
            "publish was stopped before anything changed; contact support.",
        )


@dataclass(frozen=True)
class TeardownResult:
    ok: bool
    error: str = ""


async def _namespaces_of(cf: Any, script: str) -> list[dict]:
    rows = await cf.list_durable_object_namespaces()
    return [r for r in rows if isinstance(r, dict) and r.get("script") == script]


async def teardown_script(
    cf: Any,
    script: str,
    *,
    target: str,
    classes: Iterable[str],
    migration_tag: str | None,
    delete: bool = True,
) -> TeardownResult:
    """Delete every Durable Object of ``script`` and, with ``delete``, the script.

    1. Upload a stub with ``{old_tag, new_tag: paw-tombstone, deleted_classes}``,
       which deletes every object and its storage (skipped once tombstoned, or with
       no tag or classes). A failure here is logged and the next steps still run.
    2. ``DELETE ...?force=true`` on ``target`` (a 404 is success), which also
       removes the script's namespaces.
    3. The account's namespace list must show no row for ``script``.
    ``delete=False`` only runs step 3 (a resumed teardown whose script is gone).
    Never raises: ``ok`` is False with a short reason, and the caller leaves the
    work for its retry path (the draft sweeper, the cascade ledger)."""
    names = list(dict.fromkeys(classes))
    errors: list[str] = []
    if delete and names and migration_tag and migration_tag != TOMBSTONE_TAG:
        try:
            await cf.put_worker(
                script_name=script,
                modules=[_tombstone_module()],
                main_module="index.js",
                bindings=[],
                compatibility_date=_TOMBSTONE_COMPAT,
                target=target,
                migrations={
                    "old_tag": migration_tag,
                    "new_tag": TOMBSTONE_TAG,
                    "steps": [{"deleted_classes": names}],
                },
            )
        except Exception as exc:  # noqa: BLE001 - the forced delete still runs
            logger.warning("sites: tombstone upload for %s failed: %s", script, exc)
            errors.append(f"tombstone: {exc}")
    if delete:
        try:
            if target == DISPATCH_TARGET:
                await cf.delete_worker(script, force=True)
            else:
                await cf.delete_account_script(script, force=True)
        except Exception as exc:  # noqa: BLE001 - reported, retried by the caller
            logger.warning("sites: forced delete of %s failed: %s", script, exc)
            return TeardownResult(False, f"delete: {exc}"[:300])
    try:
        left = await _namespaces_of(cf, script)
    except Exception as exc:  # noqa: BLE001 - unverified is not done
        return TeardownResult(False, f"verify: {exc}"[:300])
    if left:
        ids = ", ".join(str(r.get("id")) for r in left)
        return TeardownResult(False, f"verify: namespaces still listed for {script}: {ids}"[:300])
    if errors:
        logger.info("sites: %s is gone despite: %s", script, "; ".join(errors))
    return TeardownResult(True)


def _tombstone_module() -> Any:
    from pocketpaw_ee.sites.cloudflare_client import WorkerModule

    return WorkerModule("index.js", _TOMBSTONE_MODULE.encode(), "application/javascript+module")


__all__ = [
    "ACCOUNT_BUDGET_ENV",
    "FLAG_ENV",
    "MAX_CLASSES_ENV",
    "DurableObjectState",
    "DurableObjectsConfig",
    "Migration",
    "MigrationPlan",
    "TOMBSTONE_TAG",
    "TeardownResult",
    "VettedDurableObjects",
    "check_account_budget",
    "class_cap",
    "declares_durable_objects",
    "enabled",
    "migration_tags_after",
    "parse_durable_objects",
    "plan_migration",
    "teardown_script",
    "vet_durable_objects",
]
