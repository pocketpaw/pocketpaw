# html_paths.py — the ONE place the html-track source-map path policy lives.
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


__all__ = [
    "HTML_RESERVED_PREFIX",
    "escapes_project_root",
    "html_path_rejection",
    "is_reserved_html_path",
    "normalize_html_path",
    "reserved_html_keys",
]
