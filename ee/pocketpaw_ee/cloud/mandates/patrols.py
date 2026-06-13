# ee/pocketpaw_ee/cloud/mandates/patrols.py
# Created: 2026-06-11 (feat/belt-mandates, slice 2 — patrols).
#
# Updated: 2026-06-13 (feat/patrol-engine) — added ``issues_patrol``, the FIRST
#   LIVE patrol: it reads a REAL signal (open issues on the bound repo's GitLab
#   project) through the existing ``connectors_service.execute`` cloud path and
#   emits a populated SightingDraft per open issue — not the hardcoded stub the
#   ``deps`` patrol uses. The external call is injectable so tests mock the
#   payload instead of hitting the network. The ``deps`` / ``feedback`` patrols
#   are unchanged.
#
# The PATROL framework — a patrol is an async callable that senses a mandate's
# surface and produces Sighting DRAFTS (plain dicts; the service persists them
# as SightingDoc rows — patrols never touch the store, keeping service.py the
# sole Beanie importer).
#
# Patrols that ship:
#   * ``deps``    — parses the bound repo's manifest (pyproject.toml /
#                   package.json) and flags entries found in a DETERMINISTIC
#                   STUB TABLE of known-stale / CVE-carrying packages.
#                   >>> DEMO-BAR CONCESSION: the stale/CVE data is a hardcoded
#                   table, not a live advisory feed. The patrol's parse +
#                   sighting plumbing is production-shaped; only the data
#                   source is stubbed. <<<
#   * ``issues``  — the FIRST LIVE patrol. Reads OPEN issues on the bound repo's
#                   GitLab project via ``connectors_service.execute`` (a real
#                   httpx call in CLOUD mode) and emits one SightingDraft per
#                   open issue. No hardcoded table — this is a genuine live feed.
#   * ``feedback`` — intake-only (no sense loop); humans file sightings via
#                   ``POST /belt/mandates/{id}/feedback`` (service.file_feedback).
#                   It has no callable here on purpose.
#
# Security: the repo manifest is DATA — parsed with tomllib/json, never
# executed or shell-interpolated. A repo path that doesn't resolve or parse
# yields zero sightings (the patrol never raises into the shift trigger). The
# issues patrol's connector call is wrapped the same way — a failure (unbound
# connector, network error, malformed payload) yields zero sightings.

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A sighting draft — what a patrol hands the service for persistence.
SightingDraft = dict[str, Any]
# A patrol: async (repo_path) -> drafts. Kept narrow on purpose; future patrols
# that need more surface context can grow the signature behind the registry.
PatrolFn = Callable[[str], Awaitable[list[SightingDraft]]]


# ---------------------------------------------------------------------------
# DEMO-BAR STUB TABLE — known-stale / CVE-carrying packages. Deterministic on
# purpose: the demo needs reproducible sightings, not a live advisory feed.
# Key = normalized package name; value = the advisory the patrol reports.
# ---------------------------------------------------------------------------

KNOWN_STALE: dict[str, dict[str, Any]] = {
    # Python
    "requests": {"latest": "2.32.3", "cve": "CVE-2024-35195", "severity": 3},
    "urllib3": {"latest": "2.2.2", "cve": "CVE-2024-37891", "severity": 3},
    "pyyaml": {"latest": "6.0.2", "cve": "CVE-2020-14343", "severity": 4},
    "jinja2": {"latest": "3.1.4", "cve": "CVE-2024-34064", "severity": 3},
    "cryptography": {"latest": "43.0.0", "cve": "CVE-2024-26130", "severity": 4},
    "pillow": {"latest": "10.4.0", "cve": "CVE-2023-50447", "severity": 5},
    # JavaScript
    "lodash": {"latest": "4.17.21", "cve": "CVE-2021-23337", "severity": 4},
    "axios": {"latest": "1.7.4", "cve": "CVE-2024-39338", "severity": 4},
    "minimist": {"latest": "1.2.8", "cve": "CVE-2021-44906", "severity": 5},
    "node-fetch": {"latest": "3.3.2", "cve": "CVE-2022-0235", "severity": 3},
    "express": {"latest": "4.19.2", "cve": "CVE-2024-29041", "severity": 3},
}

