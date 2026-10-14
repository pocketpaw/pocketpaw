# ee/pocketpaw_ee/cloud/mandates/patrols.py — the mandate PATROLS.
#
# A patrol is an async callable that senses a mandate's surface and returns
# Sighting DRAFTS (plain dicts). ``service.run_patrols`` persists them, deduped
# on ``service._dedup_signal``; patrols never touch the store, which keeps
# service.py the sole Beanie importer. ``run_patrols`` passes ``workspace_id`` /
# ``user_id`` / ``upstream`` / ``issue_labels`` only to patrols whose signature
# names them.
#
#   * ``deps``     — the bound repo's manifest (pyproject.toml / package.json)
#                    against ``KNOWN_STALE``, a hardcoded demo table (the parse
#                    and sighting plumbing are real; the advisory data is not).
#   * ``issues``   — OPEN issues on the bound repo's tracker, one sighting per
#                    issue. A GitHub ``origin`` reads ``gh issue list`` and takes
#                    issues carrying a charter ``issue_labels`` label (none =
#                    nothing), keyed ``github:<owner>/<repo>#<n>`` (also the
#                    source key hand-filed feedback sightings use, so the
#                    service dedupes across both). Each ``sighted`` issue that
#                    closed or left the line comes back as a resolve marker
#                    (``_departures``; one cached ``gh issue view`` when it is
#                    not on the open list). Any other origin reads the GitLab
#                    project through ``connectors_service.execute``. Backlog
#                    reads, one cached gh list each per repo per tick
#                    (``_gh_rows``): ``issue_states`` / ``closed_issues``,
#                    ``open_pr_closes`` (each issue an OPEN PR closes),
#                    ``open_issues`` (also Clef's duplicate check), and
#                    ``open_dependencies``: an issue whose live body names a
#                    ``Depends on`` issue still open is blocked (fail safe).
#                    ``flag_needs_spec`` comments on a ``github:`` source issue
#                    and labels it ``needs-spec`` (once) for the auto plan gate.
#   * ``upstream`` — commits on a pinned GitHub dependency since the pin: the
#                    ``rev`` comes from a TOML pin file in the bound repo, the
#                    delta from ``gh api repos/<repo>/compare/<pin>...HEAD``.
#                    One summary sighting per repo plus up to 5 per-area ones,
#                    keyed on repo + pin + upstream head so a quiet day files
#                    nothing new.
#   * ``feedback`` — intake only (``service.file_feedback``); no callable here.
#
# Failure posture: a patrol never raises into the shift trigger. ``deps`` and
# ``issues`` degrade to zero sightings (an unsafe GitHub slug never reaches gh);
# ``upstream`` files one severity-1 sighting naming the problem (gh missing, bad
# pin file, API error), since a silent watch on a pinned engine is worse than a
# noisy one.
#
# Security: manifests and pin files are DATA, parsed with tomllib/json, never
# executed. External calls go through injectable seams (``execute``, ``gh``) with
# argv lists, never a shell; the upstream repo is validated as owner/name at the
# DTO and the pin as hex before either reaches a ``gh api`` path.

from __future__ import annotations

