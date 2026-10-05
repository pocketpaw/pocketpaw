# ee/pocketpaw_ee/cloud/mandates/patrols.py — the mandate PATROLS.
#
# A patrol is an async callable that senses a mandate's surface and returns
# Sighting DRAFTS (plain dicts). ``service.run_patrols`` persists them, deduped
# on ``service._dedup_signal``; patrols never touch the store, which keeps
# service.py the sole Beanie importer. ``run_patrols`` passes ``workspace_id`` /
# ``user_id`` / ``upstream`` only to patrols whose signature names them.
#
#   * ``deps``     — the bound repo's manifest (pyproject.toml / package.json)
#                    against ``KNOWN_STALE``, a hardcoded demo table (the parse
#                    and sighting plumbing are real; the advisory data is not).
#   * ``issues``   — OPEN issues on the bound repo's GitLab project through
#                    ``connectors_service.execute``, one sighting per issue.
#   * ``upstream`` — commits on a pinned GitHub dependency since the pin: the
#                    ``rev`` comes from a TOML pin file in the bound repo, the
#                    delta from ``gh api repos/<repo>/compare/<pin>...HEAD``.
#                    One summary sighting per repo plus up to 5 per-area ones,
#                    keyed on repo + pin + upstream head so a quiet day files
#                    nothing new.
#   * ``feedback`` — intake only (``service.file_feedback``); no callable here.
#
# Failure posture: a patrol never raises into the shift trigger. ``deps`` and
# ``issues`` degrade to zero sightings; ``upstream`` files one severity-1
# sighting naming the problem (gh missing, bad pin file, API error), since a
# silent watch on a pinned engine is worse than a noisy one.
#
# Security: manifests and pin files are DATA, parsed with tomllib/json, never
# executed. External calls go through injectable seams (``execute``, ``gh``) with
# argv lists, never a shell; the upstream repo is validated as owner/name at the
# DTO and the pin as hex before either reaches a ``gh api`` path.

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections import defaultdict
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
    user_id: str | None = None,
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
    bound repo's directory name (the common ``repo-dir == project`` convention),
    which is WRONG for any namespaced project — a warning fires so the operator
    knows to bind an explicit ``project='group/name'``. ``user_id`` is the actor
    the connector call is attributed to (the scheduler passes its system actor).
    ``execute`` is the connector-execute callable, injected so tests mock the
    payload; the live caller leaves it ``None`` to use ``connectors_service``.

    Resilience: a connector failure, an unbound connector, a non-success response,
    or a malformed payload all yield ZERO sightings — the patrol NEVER raises into
    the shift trigger (same contract as ``deps``)."""
    proj = project
    if not proj:
        proj = Path(repo_id).expanduser().name
        # The dir-name default is a best-effort convenience: a namespaced GitLab
        # project (``group/name``) won't match a bare dir name and ``list_issues``
        # returns zero issues with NO error. Tell the operator to bind it.
        logger.warning(
            "issues patrol: no explicit project bound — defaulting to repo dir name %r. "
            "A namespaced GitLab project will return zero sightings silently; "
            "bind project='group/name' on the mandate surface.",
            proj,
        )
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
            user_id=user_id,
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


async def _default_execute(
    workspace_id: str, name: str, body: dict[str, Any], *, user_id: str | None = None
) -> Any:
    """The live connector-execute seam — the real ``connectors_service.execute``.

    Imported lazily so the patrols module stays importable without the connectors
    package wired (and so tests that inject ``execute`` never touch it). ``user_id``
    is threaded to the connector so a scheduler-fired patrol is attributed to its
    system actor, not ``None``."""
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest

    return await connectors_service.execute(
        workspace_id, name, ExecuteActionRequest.model_validate(body), user_id=user_id
    )


# ---------------------------------------------------------------------------
# ``upstream`` — commits on a pinned GitHub dependency since the pin. The pin is
# the ``rev`` of the ``git = "https://github.com/<repo>"`` dependency in a TOML
# pin file (a Cargo.toml) inside the bound repo; the delta is one ``gh api``
# compare call (first page only, up to 250 commits). ``gh`` is injected so
# tests never hit the network.
# ---------------------------------------------------------------------------

# A gh executor: async (argv) -> (returncode, stdout, stderr).
GhFn = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

_GH_TIMEOUT = 60.0
_HEX_REV = re.compile(r"^[0-9a-f]{7,40}$")
_MAX_AREAS = 5
_MAX_AREA_COMMITS = 8
# ``fix(render): ...`` → "render"; ``feat: ...`` → "feat".
_CONVENTIONAL = re.compile(r"^(\w+)(?:\(([^)]+)\))?!?:\s")
# ``photocraft-text: ...`` / ``GPU: ...`` / ``Never crash: ...`` → that prefix.
_AREA_PREFIX = re.compile(r"^([A-Za-z0-9][\w.\-/ ]{0,30}?):\s")


async def _default_gh(argv: list[str]) -> tuple[int, str, str]:
    """The live gh seam — the develop station's argv-only subprocess runner.
    Imported lazily (the station module imports the foreman)."""
    from pocketpaw_ee.cloud.belt.develop_station import run_subprocess

    return await run_subprocess(argv, cwd=Path.home(), timeout=_GH_TIMEOUT)


def _upstream_severity(ahead: int) -> int:
    if ahead > 100:
        return 4
    if ahead > 20:
        return 3
    return 2


def _git_url_repo(url: str) -> str:
    """``https://github.com/Owner/Name.git/`` → ``owner/name`` (else "")."""
    url = url.strip().lower().rstrip("/").removesuffix(".git")
    prefix = "https://github.com/"
    return url[len(prefix) :] if url.startswith(prefix) else ""


