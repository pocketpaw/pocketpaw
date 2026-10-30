# legacy_build_shell.py — find, classify and migrate build-shell files that old Paw
# Sites authored before the generator took ownership of them.
#
# Old Paw Sites authored files the generator now owns and refuses. Since 2026-10-07
# ("open everything") package.json, vite.config.*, svelte.config.js, bunfig.toml and
# .npmrc are author-writable again (the generator merges them), so what is left is
# svelte's src/routes/+layout.ts/.js, lockfiles, a non-canonical spelling of
# paw.dependencies.json, and the generator namespaces.
#
# Three layers live here, pure first:
#
#   * ``classify_source`` — a PURE classifier. For each reserved key it says
#     SAFE_DROP (the generator emits the equivalent anyway), CONVERTIBLE (a manifest
#     under another spelling whose packages move into paw.dependencies.json through
#     the resolver) or NEEDS_REVIEW (anything we cannot prove equivalent; reported,
#     never touched).
#   * ``generator_owned_keys_message`` — the runtime safety net's wording, so a build
#     of an unmigrated pocket names the file instead of failing as generator_failed.
#   * ``run_migration`` — the operator runner behind
#     ``scripts/migrate_legacy_build_shell.py``. Dry run by default; ``apply`` writes
#     through ``pockets.service.migrate_legacy_build_shell`` (one draft version per
#     pocket, never a publish).
#
# The layout comparison is whitespace-, comment-, quote- and semicolon-insensitive.
# Anything else is NEEDS_REVIEW: a false "safe" drops behaviour a site depended on, a
# false "review" only costs a human a look.

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pocketpaw_ee.sites.dependency_manifest import (
    DEPENDENCY_MANIFEST_PATH,
    LOCKFILES,
    is_dependency_manifest_path,
    parse_manifest,
    render_manifest,
)
from pocketpaw_ee.sites.html_paths import is_reserved_html_path
from pocketpaw_ee.sites.react_paths import is_reserved_react_path, normalize_react_path
from pocketpaw_ee.sites.svelte_paths import is_reserved_svelte_path

logger = logging.getLogger(__name__)

MIGRATION_ENGINES: tuple[str, ...] = ("svelte", "react", "html")
MIGRATION_AUTHOR = "system:legacy-build-shell-migration"


class Classification(StrEnum):
    SAFE_DROP = "safe_drop"
    CONVERTIBLE = "convertible"
    NEEDS_REVIEW = "needs_review"


_ACTIONS = {
    Classification.SAFE_DROP: "drop",
    Classification.CONVERTIBLE: "convert_to_manifest_then_drop",
    Classification.NEEDS_REVIEW: "report_only",
}


@dataclass(frozen=True)
class Finding:
    """One reserved key in one source map, and what to do about it."""

    key: str  # as the author spelled it
    kind: str
    classification: Classification
    reason: str
    # CONVERTIBLE only: (name, range) pairs to hand the resolver.
    requests: tuple[tuple[str, str], ...] = ()

    @property
    def action(self) -> str:
        return _ACTIONS[self.classification]


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"(?m)(^|[^:'\"])//.*$")


def _strip_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub(r"\1", _BLOCK_COMMENT_RE.sub("", text))


def _squash(text: str) -> str:
    """Whitespace/quote/semicolon/trailing-comma-insensitive form of a JS snippet."""
    out = re.sub(r"\s+", "", text).replace('"', "'").replace(";", "")
    return re.sub(r",([}\])])", r"\1", out)


# --------------------------------------------------------------------------- #
# The svelte prerender layout
# --------------------------------------------------------------------------- #

_LAYOUT_FLAG_RE = re.compile(r"^exportconst(prerender|csr|ssr)=(true|false)$")


