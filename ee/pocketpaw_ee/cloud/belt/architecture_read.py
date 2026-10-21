# ee/pocketpaw_ee/cloud/belt/architecture_read.py — the reader and cache behind
# ``GET /belt/architecture?repo=``: one repo's committed C4 model for the
# architecture viewer.
#
# The body is ``{repo, slug, sha, stale, read_at, model, flows, loom, live}``.
# ``model`` is ``{scope, model}`` from ``docs/c4/model.json`` at ``origin/dev``
# (``origin/main`` when the clone has no ``origin/dev``, as ripple), read through
# ``service.read_repo_model`` after a fetch of that branch, so it follows the
# remote, not the checkout. ``live`` is ``architecture_live``'s block, read under
# the same cache entry; a failed live read (gh, a rate limit) keeps the fresh
# model and serves the last live block with its own ``stale: True``, ``None``
# with none yet. ``flows`` (``[]``) and ``loom`` (``None``) are the keys later
# slices fill; clients may rely on them being present.
#
# Cached per (workspace, repo) for ``TTL`` behind a single-flight lock, the same
# shape as ``app_blueprint_read``. A failed refresh (fetch, git missing, a bug)
# serves the last body with ``stale: True``, 503 with none yet. A repo outside
# the allowlist is 404 ``belt.repo_not_found``; no model, or one over
# ``MAX_MODEL_BYTES``, is 404 ``belt.architecture_not_found`` and never cached.

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import app_blueprint_read as abr
from .architecture_live import read_live
from .executor import _re_resolve_repo, _run
from .service import _READ_GIT, BeltConsoleError, read_repo_model

TTL = 60.0
_BRANCHES = ("dev", "main")  # the trunk a repo's PRs target; ripple has no dev
logger = logging.getLogger(__name__)
# ponytail: a copy of app_blueprint_read's lock + cache dicts; lift both into one
# helper when a third reader needs them
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


class _NoModel(Exception):
    """The trunk has no readable ``docs/c4/model.json``: a 404, not a stale read."""


async def read_architecture(
    workspace_id: str,
    repo: str,
    *,
    now: Callable[[], float] = time.monotonic,
    gh: abr.Run | None = None,
    runs: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    touched: Callable[[str, str], Awaitable[list[str]]] | None = None,
) -> dict[str, Any]:
    """The repo's architecture body (design doc 2026-10-21, section 4.1). The
    keyword seams default to the patrols gh runner and the Belt run readers."""
    path, _err = _re_resolve_repo(repo)
    if path is None:
        raise BeltConsoleError(404, "Repo not found.", "belt.repo_not_found")
    key = (workspace_id, str(path))
    async with _LOCKS.setdefault(key, asyncio.Lock()):
        hit = _CACHE.get(key)
        if hit and now() - hit["at"] < TTL:
            return hit["body"]
        try:
            io = (gh, runs or abr._lead_runs, touched or abr._touched)
            body = await _read(workspace_id, path, hit["body"] if hit else None, *io)
        except _NoModel:
            msg = "No C4 model in this repo. Add docs/c4/model.json on its trunk."
            raise BeltConsoleError(404, msg, "belt.architecture_not_found") from None
        except Exception:  # a failed fetch, git missing, or a bug: stale or 503
            logger.warning("Architecture read of %s failed", path, exc_info=True)
            if hit is None:
                msg = "Architecture not readable yet."
                raise BeltConsoleError(503, msg, "belt.architecture_unavailable") from None
            body = {**hit["body"], "stale": True}
        _CACHE[key] = {"body": body, "at": now()}
        return body


async def _git(path: Path, *argv: str) -> tuple[int, str, str]:
    return await _run([*_READ_GIT, *argv], cwd=path)


async def _read(
    workspace_id: str, path: Path, prev: dict[str, Any] | None, gh: Any, runs: Any, touched: Any
) -> dict[str, Any]:
    from pocketpaw_ee.cloud.mandates.patrols import _default_gh, _github_slug

    gh = gh or _default_gh

    for branch in _BRANCHES:
        code, _, _ = await _git(path, "rev-parse", "--verify", "--quiet", f"origin/{branch}")
        if code == 0:
            break
    else:
        raise _NoModel
    code, _, err = await _git(path, "fetch", "--no-tags", "origin", branch)
    if code != 0:
        raise RuntimeError(f"fetch origin {branch}: {err.strip()[:200]}")
    sha, model = await read_repo_model(path, f"origin/{branch}")
    if model is None:
        raise _NoModel
    slug = prev["slug"] if prev else await _github_slug(str(path), gh) or ""
    try:
        io = {"gh": gh, "runs": runs, "touched": touched}
        live = await read_live(workspace_id, path, branch, slug, model, **io)
    except Exception:  # gh, a rate limit, git or a bug: the model stays fresh
        logger.warning("Live read of %s failed", path, exc_info=True)
        live = {**prev["live"], "stale": True} if prev and prev.get("live") else None
    return {
        "repo": str(path),
        "slug": slug,
        "sha": sha,
        "stale": False,
        "read_at": datetime.now(UTC).isoformat(),
        "model": {"scope": model.get("scope"), "model": model["model"]},
        "flows": [],
        "loom": None,
        "live": live,
    }