def _find_rev(node: Any, repo: str) -> str | None:
    """Walk a parsed TOML tree for a dependency table whose ``git`` URL is
    ``repo`` and return its ``rev``. Crates from one repo share a rev (and a
    ``[patch]`` block repeats it), so the first match wins."""
    if isinstance(node, dict):
        git = node.get("git")
        if isinstance(git, str) and _git_url_repo(git) == repo.lower():
            rev = node.get("rev")
            if isinstance(rev, str):
                return rev.strip()
        for child in node.values():
            found = _find_rev(child, repo)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_rev(child, repo)
            if found:
                return found
    return None


def _read_pin(repo_root: Path, pin_file: str, repo: str) -> str:
    """The pinned rev for ``repo`` in ``pin_file`` (relative to the bound repo).
    Raises ``ValueError`` with an operator-facing reason on any problem."""
    root = repo_root.resolve()
    path = (root / pin_file).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"pin file {pin_file} is outside the bound repo")
    if not path.is_file():
        raise ValueError(f"pin file {pin_file} not found in the bound repo")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ValueError(f"pin file {pin_file} is not readable TOML") from None
    rev = _find_rev(data, repo)
    if not rev:
        raise ValueError(f"no git dependency on github.com/{repo} with a rev in {pin_file}")
    if not _HEX_REV.match(rev):
        raise ValueError(f"the rev for {repo} in {pin_file} is not a commit sha")
    return rev


def _commit_area(title: str) -> str:
    m = _CONVENTIONAL.match(title)
    if m:
        return (m.group(2) or m.group(1)).strip().lower()
    m = _AREA_PREFIX.match(title)
    if m:
        return m.group(1).strip().lower()
    return "other"


def _upstream_error(repo: str, code: str, problem: str) -> SightingDraft:
    """The single severity-1 sighting a broken watch files. ``code`` is a stable
    reason so a persistent failure dedupes instead of re-filing every shift."""
    return {
        "patrol": "upstream",
        "severity": 1,
        "summary": f"{repo}: upstream watch failed: {problem}"[:280],
        "evidence": {"repo": repo, "error": problem, "dedup_key": f"{repo}!{code}"},
    }