# PEP 508-ish dependency string → bare name ("requests>=2.0; extra" → "requests").
_PY_DEP_NAME = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _python_deps(repo: Path) -> list[str]:
    """Dependency names from pyproject.toml ([project].dependencies +
    [dependency-groups]). Empty list on a missing / unparseable file."""
    manifest = repo / "pyproject.toml"
    if not manifest.is_file():
        return []
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("deps patrol: unparseable pyproject.toml at %s", repo, exc_info=True)
        return []
    raw: list[str] = list(data.get("project", {}).get("dependencies", []) or [])
    for group in (data.get("dependency-groups") or {}).values():
        raw.extend(d for d in group if isinstance(d, str))
    names: list[str] = []
    for dep in raw:
        m = _PY_DEP_NAME.match(dep)
        if m:
            names.append(_normalize(m.group(1)))
    return names


def _js_deps(repo: Path) -> list[str]:
    """Dependency names from package.json (dependencies + devDependencies).
    Empty list on a missing / unparseable file."""
    manifest = repo / "package.json"
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("deps patrol: unparseable package.json at %s", repo, exc_info=True)
        return []
    names: list[str] = []
    for key in ("dependencies", "devDependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            names.extend(_normalize(n) for n in block)
    return names


async def deps_patrol(repo_id: str) -> list[SightingDraft]:
    """The ``deps`` patrol — flag manifest entries present in KNOWN_STALE.

    ``repo_id`` is the mandate surface's repo path. A path that doesn't resolve
    to a directory yields zero sightings (never raises — a broken surface must
    not wedge the shift trigger)."""
    repo = Path(repo_id).expanduser()
    if not repo.is_dir():
        logger.warning("deps patrol: repo %r not found — zero sightings", repo_id)
        return []

    seen: set[str] = set()
    drafts: list[SightingDraft] = []
    for name in [*_python_deps(repo), *_js_deps(repo)]:
        if name in seen or name not in KNOWN_STALE:
            continue
        seen.add(name)
        advisory = KNOWN_STALE[name]
        drafts.append(
            {
                "patrol": "deps",
                "severity": int(advisory["severity"]),
                "summary": (
                    f"{name} is stale/vulnerable ({advisory['cve']}) — "
                    f"latest is {advisory['latest']}"
                ),
                "evidence": {
                    "package": name,
                    "latest": advisory["latest"],
                    "cve": advisory["cve"],
                    "source": "demo-stub-table",
                },
            }
        )
    return drafts


# ---------------------------------------------------------------------------
# ``issues`` — the FIRST LIVE patrol. Reads OPEN issues on the bound repo's
# GitLab project through the existing connector execute path (a real httpx call
# in CLOUD mode), and emits one SightingDraft per open issue. The external call
# is injected so tests mock the payload; the live caller passes the real
# ``connectors_service.execute``.
# ---------------------------------------------------------------------------

# The connector + action the issues patrol reads. GitLab's ``list_issues`` is a
# CLOUD-mode bearer REST action (real httpx) the catalog already ships.
_ISSUES_CONNECTOR = "gitlab"
_ISSUES_ACTION = "list_issues"
# How many open issues a single patrol pass turns into sightings (cap so a noisy
# project doesn't flood the foreman in one shift).
_MAX_ISSUE_SIGHTINGS = 25

# A connector-execute callable: async (workspace_id, name, body, *, user_id) ->
# a result with ``.success`` / ``.data`` (the ExecuteActionResponse shape). Kept
# as a Protocol-free alias so the patrol stays decoupled from the connectors DTO.
ConnectorExecuteFn = Callable[..., Awaitable[Any]]


def _issue_severity(issue: dict[str, Any]) -> int:
    """Map a GitLab issue to a 1-5 severity from its labels — a ``critical`` /
    ``security`` / ``bug`` label lifts urgency; everything else is baseline 2.
    Deterministic so the sighting dedup key is stable across passes."""
    labels = {str(label).strip().lower() for label in (issue.get("labels") or [])}
    if labels & {"critical", "security", "p0", "blocker"}:
        return 5
    if labels & {"bug", "regression", "p1", "high"}:
        return 4
    if labels & {"p2", "medium"}:
        return 3
    return 2


async def issues_patrol(
    repo_id: str,
    *,
    workspace_id: str,
    project: str | None = None,
    connector: str = _ISSUES_CONNECTOR,
    execute: ConnectorExecuteFn | None = None,
) -> list[SightingDraft]:
    """The ``issues`` patrol — flag OPEN issues on the bound repo's GitLab project.

    Reads a LIVE signal: calls the ``gitlab`` connector's ``list_issues`` action
    through ``connectors_service.execute`` (a genuine httpx call in CLOUD mode),
    then emits one SightingDraft per OPEN issue. Unlike ``deps`` there is NO
    hardcoded table — the data is whatever the project's tracker reports right now.

    ``project`` is the GitLab project id / URL-encoded path; it defaults to the
    bound repo's directory name (the common ``repo-dir == project`` convention).
    ``execute`` is the connector-execute callable, injected so tests mock the
    payload; the live caller leaves it ``None`` to use ``connectors_service``.

    Resilience: a connector failure, an unbound connector, a non-success response,
    or a malformed payload all yield ZERO sightings — the patrol NEVER raises into
    the shift trigger (same contract as ``deps``)."""
    proj = project or Path(repo_id).expanduser().name
    if not proj:
        logger.warning(
            "issues patrol: no project resolvable from repo %r — zero sightings", repo_id
        )
        return []

    runner = execute or _default_execute
    try:
        result = await runner(
            workspace_id,
            connector,
            {
                "action": _ISSUES_ACTION,
                "params": {"project_id": proj, "state": "opened"},
                "scope": "workspace",
            },
        )
    except Exception:  # noqa: BLE001 — a connector failure must not wedge the shift
        logger.warning(
            "issues patrol: connector %r execute failed for project %r — zero sightings",
            connector,
            proj,
            exc_info=True,
        )
        return []

    if not getattr(result, "success", False):
        logger.info(
            "issues patrol: connector %r returned no success for project %r — zero sightings",
            connector,
            proj,
        )
        return []

    raw = getattr(result, "data", None)
    if not isinstance(raw, list):
        logger.info("issues patrol: unexpected payload shape for project %r — zero sightings", proj)
        return []

    drafts: list[SightingDraft] = []
    for issue in raw:
        if not isinstance(issue, dict):
            continue
        # Defensive: even though we asked for state=opened, skip anything closed.
        if str(issue.get("state") or "opened").lower() != "opened":
            continue
        title = str(issue.get("title") or "").strip()
        if not title:
            continue
        iid = issue.get("iid") if issue.get("iid") is not None else issue.get("id")
        drafts.append(
            {
                "patrol": "issues",
                "severity": _issue_severity(issue),
                "summary": f"Open issue: {title}"[:280],
                "evidence": {
                    "iid": iid,
                    "title": title,
                    "labels": list(issue.get("labels") or []),
                    "web_url": issue.get("web_url"),
                    "project": proj,
                    "source": "gitlab:list_issues",
                },
            }
        )
        if len(drafts) >= _MAX_ISSUE_SIGHTINGS:
            break
    return drafts


async def _default_execute(workspace_id: str, name: str, body: dict[str, Any]) -> Any:
    """The live connector-execute seam — the real ``connectors_service.execute``.

    Imported lazily so the patrols module stays importable without the connectors
    package wired (and so tests that inject ``execute`` never touch it)."""
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest

    return await connectors_service.execute(
        workspace_id, name, ExecuteActionRequest.model_validate(body)
    )


# Patrol registry — the service iterates this on a shift trigger. ``feedback``
# is intake-only (service.file_feedback), so it doesn't appear here. The
# ``issues`` patrol needs the workspace + project context the ``deps`` patrol
# doesn't; ``service.run_patrols`` inspects each patrol's signature and passes
# ``workspace_id`` only to the patrols that accept it (backward-compatible).
PATROLS: dict[str, PatrolFn] = {
    "deps": deps_patrol,
    "issues": issues_patrol,  # type: ignore[dict-item] — wider signature, called via kwargs
}


__all__ = [
    "KNOWN_STALE",
    "PATROLS",
    "ConnectorExecuteFn",
    "PatrolFn",
    "SightingDraft",
    "deps_patrol",
    "issues_patrol",
]
