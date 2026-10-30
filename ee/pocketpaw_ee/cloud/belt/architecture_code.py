# ee/pocketpaw_ee/cloud/belt/architecture_code.py — the reader behind
# ``GET /belt/architecture/code?repo=&component=``: one C4 component's files,
# for the architecture viewer's Code tab.
#
# It starts from ``architecture_read.read_architecture`` (its public call only,
# never its cache), or ``architecture_history.read_at`` given ``at``, so the
# files are listed at the same ``sha`` the viewer shows. A file belongs to the
# component when it matches one of the component's OWN ``paths`` globs
# (``orient.path_index`` builds the regexes), so a parent with ``src/`` also
# lists a child's ``src/ui/`` files; the live block's single-owner join is
# ``orient.component_for``, a different question.
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
#
# ``read_file`` (``&file=``) opens one file of that listing (an exact path match,
# else 404 ``belt.file_not_found``, so nothing outside the listing is read):
# ``{sha, path, lang, symbols, symbols_source, symbols_skipped, excerpt}``.
# ``symbols`` come from ``architecture_symbols`` (``symbols_source: "loom"``; the
# world-model is the workspace's, not ``sha``'s, so a line can drift on an old
# ``at``), else from ``file_symbols`` over the file at ``sha`` (``"file"``,
# ``callers: None``); ``None`` with neither. Each has the file url plus ``#L<line>``.
# ``excerpt`` is Ripple's ``C4Code`` shape ``{startLine, before, language}``:
# ``EXCERPT_LINES`` lines from ``line`` (the top by default), ``None`` for an
# empty, binary or over-``MAX_MODEL_BYTES`` file, or a failed git read.

from __future__ import annotations

import logging
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import app_blueprint_read as abr
from . import architecture_history, architecture_symbols
from . import architecture_read as ar
from .architecture_live import _LIMIT
from .file_symbols import file_symbols
from .orient import _dicts, _owned_system, path_index
from .service import MAX_MODEL_BYTES, BeltConsoleError

logger = logging.getLogger(__name__)
_CACHE_MAX = 256
_CHUNK = 500  # pathspecs per git grep
EXCERPT_LINES = 40
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
    workspace_id: str, repo: str, component: str, *, at: str = "", gh: abr.Run | None = None
) -> dict[str, Any]:
    """The component's files at the architecture read's sha (see the header)."""
    return await _files(await _read(workspace_id, repo, at), component, gh)


async def read_file(
    workspace_id: str,
    repo: str,
    component: str,
    file: str,
    *,
    at: str = "",
    line: int = 0,
    gh: abr.Run | None = None,
) -> dict[str, Any]:
    """One of the component's files: its symbols and an excerpt (see the header)."""
    body = await _read(workspace_id, repo, at)
    listing = await _files(body, component, gh)
    row = next((f for f in listing["files"] if f["path"] == file), None)  # exact: no traversal
    if row is None:
        raise BeltConsoleError(404, "No such file in this component.", "belt.file_not_found")
    scope = (body.get("model") or {}).get("scope")
    symbols, skipped = await architecture_symbols.symbols_for(Path(body["repo"]), file, scope)
    text = None
    if row["loc"]:  # None: binary; 0: empty
        try:
            text = await _blob(abr._git(Path(body["repo"])), listing["sha"], file)
        except abr.ReadFailed:
            logger.warning("Read of %s at %s failed", file, listing["sha"], exc_info=True)
    source = "loom" if symbols else None
    if not symbols and text is not None and (symbols := file_symbols(text, row["lang"])):
        source = "file"
    for s in symbols:
        s["url"] = f"{row['url']}#L{s['line']}" if row["url"] and s["line"] else row["url"]
    excerpt = None
    if text is not None:
        # split, not splitlines: \f or U+2028 would shift the line numbers
        lines = text.removesuffix("\n").split("\n")
        start = max(1, min(line, len(lines)))
        before = lines[start - 1 : start - 1 + EXCERPT_LINES]
        excerpt = {"startLine": start, "before": before, "language": lang(file)}
    return {
        "sha": listing["sha"], "path": file, "lang": row["lang"], "symbols": symbols,
        "symbols_source": source, "symbols_skipped": skipped, "excerpt": excerpt,
    }  # fmt: skip


async def _blob(git: abr.Run, sha: str, path: str) -> str | None:
    """The file's text at ``sha``, sized by ``ls-tree`` first so an oversized blob
    is never read (``None``)."""
    entry = await abr._ok(git, ["--literal-pathspecs", "ls-tree", "-l", sha, "--", path])
    _mode, kind, obj, size = (entry.split("\t", 1)[0].split() + ["", "", "", ""])[:4]
    if kind != "blob" or not size.isdigit() or int(size) > MAX_MODEL_BYTES:
        return None
    return await abr._ok(git, ["cat-file", "blob", obj])


async def _read(workspace_id: str, repo: str, at: str) -> dict[str, Any]:
    if at:
        return await architecture_history.read_at(repo, at)
    return await ar.read_architecture(workspace_id, repo)


async def _files(body: dict[str, Any], component: str, gh: abr.Run | None) -> dict[str, Any]:
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