def _classify_layout(key: str, text: str, keeps_client_bundle: bool) -> Finding:
    """+layout.ts/.js: safe only when it is flags the generator sets the same way."""
    statements = [
        _squash(s)
        for s in re.split(r"[;\n]", _strip_comments(text))
        if _squash(s)  # drop blank statements
    ]
    flags: dict[str, bool] = {}
    for stmt in statements:
        m = _LAYOUT_FLAG_RE.match(stmt)
        if m is None:
            return Finding(
                key,
                "layout",
                Classification.NEEDS_REVIEW,
                "carries layout logic beyond the prerender/csr/ssr flags (a `load`, an "
                "import, or another option). The generator now owns this file, and moving "
                "a universal load into +layout.server.ts changes when it runs, so a "
                "human has to decide where it goes.",
            )
        flags[m.group(1)] = m.group(2) == "true"
    if flags.get("prerender") is False or flags.get("ssr") is False:
        return Finding(
            key,
            "layout",
            Classification.NEEDS_REVIEW,
            "turns prerender or ssr off, which a static Paw Site cannot honour. The "
            "generator always emits `prerender = true`.",
        )
    csr = flags.get("csr", True)  # SvelteKit's default when the flag is absent
    if csr != keeps_client_bundle:
        want = "true" if csr else "false"
        return Finding(
            key,
            "layout",
            Classification.NEEDS_REVIEW,
            f"sets csr = {want} but the site resolves keepsClientBundle = "
            f"{str(keeps_client_bundle).lower()}, and the generator's +layout.ts follows "
            f"keepsClientBundle. Set the pocket's keepsClientBundle to {want} first if "
            "the site should keep that behaviour, then re-run.",
        )
    return Finding(
        key,
        "layout",
        Classification.SAFE_DROP,
        "only sets flags the generator's +layout.ts sets the same way.",
    )


def _classify_lockfile(key: str) -> Finding:
    return Finding(
        key,
        "lockfile",
        Classification.SAFE_DROP,
        "is a derived lockfile. The build resolves from the merged package.json.",
    )


def _classify_manifest_variant(key: str, text: str, source: Mapping[str, Any]) -> Finding:
    if DEPENDENCY_MANIFEST_PATH in source:
        return Finding(
            key,
            "manifest_variant",
            Classification.NEEDS_REVIEW,
            "is a second spelling of paw.dependencies.json next to the canonical file.",
        )
    try:
        packages = parse_manifest(text)
    except ValueError as exc:
        return Finding(key, "manifest_variant", Classification.NEEDS_REVIEW, str(exc))
    if not packages:
        return Finding(key, "manifest_variant", Classification.SAFE_DROP, "declares no packages.")
    return Finding(
        key,
        "manifest_variant",
        Classification.CONVERTIBLE,
        "is paw.dependencies.json under a spelling the generator does not read; its "
        "packages are re-vetted and rewritten under the canonical key.",
        tuple((name, entry["version"]) for name, entry in sorted(packages.items())),
    )


def _is_reserved(engine: str, key: str) -> bool:
    if engine == "svelte":
        return is_reserved_svelte_path(key)
    if engine == "react":
        return is_reserved_react_path(key)
    if engine == "html":
        return is_reserved_html_path(key)
    return False


def generator_owned_keys(engine: str, source: Mapping[str, Any] | None) -> list[str]:
    """Keys the generator will refuse, as the author spelled them, sorted.

    The canonical ``paw.dependencies.json`` is NOT one: the generator reads it as the
    manifest and strips it before its reserved-path guard runs.
    """
    if not isinstance(source, Mapping):
        return []
    return sorted(
        key
        for key in source
        if isinstance(key, str) and key != DEPENDENCY_MANIFEST_PATH and _is_reserved(engine, key)
    )


def classify_source(
    engine: str,
    source: Mapping[str, Any] | None,
    *,
    keeps_client_bundle: bool,
    dynamic: bool = False,
) -> list[Finding]:
    """Classify every generator-owned key in ``source``. Pure; order is by key.

    ``dynamic`` is accepted for callers; no remaining classification depends on it.
    """
    findings: list[Finding] = []
    for key in generator_owned_keys(engine, source):
        assert source is not None
        text = source[key]
        folded = normalize_react_path(key).casefold().lstrip("/")
        if not isinstance(text, str):
            findings.append(
                Finding(key, "other", Classification.NEEDS_REVIEW, "is not a text file.")
            )
            continue
        if is_dependency_manifest_path(key):
            findings.append(_classify_manifest_variant(key, text, source))
        elif folded in ("src/routes/+layout.ts", "src/routes/+layout.js") and engine == "svelte":
            findings.append(_classify_layout(key, text, keeps_client_bundle))
        elif folded in LOCKFILES:
            findings.append(_classify_lockfile(key))
        else:
            findings.append(
                Finding(
                    key,
                    "generator_namespace",
                    Classification.NEEDS_REVIEW,
                    "was already generator-owned before PS-1, so this site could not "
                    "build before either. It needs a human.",
                )
            )
    return findings


