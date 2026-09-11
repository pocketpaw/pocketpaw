# svelte_paths.py — the ONE place the svelte-track source-map path policy lives.
#
# Created: 2026-09-11 (feat/sites-svelte-edit-create, SC-1) — the svelte peer of
# ``react_paths.py`` / ``html_paths.py``, written for the same reason those were:
# the EDIT lane is about to become a second WRITER of a svelte source map, and a
# second writer is only safe once the guard it needs exists in one place.
#
# Until now the svelte edit lane needed no path policy at all, and that was sound:
# ``set_svelte_source_file`` could only OVERWRITE a key that already existed, so
# every writable path had already been vetted when ``create_svelte_site`` landed the
# map. Adding ``create`` removes that property — an edit can now mint an arbitrary
# path — so "may this path be written" has to be asked, and asked before the write.
#
# It is deliberately NOT a copy of ``react_paths``. The tracks differ in both rules:
#
#   * react reserves four generator-owned files plus ``src/paw/``; svelte's
#     generator (``svelte-scaffold.ts::isReservedPath``) reserves the
#     ``src/lib/paw/`` namespace plus the three gated-site auth files, and THROWS on
#     a collision. Those are mirrored here exactly — checking in Python turns a
#     build-time throw far from the authoring turn into an actionable error.
#   * react's authored files may live under ``src/`` or ``public/``. A svelte Paw
#     Site has no ``public/`` and no ``static/`` (neither appears in the generator's
#     templates), so the authorable tree is ``src/`` alone.
#
# WHY THE BUILD SHELL IS RESERVED HERE BUT NOT IN THE SCAFFOLD. The scaffold writes
# ``package.json`` / ``vite.config.ts`` / ``svelte.config.js`` BEFORE it overlays the
# source map, with an explicit comment that a map "MAY override them if it ever ships
# its own". That tolerance is fine for create, where the whole map is authored in one
# reviewed act; it is not fine as a per-file edit verb. An edit that could write
# ``package.json`` would be a way to rewrite the dependency manifest one call at a
# time. It is NOT the only thing standing there — ``materializeSource`` step 7 runs
# ``assertAllowed()`` over whatever package.json it actually emitted, so an unvetted
# dependency still throws — but a guard that keeps the edit lane away from the build
# config entirely is cheaper than relying on that post-emit check to be the only one.
#
# NOT reserved, deliberately: ``src/app.html``. The scaffold injects the SE-1 edit
# bridge and the EP-6 leaf manifest into it at step 6, AFTER the overlay, so an
# authored app.html still receives both. The rule this module follows is to mirror
# the generator's contract rather than invent a stricter one.
"""Svelte-track source-map path policy for Paw Sites.

A svelte-engine pocket's ``source`` is a ``{relative_path: file_contents}`` map of
hand-written SvelteKit files that the paw-sites generator materializes ON TOP of a
project skeleton it owns. Two rules govern which paths an author may write:

1. **Generator-owned paths are the generator's.** The ``src/lib/paw/`` namespace and
   the gated-site auth files (``src/hooks.server.ts``, ``src/lib/auth.ts``,
   ``src/app.d.ts``) carry the session gate; the build shell (``package.json``,
   ``vite.config.ts``, ``svelte.config.js``) carries the dependency manifest and the
   adapter/prerender configuration.

2. **Authored files live under ``src/``.** Everything else at the project root
   belongs to the skeleton, so a path outside that prefix is rejected rather than
   silently written somewhere the build ignores.

Both rules are applied to the NORMALIZED path: backslashes become forward slashes
and ``.``/``..`` segments collapse (``posixpath``, not ``os.path`` — source-map keys
are POSIX-style project-relative paths regardless of the host OS). A guard a trivial
path spelling defeats is not a guard, and ``./package.json`` / ``src\\lib\\paw\\x.ts``
/ ``src/lib/paw/../paw/x.ts`` are trivial spellings.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from typing import Any

# The namespace + files paw-sites' ``svelte-scaffold.ts`` refuses to let a source
# map write (``RESERVED_NAMESPACE`` / ``RESERVED_FILES``, enforced by its
# ``isReservedPath``). Mirrored exactly — drift here means a path this module
# accepts blows up at materialize time instead.
SVELTE_RESERVED_PREFIX = "src/lib/paw/"
SVELTE_RESERVED_AUTH_FILES: tuple[str, ...] = (
    "src/hooks.server.ts",
    "src/lib/auth.ts",
    "src/app.d.ts",
)

# The build shell. The scaffold tolerates an override from a whole authored map;
# the per-file edit lane does not (see the module header for why).
SVELTE_RESERVED_SHELL_FILES: tuple[str, ...] = (
    "package.json",
    "vite.config.ts",
    "svelte.config.js",
)

SVELTE_RESERVED_FILES: tuple[str, ...] = (
    *SVELTE_RESERVED_AUTH_FILES,
    *SVELTE_RESERVED_SHELL_FILES,
)

# The one directory an author may write into. A svelte Paw Site has no ``public/``
# and no ``static/`` — neither exists in the generator's templates — so unlike the
# react peer this is a single prefix.
SVELTE_AUTHORABLE_PREFIXES: tuple[str, ...] = ("src/",)


def normalize_svelte_path(path: str) -> str:
    """Collapse a source-map key to the path the generator will actually write.

    Backslashes become forward slashes (a Windows-authored key, or an agent that
    guessed the separator) and ``.``/``..`` segments collapse. The generator
    normalizes the same way before it throws, so normalizing first is what makes
    the guards below agree with it.
    """
    return posixpath.normpath(path.replace("\\", "/"))


def is_reserved_svelte_path(path: str) -> bool:
    """True when ``path`` resolves onto a generator-owned file or namespace."""
    norm = normalize_svelte_path(path)
    return (
        norm in SVELTE_RESERVED_FILES
        or norm == SVELTE_RESERVED_PREFIX.rstrip("/")
        or norm.startswith(SVELTE_RESERVED_PREFIX)
    )


def is_authorable_svelte_path(path: str) -> bool:
    """True when ``path`` resolves inside ``src/``.

    Note this is about the RESOLVED path: ``src/../package.json`` normalizes to
    ``package.json`` and is not authorable, which is the point.
    """
    norm = normalize_svelte_path(path)
    return any(norm.startswith(prefix) for prefix in SVELTE_AUTHORABLE_PREFIXES)


def svelte_path_rejection(path: str) -> str | None:
    """Return why ``path`` may not be written, or ``None`` when it may.

    Two rejections, checked in this order because the reserved one is the more
    specific and more useful message:

      * a generator-owned path (rule 1) — names what owns it and why;
      * anything outside ``src/`` (rule 2) — catches root-level files, ``..``
        escapes and absolute paths in one check.

    Returns a message fragment the caller wraps in its own error, so the error code
    is the caller's choice and this module stays free of any dependency on the cloud
    error hierarchy (the same contract ``react_path_rejection`` keeps).
    """
    norm = normalize_svelte_path(path)
    if is_reserved_svelte_path(norm):
        return (
            f"`{path}` resolves to `{norm}`, which the generator owns. The "
            "`src/lib/paw/` namespace and the auth files (src/hooks.server.ts, "
            "src/lib/auth.ts, src/app.d.ts) are what keep a gated site's session "
            "gate from being shadowed, and the build shell (package.json, "
            "vite.config.ts, svelte.config.js) carries the dependency allowlist "
            "and the adapter/prerender configuration. Edit under `src/` outside "
            "`src/lib/paw/`."
        )
    if not is_authorable_svelte_path(norm):
        return (
            f"`{path}` resolves to `{norm}`, which is outside the authored source "
            "tree. A svelte Paw Site's own files live under `src/` — its routes "
            "under `src/routes/`, its components under `src/lib/components/`; "
            "everything else at the project root belongs to the generated skeleton."
        )
    return None


def reserved_svelte_keys(source: Mapping[str, Any]) -> list[str]:
    """Return the source-map keys that collide with a generator-owned path.

    The whole-map form, for a caller that wants to name every offending key at once
    instead of failing on the first. Returns the keys AS THE AUTHOR SPELLED THEM
    (not normalized) so the error points at something findable in the payload.
    """
    return sorted(key for key in source if is_reserved_svelte_path(key))


# ---------------------------------------------------------------------------
# Is this path reached by anything? (the two-call contract's missing half)
# ---------------------------------------------------------------------------
#
# The react and html peers each grew this after an orphan-create incident: a
# ``create`` that landed call 1 and never got call 2 returned a flat success for a
# file nothing reached, and the agent read the clean success and reported the work
# as done over a page that never changed. Svelte gets it at birth rather than after
# the same incident.
#
# Svelte is the only track where the question has TWO answers, because a svelte
# source map holds two different kinds of file:
#
#   * ``src/lib/**`` is a MODULE tree, reached by an import specifier — resolved
#     here rather than pattern-matched, so ``$lib/components/Hero.svelte`` from
#     ``src/routes/+page.svelte`` and ``./Hero.svelte`` from a sibling both resolve
#     to the one file they mean.
#   * ``src/routes/**`` is a ROUTE tree, reached by URL. SvelteKit's file-system
#     router picks a route up with nothing importing it, so an import scan would
#     call every legitimate page an orphan. What matters for a page is whether a
#     visitor can FIND it: does anything link to its URL.
#
# The verdict is advisory either way. Call 1 of a two-call add is unreferenced by
# definition at the instant it happens, so a create that refused on it would close
# the only lane that can add a page at all.

# Extensions a specifier may omit. A resolved specifier and a source-map key are
# compared with these stripped.
_SVELTE_MODULE_EXTS: tuple[str, ...] = (".svelte", ".ts", ".js", ".mjs", ".css")

# Quoted module specifiers: ``from '...'``, a side-effect or dynamic ``import '...'``
# / ``import('...')``. Deliberately NOT a general string scan — matching any quoted
# text would let body copy mentioning a component name pass for an import, and a
# false "it is referenced" is the silence this exists to break.
_SVELTE_SPECIFIER_RE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"\n]+)['"]""")

