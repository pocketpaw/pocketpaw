# ee/pocketpaw_ee/cloud/mandates/patrols.py
# Created: 2026-06-11 (feat/belt-mandates, slice 2 — patrols).
#
# The PATROL framework — a patrol is an async callable that senses a mandate's
# surface and produces Sighting DRAFTS (plain dicts; the service persists them
# as SightingDoc rows — patrols never touch the store, keeping service.py the
# sole Beanie importer).
#
# Two patrols ship in v1:
#   * ``deps``    — parses the bound repo's manifest (pyproject.toml /
#                   package.json) and flags entries found in a DETERMINISTIC
#                   STUB TABLE of known-stale / CVE-carrying packages.
#                   >>> DEMO-BAR CONCESSION: the stale/CVE data is a hardcoded
#                   table, not a live advisory feed. The patrol's parse +
#                   sighting plumbing is production-shaped; only the data
#                   source is stubbed. <<<
#   * ``feedback`` — intake-only (no sense loop); humans file sightings via
#                   ``POST /belt/mandates/{id}/feedback`` (service.file_feedback).
#                   It has no callable here on purpose.
#
# Security: the repo manifest is DATA — parsed with tomllib/json, never
# executed or shell-interpolated. A repo path that doesn't resolve or parse
# yields zero sightings (the patrol never raises into the shift trigger).

from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path
from typing import Any, Awaitable, Callable

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


# Patrol registry — the service iterates this on a shift trigger. ``feedback``
# is intake-only (service.file_feedback), so it doesn't appear here.
PATROLS: dict[str, PatrolFn] = {
    "deps": deps_patrol,
}


__all__ = ["KNOWN_STALE", "PATROLS", "PatrolFn", "SightingDraft", "deps_patrol"]