async def _watch_upstream(
    repo_root: Path, repo: str, pin_file: str, gh: GhFn
) -> list[SightingDraft]:
    try:
        pin = _read_pin(repo_root, pin_file, repo)
    except ValueError as exc:
        return [_upstream_error(repo, "pin-file", str(exc))]

    argv = ["gh", "api", f"repos/{repo}/compare/{pin}...HEAD"]
    try:
        code, out, err = await gh(argv)
    except FileNotFoundError:
        return [_upstream_error(repo, "gh-missing", "the gh CLI is not installed")]
    except Exception as exc:  # noqa: BLE001 — a seam failure must not wedge the shift
        return [
            _upstream_error(repo, f"gh-failed@{pin[:7]}", f"gh api raised {type(exc).__name__}")
        ]
    if code != 0:
        first = (err or out or "").strip().splitlines()
        reason = first[0][:160] if first else f"exit {code}"
        return [_upstream_error(repo, f"gh-failed@{pin[:7]}", f"gh api failed: {reason}")]
    try:
        data = json.loads(out)
        ahead = int(data.get("ahead_by") or 0)
        commits = [c for c in (data.get("commits") or []) if isinstance(c, dict)]
    except (ValueError, TypeError, AttributeError):
        return [
            _upstream_error(repo, f"bad-json@{pin[:7]}", "gh api returned an unexpected payload")
        ]
    if ahead <= 0:
        return []

    # Upstream head: the last commit when the first page holds them all, else
    # the (short) head sha at the end of ``permalink_url``.
    if commits and len(commits) >= int(data.get("total_commits") or ahead):
        head = str(commits[-1].get("sha") or "")
    else:
        head = str(data.get("permalink_url") or "").rsplit(":", 1)[-1]
    head = head or f"+{ahead}"
    short = pin[:7]
    key = f"{repo}@{pin}..{head}"

    drafts: list[SightingDraft] = [
        {
            "patrol": "upstream",
            "severity": _upstream_severity(ahead),
            "summary": f"{repo}: {ahead} commits since pin {short}",
            "evidence": {
                "repo": repo,
                "pin_file": pin_file,
                "pin": pin,
                "head": head,
                "ahead_by": ahead,
                "compare_url": data.get("html_url"),
                "source": "github:compare",
                "dedup_key": key,
            },
        }
    ]

    areas: dict[str, list[dict[str, str]]] = defaultdict(list)
    for c in commits:
        title = str((c.get("commit") or {}).get("message") or "").strip().split("\n", 1)[0]
        if title:
            areas[_commit_area(title)].append({"sha": str(c.get("sha") or "")[:7], "title": title})
    # Biggest areas first; the catch-all "other" goes last.
    ranked = sorted(areas.items(), key=lambda kv: (kv[0] == "other", -len(kv[1]), kv[0]))
    for area, items in ranked[:_MAX_AREAS]:
        drafts.append(
            {
                "patrol": "upstream",
                "severity": 2,
                "summary": f"{repo} [{area}]: {len(items)} commit(s) since pin {short}"[:280],
                "evidence": {
                    "repo": repo,
                    "area": area,
                    "head": head,
                    "commits": items[:_MAX_AREA_COMMITS],
                    "source": "github:compare",
                    "dedup_key": f"{key}#{area}",
                },
            }
        )
    return drafts


async def upstream_patrol(
    repo_id: str,
    *,
    upstream: list[dict[str, Any]] | None = None,
    gh: GhFn | None = None,
) -> list[SightingDraft]:
    """The ``upstream`` patrol — report new commits on each pinned GitHub
    dependency in ``upstream`` (``[{repo, pin_file}]``, the mandate's watch list).

    Per watch: no new commits → nothing; new commits → one summary sighting
    (severity 2 / 3 / 4 at 1-20 / 21-100 / >100 commits) plus up to 5 area
    sightings citing up to 8 commits each; any failure → one severity-1 sighting.
    Never raises."""
    root = Path(repo_id).expanduser()
    runner = gh or _default_gh
    drafts: list[SightingDraft] = []
    for watch in upstream or []:
        repo = str(watch.get("repo") or "").strip()
        pin_file = str(watch.get("pin_file") or "").strip()
        if not repo or not pin_file:
            continue
        if not root.is_dir():
            drafts.append(_upstream_error(repo, "repo-missing", "the bound repo path is missing"))
            continue
        drafts.extend(await _watch_upstream(root, repo, pin_file, runner))
    return drafts


# Patrol registry — the service iterates this on a shift trigger. ``feedback``
# is intake-only (service.file_feedback), so it doesn't appear here. The
# ``issues`` patrol needs the workspace + project context the ``deps`` patrol
# doesn't; ``service.run_patrols`` inspects each patrol's signature and passes
# ``workspace_id`` only to the patrols that accept it (backward-compatible).
PATROLS: dict[str, PatrolFn] = {
    "deps": deps_patrol,
    "issues": issues_patrol,  # type: ignore[dict-item] — wider signature, called via kwargs
    "upstream": upstream_patrol,  # type: ignore[dict-item] — wider signature, called via kwargs
}


__all__ = [
    "KNOWN_STALE",
    "PATROLS",
    "ConnectorExecuteFn",
    "GhFn",
    "PatrolFn",
    "SightingDraft",
    "deps_patrol",
    "issues_patrol",
    "upstream_patrol",
]
