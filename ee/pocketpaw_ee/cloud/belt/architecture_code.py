# ee/pocketpaw_ee/cloud/belt/architecture_code.py — the reader behind
# ``GET /belt/architecture/code?repo=&component=``: one C4 component's files,
# for the architecture viewer's Code tab.
#
# It starts from ``architecture_read.read_architecture`` (its public call only,
# never its cache), so the files are listed at the same ``sha`` the viewer
# shows. A file belongs to the component when it matches one of the
# component's OWN ``paths`` globs (``orient.path_index`` builds the regexes), so
# a parent with ``src/`` also lists a child's ``src/ui/`` files; the live
# block's single-owner join is ``orient.component_for``, a different question.
#
# Body: ``{sha, files: [{path, lang, loc, url, prs}], expired, no_paths}``.
# ``loc`` is the line count (0 when empty, ``None`` for a binary), one ``git
# grep -c`` over the whole list. ``url`` is the GitHub blob at ``sha`` (``None``
# without a slug). ``prs`` are open PR numbers touching the file, from one ``gh
# pr list`` made only when the live block shows an open PR; a failed gh read
# leaves every ``prs`` empty, uncached, and never fails the body. ``expired``: the
# component has paths but they match no file. ``no_paths``: it has none.
#
# Cached per (repo, sha, component, open PR numbers): a new trunk sha or a PR
# opening or closing reads again. An id outside the owned system's components
# is 404 ``belt.component_not_found``; the read's own errors pass through.

from __future__ import annotations

import logging
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import app_blueprint_read as abr
from . import architecture_read as ar
from .architecture_live import _LIMIT
from .orient import _dicts, _owned_system, path_index
from .service import BeltConsoleError

logger = logging.getLogger(__name__)
_CACHE_MAX = 256
_CHUNK = 500  # pathspecs per git grep
# ponytail: drop-all when full; an LRU if the viewer ever walks many shas
_CACHE: dict[tuple[str, str, str, tuple[int, ...]], dict[str, Any]] = {}
_LANG = {
    "py": "python", "ts": "typescript", "tsx": "typescript", "js": "javascript",
    "jsx": "javascript", "mjs": "javascript", "svelte": "svelte", "go": "go",
    "rs": "rust", "swift": "swift", "md": "markdown", "mdx": "markdown",
    "json": "json", "css": "css", "html": "html", "sh": "shell", "yml": "yaml",
    "yaml": "yaml", "toml": "toml", "sql": "sql",
}  # fmt: skip


def lang(path: str) -> str | None:
    """A display language from the extension (the bare extension when unmapped)."""
    ext = posixpath.splitext(path)[1].lstrip(".").lower()
    return _LANG.get(ext, ext or None)


def _open_prs(live: dict[str, Any] | None) -> tuple[int, ...]:
    rows = [*((live or {}).get("components") or {}).values(), (live or {}).get("repo") or {}]
    return tuple(sorted({p["number"] for r in rows for p in r.get("prs", []) if p.get("number")}))


async def read_code(
    workspace_id: str, repo: str, component: str, *, gh: abr.Run | None = None
) -> dict[str, Any]:
    """The component's files at the architecture read's sha (see the header)."""
    body = await ar.read_architecture(workspace_id, repo)
    system = _owned_system(body["model"]) or {}
    comps = [c for k in _dicts(system.get("containers")) for c in _dicts(k.get("components"))]
    comp = next((c for c in comps if str(c.get("id")) == component), None)
    if comp is None:
        raise BeltConsoleError(404, "No such component.", "belt.component_not_found")
    sha, slug = body["sha"], body["slug"]
    if not comp.get("paths"):
        return {"sha": sha, "files": [], "expired": False, "no_paths": True}
    prs = _open_prs(body.get("live"))
    key = (body["repo"], sha, component, prs)
    if hit := _CACHE.get(key):
        return hit
    git = abr._git(Path(body["repo"]))
    own = [rule[3] for rule in path_index(body["model"]) if rule[4] == component]
    try:
        sizes: dict[str, int] = {}
        for entry in (await abr._ok(git, ["ls-tree", "-r", "-l", "-z", sha])).split("\0"):
            meta, _, path = entry.partition("\t")
            if meta.split()[1:2] == ["blob"] and any(r.fullmatch(path) for r in own):
                sizes[path] = int(meta.split()[3])
        lines = await _line_counts(git, sha, list(sizes))
    except abr.ReadFailed:
        logger.warning("Code read of %s at %s failed", body["repo"], sha, exc_info=True)
        msg = "Files not readable yet."
        raise BeltConsoleError(503, msg, "belt.architecture_unavailable") from None
    touching = await _pr_files(gh, slug) if prs and slug else {}
    files = [
        {
            "path": p,
            "lang": lang(p),
            "loc": lines.get(p, 0 if size == 0 else None),  # absent and sized: binary
            "url": f"https://github.com/{slug}/blob/{sha}/{quote(p)}" if slug else None,
            "prs": (touching or {}).get(p, []),
        }
        for p, size in sizes.items()
    ]
    out = {"sha": sha, "files": files, "expired": not files, "no_paths": False}
    if touching is None:  # a failed gh read is served, never cached
        return out
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = out
    return out


async def _line_counts(git: abr.Run, sha: str, paths: list[str]) -> dict[str, int]:
    """Lines per text file at ``sha``; git grep prints no row for an empty or
    binary file. Chunked so a big component never overflows argv."""
    counts: dict[str, int] = {}
    for i in range(0, len(paths), _CHUNK):
        argv = ["--literal-pathspecs", "grep", "-c", "-I", "-z", "-e", "", sha, "--"]
        code, out, err = await git([*argv, *paths[i : i + _CHUNK]])
        if code not in (0, 1):  # 1: nothing matched (all empty or binary)
            raise abr.ReadFailed(err.strip()[:200] or "git grep")
        for row in out.splitlines():
            name, _, n = row.partition("\0")
            counts[name.removeprefix(f"{sha}:")] = int(n)
    return counts


async def _pr_files(gh: abr.Run | None, slug: str) -> dict[str, list[int]] | None:
    """Open PR numbers per changed file; ``None`` when gh fails (files still serve)."""
    from pocketpaw_ee.cloud.mandates.patrols import _default_gh

    argv = ["gh", "pr", "list", "-R", slug, "--state", "open", "--limit", _LIMIT]
    try:
        rows = await abr._rows(gh or _default_gh, [*argv, "--json", "number,files"])
    except Exception:  # gh missing, a rate limit, bad JSON
        logger.warning("Open PR read of %s failed", slug, exc_info=True)
        return None
    touching: dict[str, list[int]] = {}
    for pr in rows:
        for f in _dicts(pr.get("files")):
            touching.setdefault(str(f.get("path")), []).append(pr.get("number"))
    return {p: sorted(n for n in ns if isinstance(n, int)) for p, ns in touching.items()}