# SvelteKit's file-system routing conventions. A file whose basename starts with
# ``+`` is claimed by the router for the directory it sits in, not by an import.
_ROUTE_CONVENTION_PREFIX = "+"
_ROUTES_ROOT = "src/routes/"
# The one route file that IS a page rather than configuration for one.
_ROUTE_PAGE_FILE = "+page.svelte"

# A route segment wrapped in parentheses is a LAYOUT GROUP — organisational only,
# contributing nothing to the URL. One in square brackets is a dynamic PARAMETER.
_GROUP_SEGMENT_RE = re.compile(r"^\(.*\)$")
_PARAM_SEGMENT_RE = re.compile(r"\[.*\]")


def _strip_module_ext(path: str) -> str:
    """Drop a module extension so a specifier and a map key compare equal."""
    for ext in _SVELTE_MODULE_EXTS:
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def _resolve_specifier(importer: str, specifier: str) -> str | None:
    """Resolve one import specifier to a project-relative path, or ``None``.

    ``None`` means "not a local file" — a bare package specifier (``svelte``,
    ``@sveltejs/kit``) resolves into node_modules and can never name a source-map
    key. Relative specifiers resolve against the IMPORTER's directory, which is the
    whole reason this is a resolver and not a substring search: ``./Hero.svelte``
    means a different file depending on who wrote it.
    """
    if specifier.startswith("$lib/"):
        # SvelteKit's built-in alias, and the form the create-svelte-site skill
        # teaches — by far the dominant spelling in a real site's +page.svelte.
        return posixpath.normpath("src/lib/" + specifier[len("$lib/") :])
    if specifier == "$lib":
        return "src/lib"
    if specifier.startswith("."):
        return posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    if specifier.startswith("/"):
        return posixpath.normpath(specifier.lstrip("/"))
    return None


