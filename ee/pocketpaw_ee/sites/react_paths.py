# react_paths.py — the ONE place the react-track source-map path policy lives.
#
# Both writers of a react source map (``create_react_site`` and the
# ``edit_react_component`` edit lane) call this module; ``sites_create`` re-exports
# the constants under their old names. It mirrors paw-sites' ``react-scaffold.ts``
# ``RESERVED_FILES`` / ``RESERVED_NAMESPACE``, matched case-insensitively on the
# normalized path.
#
# Since 2026-10-07 ("open everything") the root build files — ``package.json``,
# ``vite.config.*``, ``bunfig.toml``, ``.npmrc`` — belong to the author; the generator
# merges them with its toolchain. What stays generator-owned: ``index.html``,
# ``paw-prerender.mjs`` and ``src/paw/`` (the prerender contract), lockfiles, and
# ``paw.dependencies.json`` (only the resolver behind ``set_site_dependencies``
# writes it).
#
# :func:`react_path_is_referenced` answers a different question — does anything in
# the map reach a path — for the edit lane's ``create``, which could otherwise land a
# component nothing imports and report a clean success.
"""React-track source-map path policy for Paw Sites.

A react-engine pocket's ``source`` is a ``{relative_path: file_contents}`` map that
the paw-sites generator materializes ON TOP of a build shell it owns. Two rules
govern which paths an author (create OR edit) may write:

1. **Reserved paths are the generator's.** ``index.html``, ``paw-prerender.mjs`` and
   everything under ``src/paw/`` carry the prerender contract: an author who could
   overwrite ``paw-prerender.mjs`` could remove the pass that fills the prerender
   outlet, turning the site back into a shell that is blank with JavaScript
   disabled. Lockfiles and ``paw.dependencies.json`` are reserved too.

2. **Authored files live under ``src/`` or ``public/``**, plus the root build files
   in :data:`REACT_AUTHOR_ROOT_FILES`. Anything else at the project root is rejected
   rather than silently written somewhere the build ignores.

Both rules are applied to the NORMALIZED path: backslashes become forward slashes
and ``.``/``..`` segments collapse (``posixpath``, not ``os.path`` — source-map keys
are POSIX-style project-relative paths regardless of the host OS). A guard a
trivial path spelling defeats is not a guard, and ``./index.html`` /
``src\\paw\\entry.tsx`` / ``src/paw/../paw/entry.tsx`` are trivial spellings.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from typing import Any

from pocketpaw_ee.sites.dependency_manifest import (
    AUTHOR_SHELL_FILES,
    DEPENDENCY_MANIFEST_PATH,
    LOCKFILES,
    is_dependency_manifest_path,
)

# Paths the generator owns and no source map may write. Mirrors ``RESERVED_FILES``
# + ``RESERVED_NAMESPACE`` in paw-sites' react-scaffold.ts, which throws on a
# collision.
REACT_RESERVED_FILES: tuple[str, ...] = (
    "index.html",
    "paw-prerender.mjs",
    DEPENDENCY_MANIFEST_PATH,
    *LOCKFILES,
)
REACT_RESERVED_PREFIX = "src/paw/"
# Case-folded once: the generator matches case-insensitively, so this must.
_RESERVED_FOLDED = frozenset(f.casefold() for f in REACT_RESERVED_FILES)

# The two directories an author may write into. Anything else — a root-level file,
# a path that escapes the project with ``..``, an absolute path — is not authorable.
REACT_AUTHORABLE_PREFIXES: tuple[str, ...] = ("src/", "public/")

# Root build-shell files the author MAY write (2026-10-07, "open everything"):
# package.json, vite.config.*, bunfig.toml, .npmrc. The paw-sites generator merges
# them with its own toolchain config.
REACT_AUTHOR_ROOT_FILES: tuple[str, ...] = AUTHOR_SHELL_FILES
_AUTHOR_ROOT_FOLDED = frozenset(f.casefold() for f in REACT_AUTHOR_ROOT_FILES)


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
    folded = normalize_react_path(path).casefold()
    return (
        is_dependency_manifest_path(folded)
        or folded in _RESERVED_FOLDED
        or folded == REACT_RESERVED_PREFIX.rstrip("/")
        or folded.startswith(REACT_RESERVED_PREFIX)
    )


def is_authorable_react_path(path: str) -> bool:
    """True when ``path`` is under ``src/`` / ``public/`` or is an author root file.

    Note this is about the RESOLVED path: ``src/../README.md`` normalizes to
    ``README.md`` and is not authorable.
    """
    norm = normalize_react_path(path)
    return norm.casefold() in _AUTHOR_ROOT_FOLDED or any(
        norm.startswith(prefix) for prefix in REACT_AUTHORABLE_PREFIXES
    )


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
    if is_dependency_manifest_path(norm):
        return (
            f"`{path}` is the site's dependency manifest. Only "
            "`set_site_dependencies` writes it, because each entry is vetted against "
            "the npm registry and the supply-chain policy before it lands."
        )
    if is_reserved_react_path(norm):
        return (
            f"`{path}` resolves to `{norm}`, which the generator owns. index.html, "
            "paw-prerender.mjs and the `src/paw/` namespace carry the prerender "
            "contract that keeps the page from shipping blank without JavaScript, "
            "and lockfiles are produced by the build. Edit under `src/` (outside "
            "`src/paw/`) or `public/`, or the root build files (package.json, "
            "vite.config.*, bunfig.toml, .npmrc)."
        )
    if not is_authorable_react_path(norm):
        return (
            f"`{path}` resolves to `{norm}`, which is outside the authored source "
            "tree. A react site's own files live under `src/` or `public/`, plus "
            "the root build files (package.json, vite.config.*, bunfig.toml, .npmrc)."
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