def generator_owned_keys_message(engine: str, source: Mapping[str, Any] | None) -> str | None:
    """The error an unmigrated pocket's build surfaces, or None when there is nothing."""
    keys = generator_owned_keys(engine, source)
    if not keys:
        return None
    return reserved_path_message(keys)


def reserved_path_message(keys: list[str] | str | None) -> str:
    """Name the generator-owned file(s) in words an agent and a user can act on."""
    if isinstance(keys, str):
        keys = [keys]
    named = ", ".join(f"`{k}`" for k in keys) if keys else "a build-shell file"
    return (
        f"This site's source map contains {named}, which the site generator now owns "
        "and writes itself (src/routes/+layout.ts, lockfiles, a second spelling of "
        "paw.dependencies.json). The site was authored before that change, so it "
        "cannot build until the one-time build-shell migration moves the file out. "
        "An edit cannot fix it: the edit tools refuse those paths. npm packages "
        "belong in paw.dependencies.json via set_site_dependencies."
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


@dataclass
class _PocketPlan:
    remove: dict[str, Any] = field(default_factory=dict)
    manifest: str | None = None


async def _resolve_conversions(
    pocket: Mapping[str, Any],
    findings: list[Finding],
    resolve: Callable[..., Any],
) -> tuple[list[dict[str, Any]], _PocketPlan]:
    """Resolve every CONVERTIBLE finding; downgrade the ones the resolver refuses."""
    from pocketpaw_ee.sites.dependency_resolver import DependencyRequest

    source = pocket.get("source") or {}
    engine = pocket["engine"]
    try:
        packages = dict(parse_manifest(source.get(DEPENDENCY_MANIFEST_PATH)))
        manifest_ok = True
    except ValueError:
        packages, manifest_ok = {}, False
    before = dict(packages)
    rows: list[dict[str, Any]] = []
    plan = _PocketPlan()
    for f in findings:
        row: dict[str, Any] = {
            "file": f.key,
            "kind": f.kind,
            "class": f.classification.value,
            "action": f.action,
            "reason": f.reason,
        }
        if f.classification is Classification.SAFE_DROP:
            plan.remove[f.key] = source[f.key]
        elif f.classification is Classification.CONVERTIBLE:
            if not manifest_ok:
                row.update(
                    {"class": Classification.NEEDS_REVIEW.value, "action": "report_only"},
                    reason="the existing paw.dependencies.json is unreadable, so packages "
                    "cannot be merged into it.",
                )
                rows.append(row)
                continue
            wanted = [(n, r) for n, r in f.requests if n not in packages]
            row["already_declared"] = sorted(n for n, _ in f.requests if n in packages)
            result = await resolve(
                [DependencyRequest(name=n, range=r) for n, r in wanted],
                engine,
                already_declared=packages.keys(),
            )
            if result.rejected:
                row.update(
                    {
                        "class": Classification.NEEDS_REVIEW.value,
                        "action": "report_only",
                        "rejected": [r.as_dict() for r in result.rejected],
                    },
                    reason="the resolver refused some of its packages, so the file is "
                    "left in place for a human.",
                )
                rows.append(row)
                continue
            resolved = {n: p.manifest_entry() for n, p in result.packages.items()}
            packages.update(resolved)
            row["packages"] = {n: e["version"] for n, e in sorted(resolved.items())}
            plan.remove[f.key] = source[f.key]
        rows.append(row)
    if packages != before:
        plan.manifest = render_manifest(packages)
    return rows, plan


async def run_migration(
    *,
    apply: bool = False,
    workspace_id: str | None = None,
    after: str | None = None,
    batch_size: int = 100,
    limit: int | None = None,
    resolve_packages: bool = True,
    _pockets: Any = None,
    _resolve: Callable[..., Any] | None = None,
    _keeps_client_bundle: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Scan source-map site pockets, classify, and (with ``apply``) migrate them.

    Idempotent: a migrated pocket has no generator-owned keys left to find, and the
    write is a no-op when nothing changed. Resumable: pass the report's
    ``last_pocket_id`` as ``after``. ``resolve_packages=False`` skips the registry
    (dry run only): CONVERTIBLE findings are then reported unresolved.
    """
    if _pockets is None:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        _pockets = pockets_service
    if _resolve is None:
        from pocketpaw_ee.sites.dependency_resolver import resolve_dependencies

        _resolve = resolve_dependencies
    if _keeps_client_bundle is None:
        from pocketpaw_ee.sites.service import _resolve_keeps_client_bundle

        _keeps_client_bundle = _resolve_keeps_client_bundle
    if apply and not resolve_packages:
        raise ValueError("apply needs package resolution; drop --no-resolve")

    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "scanned": 0,
        "affected_pockets": 0,
        "migrated_pockets": 0,
        "summary": {c.value: 0 for c in Classification},
        "pockets": [],
        "errors": [],
        "last_pocket_id": after,
    }
    cursor = after
    while limit is None or report["scanned"] < limit:
        page_size = batch_size if limit is None else min(batch_size, limit - report["scanned"])
        page = await _pockets.scan_source_site_pockets(
            engines=MIGRATION_ENGINES,
            after_id=cursor,
            limit=page_size,
            workspace_id=workspace_id,
        )
        if not page:
            break
        for pocket in page:
            cursor = pocket["id"]
            report["scanned"] += 1
            report["last_pocket_id"] = cursor
            await _migrate_one(
                pocket, report, apply, resolve_packages, _pockets, _resolve, _keeps_client_bundle
            )
        if len(page) < page_size:
            break
    return report


async def _migrate_one(
    pocket: Mapping[str, Any],
    report: dict[str, Any],
    apply: bool,
    resolve_packages: bool,
    pockets: Any,
    resolve: Callable[..., Any],
    keeps_client_bundle: Callable[[Mapping[str, Any]], bool],
) -> None:
    findings = classify_source(
        pocket["engine"],
        pocket.get("source"),
        keeps_client_bundle=keeps_client_bundle(pocket),
        dynamic=pocket.get("pattern") == "dynamic",
    )
    if not findings:
        return
    report["affected_pockets"] += 1
    entry: dict[str, Any] = {
        "pocket_id": pocket["id"],
        "workspace": pocket["workspace"],
        "engine": pocket["engine"],
        "files": [],
        "applied": False,
    }
    report["pockets"].append(entry)
    try:
        if resolve_packages:
            rows, plan = await _resolve_conversions(pocket, findings, resolve)
        else:
            rows = [
                {
                    "file": f.key,
                    "kind": f.kind,
                    "class": f.classification.value,
                    "action": f.action,
                    "reason": f.reason,
                    **({"requests": dict(f.requests)} if f.requests else {}),
                }
                for f in findings
            ]
            plan = _PocketPlan()
        entry["files"] = rows
        for row in rows:
            report["summary"][row["class"]] += 1
        if apply and (plan.remove or plan.manifest is not None):
            changed = await pockets.migrate_legacy_build_shell(
                pocket["id"],
                workspace_id=pocket["workspace"],
                remove=plan.remove,
                manifest=plan.manifest,
                author=MIGRATION_AUTHOR,
            )
            entry["applied"] = bool(changed)
            if changed:
                report["migrated_pockets"] += 1
    except Exception as exc:  # noqa: BLE001 — one bad pocket must not stop the run
        logger.warning("legacy_build_shell: pocket %s failed", pocket["id"], exc_info=True)
        report["errors"].append(
            {"pocket_id": pocket["id"], "error": f"{type(exc).__name__}: {exc}"}
        )


__all__ = [
    "Classification",
    "Finding",
    "MIGRATION_AUTHOR",
    "MIGRATION_ENGINES",
    "classify_source",
    "generator_owned_keys",
    "generator_owned_keys_message",
    "reserved_path_message",
    "run_migration",
]
