# html_paths.py — the ONE place the html-track source-map path policy lives.
#
# Updated: 2026-09-06 (feat/fx-mcp-server) — ``_html_references`` also scans ES
# ``import ... from '...'`` / ``import '...'`` / ``import('...')`` so a file reached
# only through a module import (an ``_fx/`` effect's vendor dep) is not flagged
# ``unreferenced``.
#
# Updated: 2026-09-01 (fix/sites-html-orphan-create) — added
# ``html_path_is_referenced``, the first question in this module about REACHABILITY
# rather than policy: does anything in the site point at that file. It lives here
# because the answer is html's alone. The react peer resolves import specifiers; an
# html site has no module graph to walk, so this resolves URL references (href / src / srcset /
# poster, CSS url() and @import) against the referring file's directory, and honours
# the directory-index rule the preview resolver uses. See the section comment above
# the function for the incident and the three rules that follow from it.
#
# Created: 2026-08-13 (feat/sites-html-edit-lane, HE-10) — the html peer of
# ``react_paths.py``, written for the same reason that module was extracted: the
# EDIT lane is a second writer of a source map that until now only ``create``
# wrote, and a second writer carrying its own copy of the guard is how the guard
# rots.
#
# It is deliberately NOT a copy of ``react_paths``. The two engines have opposite
# shapes, and copying react's rules onto html would have rejected almost every
# legitimate html edit:
#
#   * react's authored files must live under ``src/`` or ``public/`` — everything
#     at the project root belongs to a generated build shell. An html site HAS no
#     build shell: ``html-scaffold.ts`` writes the author's map verbatim into the
#     servable directory, so ``index.html`` and ``styles.css`` at the ROOT are the
#     normal case. Porting react's positive rule here would reject the entry
#     document of every html site.
#   * react reserves four generator-owned files plus ``src/paw/``. html reserves
#     exactly ONE namespace, ``_paw/``, and reserves nothing else — there is no
#     ``package.json`` on this track to protect, because there is no build.
#
# So what transfers is the STRUCTURE (normalize first, one verdict function, no
# dependency on the cloud error hierarchy) and not the rules.
"""html-track source-map path policy for Paw Sites.

An html-engine pocket's ``source`` is a ``{relative_path: file_contents}`` map of
raw HTML/CSS/JS that the paw-sites generator materializes **verbatim** into the
directory the edge serves. There is no framework, no build, and no generated
shell the author writes on top of — the map IS the site.

That leaves exactly two rules, both mirrored from ``html-scaffold.ts``'s
``assertWritablePath``, which throws at materialize time:

1. **The path may not escape the project root.** ``../etc/passwd`` and absolute
   paths are rejected. The generator asserts this too; checking here turns a
   build-time throw far from the authoring turn into an actionable error.

2. **``_paw/`` belongs to the generator.** The HE-7/HE-8 editing artifacts live
   there — most importantly ``_paw/edit-manifest.json``, the uid→byteSpan map the
   native editor drives its write path from. An author who could write under
   ``_paw/`` could shadow that manifest, and a shadowed manifest does not fail
   loudly: it points valid uids at wrong byte offsets, so the next native edit
   splices into the middle of a tag.

Both rules apply to the NORMALIZED path (``posixpath``, not ``os.path`` —
source-map keys are POSIX-style project-relative paths whatever the host OS is),
because a guard a trivial spelling defeats is not a guard: ``_paw/./x`` and
``_paw\\x`` and ``foo/../_paw/x`` all resolve into the reserved namespace.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping

# The namespace the html generator owns. Mirrors ``RESERVED_NAMESPACE`` in
# paw-sites' html-scaffold.ts, which throws on a collision. Stored WITH the
# trailing slash so the guard matches the directory and not a sibling that merely
# shares the prefix — ``_pawprint.html`` is a perfectly ordinary page name and
# must stay writable.
HTML_RESERVED_PREFIX = "_paw/"


def normalize_html_path(path: str) -> str:
    """Collapse a source-map key to the path the generator will actually write.

    Backslashes become forward slashes (a Windows-authored key, or an agent that
    guessed the separator) and ``.``/``..`` segments collapse. The generator
    normalizes the same way before it throws, so normalizing first is what makes
    this module agree with it.
    """
    return posixpath.normpath(path.replace("\\", "/"))


def is_reserved_html_path(path: str) -> bool:
    """True when ``path`` resolves onto the generator-owned ``_paw/`` namespace.

    Matches the directory itself as well as anything under it, so neither
    ``_paw`` nor ``_paw/edit-manifest.json`` is writable.
    """
    norm = normalize_html_path(path)
    return norm == HTML_RESERVED_PREFIX.rstrip("/") or norm.startswith(HTML_RESERVED_PREFIX)


def escapes_project_root(path: str) -> bool:
    """True when ``path`` resolves outside the project directory.

    Two ways out, and both are checked on the NORMALIZED path: a ``..`` that
    survives normalization (``../x``, ``a/../../x``), and an absolute path
    (``/etc/passwd``). A ``..`` in the middle that normalization absorbs
    (``a/../b`` → ``b``) is not an escape and is allowed through.
    """
    norm = normalize_html_path(path)
    return norm == ".." or norm.startswith("../") or posixpath.isabs(norm)


def html_path_rejection(path: str) -> str | None:
    """Return why ``path`` may not be written, or ``None`` when it may.

    The single-path verdict the edit lane needs. Checked escape-first: a path that
    leaves the project is the more fundamental problem, and naming the reserved
    namespace for ``../_paw/x`` would point the author at the wrong rule.

    Returns a message fragment the caller wraps in its own error, so the error
    code stays the caller's choice (the sites service raises two distinct codes)
    and this module keeps no dependency on the cloud error hierarchy — the same
    contract ``react_paths.react_path_rejection`` holds.
    """
    norm = normalize_html_path(path)
    if escapes_project_root(path):
        return (
            f"`{path}` resolves to `{norm}`, which is outside the site directory. "
            "An html site's files are project-relative — `index.html`, "
            "`styles.css`, `about/index.html`. A path that climbs out of the "
            "project with `..`, or an absolute path, is not writable."
        )
    if is_reserved_html_path(path):
        return (
            f"`{path}` resolves to `{norm}`, which is inside the generator-owned "
            "`_paw/` namespace. That is where the editing artifacts live — "
            "`_paw/edit-manifest.json` maps each editable element to a byte range "
            "in your source, and the visual editor writes through it. Shadowing "
            "it would point valid elements at wrong offsets. Write your own files "
            "anywhere else, including the project root."
        )
    if not norm or norm == ".":
        return (
            f"`{path}` does not name a file. Give the path relative to the site "
            "root, like `index.html` or `css/site.css`."
        )
    return None


def reserved_html_keys(source: dict[str, object]) -> list[str]:
    """Return the source-map keys that collide with the generator-owned namespace.

    The whole-map form, the peer of ``react_paths.reserved_react_keys``. Returns
    the keys AS THE AUTHOR SPELLED THEM (not normalized) so the error points at
    something findable in the payload.
    """
    return sorted(key for key in source if is_reserved_html_path(key))


# ---------------------------------------------------------------------------
# Is this file reached by anything? (the two-call contract's missing half)
# ---------------------------------------------------------------------------
#
# Added 2026-09-01 (fix/sites-html-orphan-create), the peer of
# ``react_paths.react_path_is_referenced`` and added for the same incident one
# engine over: a ``create`` that landed a file nothing points at returned the same
# flat success as a change that is actually visible, so the caller reported work the
# user could not find. "Add an about page" is a new FILE plus a LINK from an
# existing one, and only the file half had a tool result.
#
# The QUESTION transfers; the ANSWER does not. An html site is not a module tree —
# nothing imports anything — so resolving import specifiers here would find nothing
# and call every page an orphan. Files are reached by URL: ``href`` / ``src`` /
# ``srcset`` / ``poster`` in markup, ``url()`` and ``@import`` in CSS. Three rules
# make that scan agree with what the server actually does:
#
#   1. RESOLVE, never substring-match. A reference is relative to the REFERRING
#      file's directory, so ``../contact.html`` names different files depending on
#      who wrote it, and ``https://example.com/about/`` is not a link to the local
#      ``about/index.html`` however much of the path it happens to contain. A
#      substring scan gets that backwards precisely where it matters: an author who
#      has not linked the new page yet is exactly the author with a stale external
#      link still sitting in the markup.
#   2. A DIRECTORY LINK REACHES ITS INDEX. Nobody writes ``/about/index.html`` in a
#      nav; they write ``/about``. ``artifact_preview.resolve`` serves
#      ``resolved / "index.html"`` for that request, so this resolves it the same
#      way. Without the alias every correctly linked page reads as unreachable.
#   3. OFF-SITE SCHEMES ARE NOT FILES. ``mailto:``, ``tel:``, ``data:``, ``http(s)``
#      and a bare ``#fragment`` name nothing in the source map.

# Attribute references. ``poster`` is included because a hero <video> names its
# still frame there, and that still is a real file in the map.
_HTML_REF_ATTR_RE = re.compile(r"""\b(?:href|src|poster)\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
# ``srcset`` holds a comma-separated list of "<url> <descriptor>" pairs, so its
# value is split further below rather than used whole.
_HTML_SRCSET_RE = re.compile(r"""\bsrcset\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
# CSS reaches files two ways, and a background image is as real a reference as an
# ``<img src>``. Quotes inside ``url()`` are optional, hence the optional group.
_CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"""@import\s+['"]([^'"]+)['"]""", re.IGNORECASE)
# ES module imports reach files too: a page's <script type="module"> or an
# ``_fx/`` effect's index.js pulling in a vendor file. Static ``import x from '…'``
# / bare ``import '…'`` and dynamic ``import('…')`` both count.
_JS_IMPORT_RE = re.compile(r"""\bimport\s+(?:[^'";]*?\bfrom\s+)?['"]([^'"]+)['"]""")
_JS_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)""")

# Schemes that name something outside this site. ``//`` is protocol-relative (still
# off-site) and a leading ``#`` is a fragment on the current page.
_OFF_SITE_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "//",
    "data:",
    "mailto:",
    "tel:",
    "javascript:",
    "#",
)


def _html_references(text: str) -> list[str]:
    """Every candidate URL reference in one file, as written."""
    refs: list[str] = list(_HTML_REF_ATTR_RE.findall(text))
    for value in _HTML_SRCSET_RE.findall(text):
        for candidate in value.split(","):
            parts = candidate.split()
            if parts:
                refs.append(parts[0])
    refs.extend(_CSS_URL_RE.findall(text))
    refs.extend(_CSS_IMPORT_RE.findall(text))
    refs.extend(_JS_IMPORT_RE.findall(text))
    refs.extend(_JS_DYNAMIC_IMPORT_RE.findall(text))
    return refs


def _resolve_html_ref(referrer: str, ref: str) -> str | None:
    """Resolve one reference to a project-relative path, or ``None`` when it names
    nothing in this site (an off-site scheme, a bare fragment, an empty href).

    The empty string is a real answer, not a failure: ``/`` and ``./`` resolve to
    the site root, which is served by the root ``index.html``.
    """
    ref = ref.strip()
    if not ref or ref.lower().startswith(_OFF_SITE_PREFIXES):
        return None
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    if not ref:
        return None
    if ref.startswith("/"):
        resolved = posixpath.normpath(ref.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(referrer), ref))
    return "" if resolved in (".", "/") else resolved


def html_path_is_referenced(source: Mapping[str, object], path: str) -> bool:
    """Does any OTHER file in ``source`` point at ``path``?

    ``source`` is the map AFTER the write, so the written file is present and is
    skipped — a page whose only link to itself is its own is still unreachable.
    """
    norm = normalize_html_path(path)

    # The request path a directory link resolves to (rule 2). The root
    # ``index.html`` is reached by ``/``, which resolves to "".
    if norm == "index.html":
        dir_alias: str | None = ""
    elif norm.endswith("/index.html"):
        dir_alias = norm[: -len("/index.html")]
    else:
        dir_alias = None

    for key, text in source.items():
        referrer = normalize_html_path(key)
        if referrer == norm:
            continue
        for ref in _html_references(str(text)):
            resolved = _resolve_html_ref(referrer, ref)
            if resolved is None:
                continue
            if resolved == norm or (dir_alias is not None and resolved == dir_alias):
                return True
    return False


__all__ = [
    "HTML_RESERVED_PREFIX",
    "escapes_project_root",
    "html_path_rejection",
    "is_reserved_html_path",
    "normalize_html_path",
    "reserved_html_keys",
]