def route_url_for(path: str) -> str | None:
    """The URL a ``src/routes/**`` router file is served at, or ``None``.

    ``None`` means the path is not a route file whose URL can be named literally:
    it is not under ``src/routes/``, it is not a router convention file, or it
    carries a dynamic ``[param]`` segment no fixed href can be compared against.

        src/routes/+page.svelte             -> "/"
        src/routes/about/+page.svelte       -> "/about"
        src/routes/(marketing)/faq/+page.ts -> "/faq"   (group adds no segment)
        src/routes/blog/[slug]/+page.svelte -> None     (dynamic)
    """
    norm = normalize_svelte_path(path)
    if not norm.startswith(_ROUTES_ROOT):
        return None
    rel = norm[len(_ROUTES_ROOT) :]
    segments = rel.split("/")
    if not segments or not segments[-1].startswith(_ROUTE_CONVENTION_PREFIX):
        return None
    dir_segments = segments[:-1]
    if any(_PARAM_SEGMENT_RE.search(seg) for seg in dir_segments):
        return None
    url_segments = [seg for seg in dir_segments if not _GROUP_SEGMENT_RE.match(seg)]
    return "/" + "/".join(url_segments) if url_segments else "/"


def _links_to(text: str, url: str) -> bool:
    """Does ``text`` contain a link to ``url``?

    Matched on the quoted attribute value so that ``/about`` is not satisfied by a
    link to ``/about-us``, and a trailing slash is accepted because both spellings
    reach the same prerendered page.
    """
    pattern = r"""(?:href|action)\s*=\s*['"]""" + re.escape(url) + r"""/?['"]"""
    return bool(re.search(pattern, str(text)))


