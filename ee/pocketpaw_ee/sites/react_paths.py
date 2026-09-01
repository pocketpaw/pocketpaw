# react_paths.py — the ONE place the react-track source-map path policy lives.
#
# Updated: 2026-09-01 (fix/sites-react-orphan-create) — added
# ``react_path_is_referenced``, which answers "does anything in this source map
# reach that path". The module was pure POLICY (may this path be written); this is
# the first question about REACHABILITY, and it lives here because the answer
# depends on the same two-prefix layout the policy above encodes: ``src/`` is a
# module tree reached by import specifiers, ``public/`` is copied to the web root
# and reached by URL. Written for the edit lane's ``create``, which could land a
# component nothing imports and report a clean success — see the section comment
# above the function for the incident that produced it.
#
# Created: 2026-08-11 (feat/sites-react-edit-lane, RX-3) — extracted from
# ``agent/mcp_servers/sites_create.py``, which owned ``REACT_RESERVED_FILES`` /
# ``REACT_RESERVED_PREFIX`` / ``_reserved_react_keys`` because create was the only
# writer of a react ``source`` map. ``edit_react_component`` is the second writer,
# and a second writer with its own copy of the guard is how the guard rots: an edit
# that could write ``package.json`` would defeat the generator's dependency
# allowlist and, with it, the supply-chain release-age floor the manifest is what
# enforces. So the normalization + the reserved set moved HERE and both writers call
# it. ``sites_create`` re-exports the two constants and ``_reserved_react_keys``
# under their old names, so nothing that imported them from there had to change.
#
# What is NEW here (create never needed it) is :func:`react_path_rejection` — the
# single-path verdict an edit needs. Create validates a whole map and only had to
# answer "which keys collide"; an edit names ONE path and has to answer "may this
# path be written at all", which is the reserved question PLUS a positive one:
# the resolved path must land under ``src/`` or ``public/``. Create got that second
# half for free (its required ``src/App.tsx`` key and the generator's own scaffold
# meant a stray root file was merely inert), but an edit with ``create=True`` can
# mint an arbitrary path, so "not reserved" is not the same as "allowed".
"""React-track source-map path policy for Paw Sites.

A react-engine pocket's ``source`` is a ``{relative_path: file_contents}`` map that
the paw-sites generator materializes ON TOP of a build shell it owns. Two rules
govern which paths an author (create OR edit) may write:

1. **Reserved paths are the generator's.** ``index.html``, ``package.json``,
   ``vite.config.ts``, ``paw-prerender.mjs`` and everything under ``src/paw/``
   carry the prerender contract. paw-sites' ``react-scaffold.ts`` throws on a
   collision; checking here turns a build-time throw far from the authoring turn
   into an actionable error. It is not tidiness: an author who could overwrite
   ``paw-prerender.mjs`` could remove the pass that fills the prerender outlet,
   turning the site back into a shell that is blank with JavaScript disabled — and
   an author who could overwrite ``package.json`` would be writing the dependency
   manifest, which is where the supply-chain release-age floor is enforced.

2. **Authored files live under ``src/`` or ``public/``.** Everything else at the
   project root belongs to the shell, so a path outside those two prefixes is
   rejected rather than silently written somewhere the build ignores.

Both rules are applied to the NORMALIZED path: backslashes become forward slashes
and ``.``/``..`` segments collapse (``posixpath``, not ``os.path`` — source-map keys
are POSIX-style project-relative paths regardless of the host OS). A guard a
trivial path spelling defeats is not a guard, and ``./package.json`` /
``src\\paw\\entry.tsx`` / ``src/paw/../paw/entry.tsx`` are trivial spellings.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from typing import Any

# Paths the generator owns and no source map may write. Mirrors ``RESERVED_FILES``
# + ``RESERVED_NAMESPACE`` in paw-sites' react-scaffold.ts, which throws on a
# collision.
REACT_RESERVED_FILES: tuple[str, ...] = (
    "index.html",
    "package.json",
    "vite.config.ts",
    "paw-prerender.mjs",
)
REACT_RESERVED_PREFIX = "src/paw/"

# The two directories an author may write into. Anything else — a root-level file,
# a path that escapes the project with ``..``, an absolute path — is not authorable.
REACT_AUTHORABLE_PREFIXES: tuple[str, ...] = ("src/", "public/")


def normalize_react_path(path: str) -> str:
    """Collapse a source-map key to the path the generator will actually write.

    Backslashes become forward slashes (a Windows-authored key, or an agent that
    guessed the separator) and ``.``/``..`` segments collapse. The generator
    normalizes the same way before it throws, so normalizing first is what makes
    the guards below agree with it.
    """
    return posixpath.normpath(path.replace("\\", "/"))


def is_reserved_react_path(path: str) -> bool:
    """True when ``path`` resolves onto a generator-owned file or namespace."""
    norm = normalize_react_path(path)
    return norm in REACT_RESERVED_FILES or norm.startswith(REACT_RESERVED_PREFIX)


def is_authorable_react_path(path: str) -> bool:
    """True when ``path`` resolves inside ``src/`` or ``public/``.

    Note this is about the RESOLVED path: ``src/../package.json`` normalizes to
    ``package.json`` and is not authorable, which is the point.
    """
    norm = normalize_react_path(path)
    return any(norm.startswith(prefix) for prefix in REACT_AUTHORABLE_PREFIXES)


def reserved_react_keys(source: dict[str, Any]) -> list[str]:
    """Return the source-map keys that collide with a generator-owned path.

    The whole-map form, used by ``create_react_site`` to name every offending key
    at once instead of failing on the first. Returns the keys AS THE AUTHOR SPELLED
    THEM (not normalized) so the error message points at something findable in the
    payload.
    """
    return sorted(key for key in source if is_reserved_react_path(key))


def react_path_rejection(path: str) -> str | None:
    """Return why ``path`` may not be written, or ``None`` when it may.

    The single-path form, used by the edit lane. Two rejections, checked in this
    order because the reserved one is the more specific and more useful message:

      * a generator-owned path (rule 1) — names the shell and the reason;
      * anything outside ``src/`` / ``public/`` (rule 2) — catches root-level
        files, ``..`` escapes and absolute paths in one check.

    Returns a message fragment the caller wraps in its own error, so the error
    code is the caller's choice (the sites service raises two distinct codes) and
    this module stays free of any dependency on the cloud error hierarchy.
    """
    norm = normalize_react_path(path)
    if norm in REACT_RESERVED_FILES or norm.startswith(REACT_RESERVED_PREFIX):
        return (
            f"`{path}` resolves to `{norm}`, which the generator owns. The build "
            "shell (index.html, package.json, vite.config.ts, paw-prerender.mjs) "
            "and the `src/paw/` namespace carry the prerender contract that keeps "
            "the page from shipping blank without JavaScript, and package.json is "
            "where the dependency allowlist lives. Edit under `src/` (outside "
            "`src/paw/`) or `public/`."
        )
    if not any(norm.startswith(prefix) for prefix in REACT_AUTHORABLE_PREFIXES):
        return (
            f"`{path}` resolves to `{norm}`, which is outside the authored source "
            "tree. A react site's own files live under `src/` or `public/`; "
            "everything else at the project root belongs to the generated build "
            "shell."
        )
    return None


# ---------------------------------------------------------------------------
# Is this path reached by anything? (the two-call contract's missing half)
# ---------------------------------------------------------------------------
#
# Added 2026-09-01 (fix/sites-react-orphan-create). Adding a section to a react site
# is TWO calls — ``create=true`` writes ``src/components/<Name>.tsx``, then a second
# ``edits`` call imports and renders it in ``src/App.tsx`` — and nothing connected
# the two. A create that landed call 1 and never got call 2 returned a flat success
# for a file the bundle does not contain: no import, no render, no trace on the page.
# The agent read the clean success and told the user the work was done, which is how
# a real incident produced "I added it to a component" over a site that never changed.
#
# So a create now ANSWERS the question its own success hides: does anything reach
# this file yet? The verdict is advisory — call 1 is unreferenced by definition at
# the instant it happens, and a create that refused or errored on it would close the
# only lane that can add a section at all.
#
# The two prefixes are reached two different ways and a single rule gets one of them
# wrong. ``src/`` is a MODULE tree: reached by an import specifier, which is resolved
# here rather than pattern-matched, so `./components/Hero` from `src/App.tsx` and
# `../components/Hero` from `src/sections/X.tsx` both resolve to the one file they
# mean. ``public/`` is NOT a module tree: it is copied to the web root and reached by
# URL (`<img src="/logo.png">`), so an import-only scan would call every correctly
# used asset an orphan.

# Extensions a specifier may omit. A resolved specifier and a source-map key are
# compared with these stripped, because `./components/Hero` and
# `src/components/Hero.tsx` are the same file written two ways.
_REACT_MODULE_EXTS: tuple[str, ...] = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".css")

# Quoted module specifiers: `from '...'`, a side-effect or dynamic `import '...'` /
# `import('...')`, and `require('...')`. Deliberately NOT a general string scan —
# matching any quoted text would let a testimonial mentioning "Testimonials" pass
# for an import, and a false "it is referenced" is the silence this exists to break.
_REACT_SPECIFIER_RE = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]([^'"\n]+)['"]"""
)


