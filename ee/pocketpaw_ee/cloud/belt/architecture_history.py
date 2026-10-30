# ee/pocketpaw_ee/cloud/belt/architecture_history.py — the architecture viewer's
# history: ``GET /belt/architecture/history?repo=`` lists the trunk's model
# commits, ``?at=<sha>`` on the architecture read (and the Code tab's read) reads
# the model as of one of them, and ``?diff=<sha>`` compares a read against one.
#
# The trunk is ``architecture_read``'s (``origin/dev``, else ``origin/main``), as
# that read last fetched it: nothing here fetches. History is the last
# ``HISTORY_LIMIT`` trunk commits touching ``docs/c4/``, newest first, as
# ``[{sha, date, subject, added, removed, changed}]``: the counts are
# ``architecture_diff``'s against the commit's first parent (a root commit, or a
# parent with no model, counts everything as added; ``None`` without a readable
# model). A commit's counts never change, so they are cached per (repo, sha).
#
# A commit is 404 ``belt.commit_not_found`` unless it is 7-40 hex characters
# (checked before any git call, so it is never an option) naming a commit
# reachable from the trunk. An ``at`` body has the live read's keys with that
# commit's full ``sha``, ``stale: False`` and ``live: None``, cached per (repo,
# full sha) with no TTL up to ``_CACHE_MAX`` (then dropped whole). Git missing
# or timing out is 503 ``belt.architecture_unavailable``, as the live read.
# Nothing here depends on the workspace (the repo allowlist is global), so no
# call takes one.

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import app_blueprint_read as abr
from .architecture_diff import diff_counts, diff_models
from .architecture_flows import load_flows
from .architecture_read import _BRANCHES, _git
from .executor import _re_resolve_repo
from .service import BeltConsoleError, read_repo_model

HISTORY_LIMIT = 50
_CACHE_MAX = 64
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
# ponytail: drop-all when full; an LRU if the viewer ever walks many commits
_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_COUNTS: dict[tuple[str, str], dict[str, int] | None] = {}  # bounded by _COUNTS_MAX, same drop
_COUNTS_MAX = 512


async def _trunk(repo: str) -> tuple[Path, str]:
    """``(path, origin/<branch>)``: the allowlisted repo and its fetched trunk ref."""
    path, _err = _re_resolve_repo(repo)
    if path is None:
        raise BeltConsoleError(404, "Repo not found.", "belt.repo_not_found")
    for branch in _BRANCHES:
        ref = f"origin/{branch}"
        if (await _git(path, "rev-parse", "--verify", "--quiet", ref))[0] == 0:
            return path, ref
    msg = "No C4 model in this repo. Add docs/c4/model.json on its trunk."
    raise BeltConsoleError(404, msg, "belt.architecture_not_found")


def _unavailable(err: str) -> BeltConsoleError:
    msg = f"Architecture history not readable yet: {err.strip()[:200]}"
    return BeltConsoleError(503, msg, "belt.architecture_unavailable")


async def read_history(repo: str) -> list[dict[str, Any]]:
    """The trunk's model commits with their counts, newest first (see the header)."""
    try:
        path, ref = await _trunk(repo)
        fmt = "--format=%H%x1f%cI%x1f%s"
        argv = ("log", "-n", str(HISTORY_LIMIT), fmt, ref, "--", "docs/c4/")
        code, out, err = await _git(path, *argv)
        if code != 0:
            raise _unavailable(err)
        rows = [line.split("\x1f", 2) for line in out.splitlines() if line]
        gate = asyncio.Semaphore(8)
        jobs = (_counts(path, s, gate) for s, _d, _m in rows)
        counts = await asyncio.gather(*jobs, return_exceptions=True)  # none left running
        if fail := next((c for c in counts if isinstance(c, BaseException)), None):
            raise fail
    except (OSError, RuntimeError) as exc:  # git missing, or it timed out
        raise _unavailable(str(exc)) from None
    return [
        {"sha": s, "date": d, "subject": m, **(c or dict.fromkeys(("added", "removed", "changed")))}
        for (s, d, m), c in zip(rows, counts, strict=True)
    ]


async def _counts(path: Path, sha: str, gate: asyncio.Semaphore) -> dict[str, int] | None:
    """``sha``'s ``{added, removed, changed}`` against its first parent, cached."""
    key = (str(path), sha)
    if key in _COUNTS:
        return _COUNTS[key]
    async with gate:
        _, model = await read_repo_model(path, sha)
        _, parent = await read_repo_model(path, f"{sha}^")
    counts = None if model is None else diff_counts(diff_models(_body(parent), _body(model)))
    if len(_COUNTS) >= _COUNTS_MAX:
        _COUNTS.clear()
    _COUNTS[key] = counts
    return counts


def _body(model: dict[str, Any] | None) -> dict[str, Any]:
    return model["model"] if model else {}


async def read_at(repo: str, at: str, *, gh: abr.Run | None = None) -> dict[str, Any]:
    """The architecture body as of trunk commit ``at`` (see the header)."""
    try:
        return await _read_at(repo, at, gh)
    except (OSError, RuntimeError) as exc:  # git missing, or it timed out
        raise _unavailable(str(exc)) from None


async def read_diff(repo: str, at: str, body: dict[str, Any]) -> dict[str, Any]:
    """``body`` (a read) plus ``diff``: its model against trunk commit ``at``'s."""
    try:
        old = await read_at(repo, at)
    except BeltConsoleError as exc:  # a bad sha gets its own code, apart from at='s
        code = "belt.diff_commit_not_found" if exc.code == "belt.commit_not_found" else exc.code
        raise BeltConsoleError(exc.status_code, exc.message, code) from None
    return {**body, "diff": diff_models(old["model"]["model"], body["model"]["model"])}


async def _read_at(repo: str, at: str, gh: abr.Run | None) -> dict[str, Any]:
    from pocketpaw_ee.cloud.mandates.patrols import _default_gh, _github_slug

    missing = BeltConsoleError(404, "No such commit on the trunk.", "belt.commit_not_found")
    if not _SHA_RE.fullmatch(at):
        raise missing
    path, ref = await _trunk(repo)
    code, sha, _ = await _git(path, "rev-parse", "--verify", "--quiet", f"{at}^{{commit}}")
    if code != 0:
        raise missing
    if hit := _CACHE.get((str(path), sha := sha.strip())):
        return hit
    code, _, err = await _git(path, "merge-base", "--is-ancestor", sha, ref)
    if code == 1:
        raise missing
    if code != 0:
        raise _unavailable(err)
    _, model = await read_repo_model(path, sha)
    if model is None:
        msg = "No readable C4 model at this commit."
        raise BeltConsoleError(404, msg, "belt.architecture_not_found")
    flows, warnings = await load_flows(path, sha, model)
    body = {
        "repo": str(path),
        "slug": await _github_slug(str(path), gh or _default_gh) or "",
        "sha": sha,
        "stale": False,
        "read_at": datetime.now(UTC).isoformat(),
        "model": {"scope": model.get("scope"), "model": model["model"]},
        "flows": flows,
        "warnings": warnings,
        "loom": None,
        "live": None,
    }
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[(str(path), sha)] = body
    return body
