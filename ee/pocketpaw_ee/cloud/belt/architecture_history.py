# ee/pocketpaw_ee/cloud/belt/architecture_history.py — the architecture viewer's
# history: ``GET /belt/architecture/history?repo=`` lists the trunk's model
# commits, and ``GET /belt/architecture?repo=&at=<sha>`` (and the Code tab's
# ``/architecture/code?...&at=``) reads the model as of one of them.
#
# The trunk is ``architecture_read``'s (``origin/dev``, else ``origin/main``), as
# that read last fetched it: neither call fetches. History is the last
# ``HISTORY_LIMIT`` trunk commits touching ``docs/c4/``, newest first, as
# ``[{sha, date, subject}]`` (``date`` the committer date, ISO 8601).
#
# An ``at`` read is 404 ``belt.commit_not_found`` unless ``at`` is 7-40 hex
# characters (checked before any git call, so it is never an option) naming a
# commit reachable from the trunk. Its body has the live read's keys with that
# commit's full ``sha``, ``stale: False`` and ``live: None``; it never reads the
# live block and starts no background task. A commit's model never changes, so
# bodies are cached per (repo, full sha) with no TTL, up to ``_CACHE_MAX``.

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import app_blueprint_read as abr
from .architecture_flows import load_flows
from .architecture_read import _BRANCHES, _git
from .executor import _re_resolve_repo
from .service import BeltConsoleError, read_repo_model

HISTORY_LIMIT = 50
_CACHE_MAX = 64
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
# ponytail: drop-all when full; an LRU if the viewer ever walks many commits
_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


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


async def read_history(workspace_id: str, repo: str) -> list[dict[str, str]]:
    """The trunk's model commits, newest first (see the header)."""
    path, ref = await _trunk(repo)
    fmt = "--format=%H%x1f%cI%x1f%s"
    code, out, err = await _git(path, "log", "-n", str(HISTORY_LIMIT), fmt, ref, "--", "docs/c4/")
    if code != 0:
        raise _unavailable(err)
    rows = [line.split("\x1f", 2) for line in out.splitlines() if line]
    return [{"sha": s, "date": d, "subject": m} for s, d, m in rows]


async def read_at(
    workspace_id: str, repo: str, at: str, *, gh: abr.Run | None = None
) -> dict[str, Any]:
    """The architecture body as of trunk commit ``at`` (see the header)."""
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