def svelte_path_is_referenced(source: Mapping[str, Any], path: str) -> bool:
    """Does any OTHER file in ``source`` reach ``path``?

    ``source`` is the source map AFTER the write, so the created file is present and
    is skipped — a module that imports itself is still unreachable from the page.

    Three shapes, because a svelte source map holds three kinds of file:

      * a ROUTE PAGE (``src/routes/**/+page.svelte``) is reached by URL, so the
        question is whether anything links to it. SvelteKit WILL still prerender an
        unlinked route — its default ``entries`` is every non-dynamic route — so the
        page exists either way; what an unlinked page lacks is a way for a visitor
        to find it, which is exactly what the caller needs to be told;
      * a SIBLING CONVENTION FILE (``+page.ts``, ``+layout.svelte``, ...) is claimed
        by the file-system router because of the directory it sits in, so it counts
        as reached as soon as that directory holds the ``+page.svelte`` it serves;
      * anything else (``src/lib/**``) is a MODULE, reached by an import specifier.
    """
    norm = normalize_svelte_path(path)
    others = {key: text for key, text in source.items() if normalize_svelte_path(key) != norm}

    url = route_url_for(norm)
    if url is not None:
        # ``+page.svelte`` IS the page — the thing a visitor navigates to, so the
        # question for it is whether anything links to its URL. Every other ``+``
        # file in the directory (``+page.ts``, ``+layout.svelte``, ``+error.svelte``)
        # CONFIGURES that page: the router reaches it through the directory, never
        # through a link of its own, so the page's presence is the whole answer.
        if norm.rsplit("/", 1)[-1] != _ROUTE_PAGE_FILE:
            directory = norm.rsplit("/", 1)[0]
            return any(
                normalize_svelte_path(key).rsplit("/", 1)[0] == directory
                and normalize_svelte_path(key).rsplit("/", 1)[-1] == _ROUTE_PAGE_FILE
                for key in others
            )
        if url == "/":
            return True  # the root page is the site's entry; nothing needs to link it
        return any(_links_to(text, url) for text in others.values())

    target = _strip_module_ext(norm)
    for key, text in others.items():
        importer = normalize_svelte_path(key)
        for specifier in _SVELTE_SPECIFIER_RE.findall(str(text)):
            resolved = _resolve_specifier(importer, specifier)
            if resolved is not None and _strip_module_ext(resolved) == target:
                return True
    return False


__all__ = [
    "SVELTE_AUTHORABLE_PREFIXES",
    "SVELTE_RESERVED_AUTH_FILES",
    "SVELTE_RESERVED_FILES",
    "SVELTE_RESERVED_PREFIX",
    "SVELTE_RESERVED_SHELL_FILES",
    "is_authorable_svelte_path",
    "is_reserved_svelte_path",
    "normalize_svelte_path",
    "reserved_svelte_keys",
    "route_url_for",
    "svelte_path_is_referenced",
    "svelte_path_rejection",
]