import json
import logging
import re
import time
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
    issue_labels: list[str] | None = None,
    gh: GhFn | None = None,
    sighted: list[str] | None = None,
) -> list[SightingDraft]:
    """The ``issues`` patrol — flag OPEN issues on the bound repo's tracker.

    A bound repo whose ``origin`` is on GitHub goes to ``_github_issues``
    (``gh issue list``, filtered on the charter's ``issue_labels``), which also
    marks each ``sighted`` issue (the line's unresolved ``github:`` sources) that
    closed or left the line; any other repo keeps the GitLab path below.

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
    runner_gh = gh or _default_gh
    slug = await _github_slug(repo_id, runner_gh)
    if slug is not None:
        return await _github_issues(slug, issue_labels or [], runner_gh, sighted)
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


# The GitHub half of ``issues``: open issues through ``gh issue list`` (argv,
# scrubbed env), taken when they carry a charter ``issue_labels`` label. The
# source key ``github:<owner>/<repo>#<n>`` is also the dedup key, so an issue a
# human already filed through the feedback intake is not sighted twice.
_GH_ISSUE_LIMIT = "100"


async def _github_slug(repo_id: str, gh: GhFn) -> str | None:
    """``owner/repo`` when the bound repo's ``origin`` is on GitHub, ``""`` when
    it is but the slug is unsafe for ``-R`` (a ``.``/``..`` or dash-led part),
    ``None`` when it is not GitHub (or unreadable): the GitLab path."""
    from pocketpaw_ee.cloud.belt.cloud_station import _GH_SLUG

    try:
        code, out, _ = await gh(
            ["git", "-C", str(Path(repo_id).expanduser()), "remote", "get-url", "origin"]
        )
    except Exception:  # noqa: BLE001 — no git, no origin: not a GitHub repo
        return None
    found = _GH_SLUG.search(out.strip()) if code == 0 else None
    if not found:
        return None
    slug = found.group(1)
    if any(p in {".", ".."} or p.startswith("-") for p in slug.split("/")):
        logger.warning("issues patrol: unsafe GitHub slug %r — zero sightings", slug)
        return ""
    return slug


async def _github_issues(
    slug: str, labels: list[str], gh: GhFn, sighted: list[str] | None = None
) -> list[SightingDraft]:
    """One draft per OPEN issue on ``slug`` carrying a label in ``labels``
    (case-insensitive), plus a resolve marker per ``sighted`` issue that left
    the line (``_departures``). No labels = nothing (the charter opts in). A gh
    failure or a malformed payload is zero drafts and resolves nothing."""
    wanted = {label.strip().lower() for label in labels if label.strip()}
    if not slug or not wanted:
        return []
    argv = [
        "gh",
        "issue",
        "list",
        "-R",
        slug,
        "--state",
        "open",
        "--limit",
        _GH_ISSUE_LIMIT,
        "--json",
        "number,title,body,labels,url",
    ]
    try:
        code, out, err = await gh(argv)
        issues = json.loads(out) if code == 0 else None
    except Exception:  # noqa: BLE001 — a gh failure must not wedge the shift
        logger.warning("issues patrol: gh issue list failed for %s", slug, exc_info=True)
        return []
    if not isinstance(issues, list):
        logger.info(
            "issues patrol: gh issue list %s gave nothing (exit %s): %s",
            slug,
            code,
            (err or "")[-200:],
        )
        return []
    drafts: list[SightingDraft] = []
    open_labels: dict[int, set[str]] = {}
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            continue
        names = [
            str(lb.get("name") or "") for lb in issue.get("labels") or [] if isinstance(lb, dict)
        ]
        open_labels[issue["number"]] = {n.lower() for n in names}
        if not wanted & {n.lower() for n in names}:
            continue
        n, title = issue["number"], str(issue.get("title") or "").strip()
        body = str(issue.get("body") or "").strip()
        key = f"github:{slug}#{n}"
        drafts.append(
            {
                "patrol": "issues",
                "severity": _issue_severity({"labels": names}),
                "summary": f"GitHub issue #{n}: {title}\n{body}"[:280],
                "evidence": {
                    "number": n,
                    "title": title,
                    "labels": names,
                    "url": issue.get("url"),
                    "text": body[:4000],
                    "source": key,
                    "dedup_key": key,
                },
            }
        )
    return drafts + await _departures(slug, wanted, open_labels, sighted or [], gh)


# A pass views at most this many issues it could not read off a list; the views
# are cached per issue for a tick (``_GH_ROWS_TTL``), a failed one included.
_MAX_ISSUE_VIEWS = 20
_ISSUE_VIEWS: dict[tuple[str, int], tuple[float, dict[str, Any] | None]] = {}


async def _issue_view(slug: str, n: int, gh: GhFn, budget: list[int]) -> dict[str, Any] | None:
    """``gh issue view <n> -R <slug> --json state,labels,body`` (PRs answer too),
    cached per tick. ``budget`` is the pass's remaining views (``[k]``); a cache
    hit spends none. None when the slug is unsafe, the budget is spent or gh
    fails: the caller reads that as unknown. Never raises."""
    slug = slug.lower()
    hit = _ISSUE_VIEWS.get((slug, n))
    if hit is not None and time.monotonic() - hit[0] < _GH_ROWS_TTL:
        return hit[1]
    if budget[0] < 1 or any(p in {".", ".."} or p.startswith("-") for p in slug.split("/")):
        return None
    budget[0] -= 1
    try:
        code, out, _ = await gh(
            ["gh", "issue", "view", str(n), "-R", slug, "--json", "state,labels,body"]
        )
        row = json.loads(out) if code == 0 else None
    except Exception:  # noqa: BLE001 — an unread issue is unknown, never a crash
        logger.warning("mandate: gh issue view %s#%s failed", slug, n, exc_info=True)
        row = None
    row = row if isinstance(row, dict) else None
    _ISSUE_VIEWS[(slug, n)] = (time.monotonic(), row)
    return row


async def _departures(
    slug: str, wanted: set[str], open_labels: dict[int, set[str]], sighted: list[str], gh: GhFn
) -> list[SightingDraft]:
    """A resolve marker ``{"patrol": "issues", "resolve": <source>, "reason":
    ...}`` per ``sighted`` ``github:<slug>#<n>`` no longer open on this line:
    ``issue-closed``, or ``issue-left-line`` when it is open without a line
    label. ``open_labels`` is the open list; a number missing from it costs one
    ``gh issue view``. Open and still labelled (past the list's limit), or a
    failed view, moves nothing."""
    out: list[SightingDraft] = []
    budget = [_MAX_ISSUE_VIEWS]
    for source in dict.fromkeys(sighted):
        found = _SPEC_SOURCE.match(source)
        if not found or found[1].lower() != slug.lower():
            continue
        n = int(found[2])
        labels = open_labels.get(n)
        if labels is None:
            view = await _issue_view(slug, n, gh, budget) or {}
            state = str(view.get("state") or "").lower()
            if not state:
                continue
            if state != "open":
                out.append({"patrol": "issues", "resolve": source, "reason": "issue-closed"})
                continue
            labels = {
                str(lb.get("name") or "").lower()
                for lb in view.get("labels") or []
                if isinstance(lb, dict)
            }
        if not wanted & labels:
            out.append({"patrol": "issues", "resolve": source, "reason": "issue-left-line"})
    return out


# The bound repo's gh lists the backlog reads: rows cached per slug for a
# scheduler tick, so the mandates on one repo share each call. Issue states feed
# the foreman's ``Depends on:`` notes; open PRs the in-flight rule.
_ISSUE_STATES: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_OPEN_PRS: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_OPEN_ISSUES: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_GH_ROWS_TTL = 60.0
_GH_STATE_LIMIT = "500"
_GH_PR_LIMIT = "200"


async def _gh_rows(
    cache: dict[str, tuple[float, list[dict[str, Any]]]],
    repo_id: str,
    argv: list[str],
    gh: GhFn | None,
) -> tuple[str, list[dict[str, Any]]]:
    """``(slug, rows)``: the JSON rows ``argv`` (``gh <noun> list``, ``-R <slug>``
    added) prints for the bound repo, cached in ``cache`` for ``_GH_ROWS_TTL``.
    No slug (not GitHub, or unsafe) reads ``("", [])``; a gh failure, no rows.
    Never raises."""
    runner = gh or _default_gh
    slug = await _github_slug(repo_id, runner)
    if not slug:
        return "", []
    hit = cache.get(slug)
    if hit is not None and time.monotonic() - hit[0] < _GH_ROWS_TTL:
        return slug, hit[1]
    try:
        code, out, _ = await runner([*argv[:3], "-R", slug, *argv[3:]])
        rows = json.loads(out) if code == 0 else []
    except Exception:  # noqa: BLE001 — a missing read never holds the shift
        logger.warning("mandate: %s failed for %s", " ".join(argv[:3]), slug, exc_info=True)
        rows = []
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    cache[slug] = (time.monotonic(), rows)
    return slug, rows


async def _slug_states(repo_id: str, gh: GhFn | None) -> tuple[str, dict[int, str]]:
    """``(slug, {number: "open"|"closed"})``: one cached ``gh issue list --state
    all`` on the bound repo. ``("", {})`` off GitHub; a gh failure, no states."""
    argv = ["gh", "issue", "list", "--state", "all", "--limit", _GH_STATE_LIMIT]
    slug, rows = await _gh_rows(_ISSUE_STATES, repo_id, [*argv, "--json", "number,state"], gh)
    states = {
        r["number"]: str(r.get("state") or "").lower()
        for r in rows
        if isinstance(r.get("number"), int)
    }
    return slug, states


async def issue_states(repo_id: str, gh: GhFn | None = None) -> dict[int, str]:
    """``{number: "open"|"closed"}`` for the bound repo's GitHub issues; empty
    when the repo is not on GitHub (or its slug is unsafe) or gh fails. Never
    raises."""
    return (await _slug_states(repo_id, gh))[1]


async def closed_issues(repo_id: str, gh: GhFn | None = None) -> set[str]:
    """``{"github:<slug>#<n>"}`` for each CLOSED issue on the bound repo, keyed
    like an issue sighting's ``source``. Empty off GitHub or when gh fails (an
    issue past the list's limit reads as not closed). Never raises."""
    slug, states = await _slug_states(repo_id, gh)
    return {f"github:{slug}#{n}" for n, state in states.items() if state == "closed"}


# An issue's ``Depends on`` section: the line (``**Depends on:** #4``, ``Depends
# on #412``, a ``## Depends on`` heading) and the lines under it, up to a blank
# line or a heading; a heading with no reference may sit a blank line above its
# list. A reference is ``#N`` (the issue's own repo), ``owner/repo#N`` or an
# issue / PR URL; the first ``_DEPENDS_REFS_CAP`` count.
_DEPENDS_HEAD = re.compile(r"^[\s>*_#-]*depends\s+on\b[\s*_:]*(.*)$", re.IGNORECASE)
_DEPENDS_REF = re.compile(
    r"(?:github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/|([\w.-]+/[\w.-]+)?#)(\d+)"
)
_SECTION_END = re.compile(r"^(?:#{1,6}\s|\*\*[^*]+\*\*:?$)")
_DEPENDS_REFS_CAP = 10


def depends_on_refs(body: str, slug: str) -> list[str]:
    """Every ``owner/repo#N`` the ``Depends on`` sections of ``body`` name,
    lowercased, in order; a bare ``#N`` is ``slug``'s."""
    refs: list[str] = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        head = _DEPENDS_HEAD.match(line)
        if not head:
            continue
        section, rest = [head[1]], lines[i + 1 :]
        if not _DEPENDS_REF.search(head[1]):
            while rest and not rest[0].strip():
                rest = rest[1:]
        for nxt in rest:
            if not nxt.strip() or _SECTION_END.match(nxt.strip()) or _DEPENDS_HEAD.match(nxt):
                break
            section.append(nxt)
        for m in _DEPENDS_REF.finditer("\n".join(section)):
            refs.append(f"{(m[1] or m[2] or slug).lower()}#{m[3]}")
    return list(dict.fromkeys(refs))[:_DEPENDS_REFS_CAP]


async def open_dependencies(
    repo_id: str, sources: list[str], gh: GhFn | None = None
) -> dict[str, list[str]]:
    """``{source: refs}`` for each ``github:<owner>/<repo>#<n>`` in ``sources``
    whose LIVE issue body names a ``Depends on`` issue that is not closed yet.

    The body comes off the bound repo's cached open-issue list, else one cached
    ``gh issue view``; a bound-repo ref's state off the cached ``--state all``
    list, else a view (a cross-repo ref always). Empty when the bound repo is
    not on GitHub. Fail safe: an unreadable body blocks as ``["?"]`` and an
    unknown ref state counts as open. Never raises."""
    runner = gh or _default_gh
    bound, states = await _slug_states(repo_id, runner)
    bound = bound.lower()
    if not bound:
        return {}
    bodies = {
        r["number"]: str(r.get("body") or "")
        for r in await open_issues(repo_id, runner)
        if isinstance(r.get("number"), int)
    }
    budget = [_MAX_ISSUE_VIEWS]
    blocked: dict[str, list[str]] = {}
    for source in dict.fromkeys(sources):
        found = _SPEC_SOURCE.match(source)
        if not found:
            continue
        slug, n = found[1].lower(), int(found[2])
        body = bodies.get(n) if slug == bound else None
        if body is None:
            view = await _issue_view(slug, n, runner, budget)
            body = None if view is None else str(view.get("body") or "")
        if body is None:
            blocked[source] = ["?"]
            continue
        waiting = []
        for ref in depends_on_refs(body, slug):
            ref_slug, ref_n = ref.rsplit("#", 1)
            state = states.get(int(ref_n)) if ref_slug == bound else None
            if state is None:
                view = await _issue_view(ref_slug, int(ref_n), runner, budget) or {}
                state = str(view.get("state") or "").lower()
            if state not in ("closed", "merged"):
                waiting.append(ref)
        if waiting:
            blocked[source] = waiting
    return blocked


async def open_issues(repo_id: str, gh: GhFn | None = None) -> list[dict[str, Any]]:
    """The bound repo's OPEN GitHub issues (``number``, ``title``, ``body``,
    ``labels``): the candidates Clef checks a Belt finding against for a
    duplicate, and the live labels the lead planner reads. [] when the repo is
    not on GitHub or gh fails. Never raises."""
    argv = ["gh", "issue", "list", "--state", "open", "--limit", _GH_PR_LIMIT]
    fields = "number,title,body,labels"
    _, rows = await _gh_rows(_OPEN_ISSUES, repo_id, [*argv, "--json", fields], gh)
    return rows


async def open_pr_closes(repo_id: str, gh: GhFn | None = None) -> dict[str, int]:
    """``{"github:<slug>#<n>": pr}``: each issue an OPEN PR on the bound repo
    closes (``Closes`` / ``Fixes`` / ``Resolves #n`` in its body), keyed like an
    issue sighting's ``source``, to the lowest such PR. Empty when the repo is
    not on GitHub or gh fails. Never raises."""
    from pocketpaw_ee.cloud.belt.cloud_station import closed_by

    argv = ["gh", "pr", "list", "--state", "open", "--limit", _GH_PR_LIMIT]
    slug, rows = await _gh_rows(_OPEN_PRS, repo_id, [*argv, "--json", "number,body"], gh)
    found: dict[str, int] = {}
    for r in sorted(
        (r for r in rows if isinstance(r.get("number"), int)), key=lambda r: r["number"]
    ):
        for n in closed_by(str(r.get("body") or "")):
            found.setdefault(f"github:{slug}#{n}", r["number"])
    return found


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


NEEDS_SPEC = "needs-spec"
_SPEC_SOURCE = re.compile(r"^github:([\w.-]+/[\w.-]+)#(\d+)$")


async def flag_needs_spec(source: str, reason: str, gh: GhFn | None = None) -> bool:
    """Comment ``reason`` on the ``github:<owner>/<repo>#<n>`` issue ``source``
    names and label it ``needs-spec``, so a human or a later spec pass rewrites
    it. Once per issue: one already labelled is left alone. True when it
    commented; never raises."""
    found = _SPEC_SOURCE.match(source or "")
    if not found or any(p in {".", ".."} or p.startswith("-") for p in found[1].split("/")):
        return False
    slug, number = found.groups()
    runner = gh or _default_gh
    try:
        code, out, _ = await runner(["gh", "issue", "view", number, "-R", slug, "--json", "labels"])
        labels = json.loads(out).get("labels") if code == 0 else None
        if not isinstance(labels, list) or NEEDS_SPEC in {lb.get("name") for lb in labels}:
            return False
        # Creating a label that exists fails harmlessly.
        await runner(["gh", "label", "create", NEEDS_SPEC, "-R", slug, "--color", "fbca04"])
        code, _, err = await runner(
            ["gh", "issue", "comment", number, "-R", slug, "--body", reason]
        )
        if code != 0:
            logger.warning("needs-spec: comment on %s#%s failed: %s", slug, number, err[-200:])
            return False
        await runner(["gh", "issue", "edit", number, "-R", slug, "--add-label", NEEDS_SPEC])
        return True
    except Exception:  # noqa: BLE001 — a missed flag never fails the gate
        logger.warning("needs-spec: flagging %s failed", source, exc_info=True)
        return False


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