def _strip_module_ext(path: str) -> str:
    """Drop a module extension so a specifier and a map key compare equal."""
    for ext in _REACT_MODULE_EXTS:
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def _resolve_specifier(importer: str, specifier: str) -> str | None:
    """Resolve one import specifier to a project-relative path, or ``None``.

    ``None`` means "not a local file" — a bare package specifier (``react``,
    ``react-dom/client``) resolves into node_modules and can never name a source-map
    key. Relative specifiers resolve against the IMPORTER's directory, which is the
    whole reason this is a resolver and not a substring search: `./Hero` means a
    different file depending on who wrote it.
    """
    if specifier.startswith("."):
        return posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    if specifier.startswith("/"):
        return posixpath.normpath(specifier.lstrip("/"))
    # Vite's conventional root aliases. Neither is configured in the generator's
    # shell today; resolving them anyway costs nothing and means the day one is,
    # this reports "referenced" instead of nagging about a wired component.
    if specifier.startswith(("@/", "~/")):
        return posixpath.normpath("src/" + specifier[2:])
    return None


def react_path_is_referenced(source: Mapping[str, Any], path: str) -> bool:
    """Does any OTHER file in ``source`` reach ``path``?

    ``source`` is the source map AFTER the write, so the created file is present and
    is skipped — a module that imports itself is still unreachable from the page.

    A ``public/`` path is matched on its URL (``public/img/logo.png`` →
    ``/img/logo.png``) as a substring, because an asset reference is an attribute
    value in arbitrary markup and there is no grammar to resolve. A ``src/`` path is
    matched by resolving every import specifier in every other file and comparing
    extension-stripped paths, so the answer does not depend on how the import was
    spelled.
    """
    norm = normalize_react_path(path)

    if norm.startswith("public/"):
        url = "/" + norm[len("public/") :]
        return any(url in str(text) for key, text in source.items() if key != norm)

    target = _strip_module_ext(norm)
    for key, text in source.items():
        if normalize_react_path(key) == norm:
            continue  # the file itself — a self-import reaches nothing
        importer = normalize_react_path(key)
        for specifier in _REACT_SPECIFIER_RE.findall(str(text)):
            resolved = _resolve_specifier(importer, specifier)
            if resolved is not None and _strip_module_ext(resolved) == target:
                return True
    return False


__all__ = [
    "REACT_AUTHORABLE_PREFIXES",
    "REACT_RESERVED_FILES",
    "REACT_RESERVED_PREFIX",
    "is_authorable_react_path",
    "is_reserved_react_path",
    "normalize_react_path",
    "react_path_is_referenced",
    "react_path_rejection",
    "reserved_react_keys",
]
