# ee/pocketpaw_ee/cloud/belt/architecture_read.py — the reader and cache behind
# ``GET /belt/architecture?repo=``: one repo's committed C4 model for the
# architecture viewer.
#
# The body is ``{repo, slug, sha, stale, read_at, model, flows, warnings, loom,
# live}``.
# ``model`` is ``{scope, model}`` from ``docs/c4/model.json`` at ``origin/dev``
# (``origin/main`` when the clone has no ``origin/dev``, as ripple), read through
# ``service.read_repo_model`` after a fetch of that branch, so it follows the
# remote, not the checkout. ``live`` is ``architecture_live``'s block, read under
# the same cache entry; a failed live read (gh, a rate limit) keeps the fresh
# model and serves the last live block with its own ``stale: True``, ``None``
# with none yet. ``flows`` and ``warnings`` are ``architecture_flows``'s read of
# ``docs/c4/flows.json`` at the same sha (``[]`` with no file). ``loom``
# (``None``) is the key a later slice fills; clients may rely on it.
#
# Cached per (workspace, repo), stale-while-revalidate: a read within ``TTL``
# is the cached body; a later one is still the cached body, served at once,
# while one background task per key refreshes it (``_TASKS`` holds the task,
# so it is never collected mid-read and a second caller never starts another).
# The live block's gh reads take 4-10 s against ~1.5 s for the model, so on a
# miss only the model is read inline (behind a per-key lock, so concurrent
# misses fetch once) and the body goes out with ``live: None`` while a
# background task fills it in. A failed refresh (fetch, git missing, a bug)
# keeps the last body with ``stale: True``; a miss that fails is 503. A repo
# outside the allowlist is 404 ``belt.repo_not_found``; no model, or one over
# ``MAX_MODEL_BYTES``, is 404 ``belt.architecture_not_found`` and never cached
# (a refresh that finds the model gone drops the entry).

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import app_blueprint_read as abr
from .architecture_flows import load_flows
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
_TASKS: dict[tuple[str, str], asyncio.Task[None]] = {}  # the refresh in flight per key


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
    from pocketpaw_ee.cloud.mandates.patrols import _default_gh

    path, _err = _re_resolve_repo(repo)
    if path is None:
        raise BeltConsoleError(404, "Repo not found.", "belt.repo_not_found")
    key = (workspace_id, str(path))
    io = (gh or _default_gh, runs or abr._lead_runs, touched or abr._touched)
    if (hit := _CACHE.get(key)) is None:
        async with _LOCKS.setdefault(key, asyncio.Lock()):
            if (hit := _CACHE.get(key)) is None:  # still a miss: the model now, live behind
                try:
                    body, branch, model = await _read_model(path, None, io[0])
                except _NoModel:
                    msg = "No C4 model in this repo. Add docs/c4/model.json on its trunk."
                    raise BeltConsoleError(404, msg, "belt.architecture_not_found") from None
                except Exception:  # a failed fetch, git missing, or a bug
                    logger.warning("Architecture read of %s failed", path, exc_info=True)
                    msg = "Architecture not readable yet."
                    raise BeltConsoleError(503, msg, "belt.architecture_unavailable") from None
                hit = _CACHE[key] = {"body": body, "at": now()}
                _behind(key, now, _with_live(workspace_id, path, body, branch, model, None, io))
    elif now() - hit["at"] >= TTL and key not in _TASKS:
        _behind(key, now, _refresh(workspace_id, path, hit["body"], io))
    return hit["body"]


def _behind(key: tuple[str, str], now: Callable[[], float], read: Awaitable[Any]) -> None:
    """Run ``read`` in the background and cache its body (``None`` drops the entry)."""

    async def run() -> None:
        try:
            body = await read
            if body is None:
                _CACHE.pop(key, None)
            else:
                _CACHE[key] = {"body": body, "at": now()}
        except Exception:  # a bug past the readers' own handling: log it, keep the last body
            logger.exception("Background architecture read of %s failed", key[1])
        finally:
            _TASKS.pop(key, None)

    _TASKS[key] = asyncio.create_task(run())


async def _refresh(
    workspace_id: str, path: Path, prev: dict[str, Any], io: tuple[Any, Any, Any]
) -> dict[str, Any] | None:
    """A fresh body; the last one with ``stale: True`` on a failed read, ``None``
    when the model is gone."""
    try:
        body, branch, model = await _read_model(path, prev, io[0])
    except _NoModel:
        return None
    except Exception:  # a failed fetch, git missing, or a bug: stale
        logger.warning("Architecture read of %s failed", path, exc_info=True)
        return {**prev, "stale": True}
    return await _with_live(workspace_id, path, body, branch, model, prev, io)


async def _git(path: Path, *argv: str) -> tuple[int, str, str]:
    return await _run([*_READ_GIT, *argv], cwd=path)


async def _read_model(
    path: Path, prev: dict[str, Any] | None, gh: Any
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """``(body, branch, model)``: the body with ``live: None``, the trunk it read."""
    from pocketpaw_ee.cloud.mandates.patrols import _github_slug

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
    flows, warnings = await load_flows(path, sha, model)
    slug = prev["slug"] if prev else await _github_slug(str(path), gh) or ""
    body = {
        "repo": str(path),
        "slug": slug,
        "sha": sha,
        "stale": False,
        "read_at": datetime.now(UTC).isoformat(),
        "model": {"scope": model.get("scope"), "model": model["model"]},
        "flows": flows,
        "warnings": warnings,
        "loom": None,
        "live": None,
    }
    return body, branch, model


async def _with_live(
    workspace_id: str,
    path: Path,
    body: dict[str, Any],
    branch: str,
    model: dict[str, Any],
    prev: dict[str, Any] | None,
    io: tuple[Any, Any, Any],
) -> dict[str, Any]:
    """``body`` with its live block; a failed live read keeps the model fresh."""
    gh, runs, touched = io
    try:
        io_kw = {"gh": gh, "runs": runs, "touched": touched}
        live = await read_live(workspace_id, path, branch, body["slug"], model, **io_kw)
    except Exception:  # gh, a rate limit, git or a bug: the model stays fresh
        logger.warning("Live read of %s failed", path, exc_info=True)
        live = {**prev["live"], "stale": True} if prev and prev.get("live") else None
    return {**body, "live": live}
