# dependency_manifest.py — the shape and the rules of ``paw.dependencies.json``.
#
# Authors declare npm packages on the svelte, react and html tracks. The resolved
# set rides the pocket's own source map as ONE reserved file, ``paw.dependencies.json``,
# so draft versioning, revert, the preview content hash, the project zip and
# ``read_site_source`` all see it without a second storage field. Cross-repo
# contract: docs/design/drafts/2026-09-24-sites-deps-verify-contract.md §1/§2.
# ``TOOLCHAIN_RESERVED`` / ``MAX_DECLARED_PACKAGES`` come from the vendored paw-sites
# allowlist (package data, so still network-free).
#
# It also owns ``requires_sandbox``, the one host-vs-sandbox predicate: a source that
# declares packages OR carries any authored build-shell file (package.json,
# vite.config.*, svelte.config.*, bunfig.toml, .npmrc) never builds on the API host.
# Invariant: every caller deciding host vs sandbox uses it, never
# ``has_author_dependencies`` alone, and it fails closed.
#
# Network-free and dependency-free: imported by the path policies, by
# ``generator_client`` (the host-install guard), ``service``/``verify`` (routing) and
# ``project_zip``; the registry resolver lives in ``dependency_resolver`` and imports
# THIS, never the other way round.
"""The ``paw.dependencies.json`` manifest: path, schema, and name/version rules.

Only the resolver writes this file (``sites.service.set_site_dependencies`` and the
create tools' ``dependencies`` param). Every generic edit lane treats the path as
reserved, and the paw-sites generator re-validates whatever it finds, so the rules
here are the UX layer and the generator is the gate.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from typing import Any

from pocketpaw_ee.sites.vetted_pins import VENDORED_ALLOWLIST

#: Where the manifest lives: the ROOT of the source map on every engine. On svelte
#: that is outside ``src/`` on purpose (contract §1).
DEPENDENCY_MANIFEST_PATH = "paw.dependencies.json"

#: The manifest schema version this module writes and reads.
MANIFEST_SCHEMA = 1

#: paw-sites ``MAX_AUTHOR_PACKAGES``, read from the vendored allowlist. The resolver no
#: longer enforces it (2026-10-07, "open everything"); it stays only until the vendored
#: allowlist is re-vendored from the paw-sites release that drops the cap.
MAX_DECLARED_PACKAGES: int = int(VENDORED_ALLOWLIST["maxAuthorPackages"])

#: npm's own ceiling on a package name.
MAX_NAME_LENGTH = 214

#: An EXACT semver, the only version shape the manifest may carry.
EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")

# validate-npm-package-name's "new package" rules, as one pattern: lowercase,
# URL-safe, no leading dot or underscore, an optional single ``@scope/`` prefix.
_NAME_RE = re.compile(r"^(?:@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*$")

# Names npm refuses outright, plus the node core modules a bare import would
# shadow. Not exhaustive for node's whole stdlib — the registry refuses the rest.
_BLOCKED_NAMES = frozenset(
    {
        "node_modules",
        "favicon.ico",
        "fs",
        "path",
        "http",
        "https",
        "os",
        "crypto",
        "child_process",
        "net",
        "stream",
        "util",
        "url",
        "events",
        "buffer",
        "process",
    }
)

#: Toolchain-owned packages an author may not declare (contract §2), from paw-sites
#: ``TOOLCHAIN_RESERVED``. A string ending in ``/`` reserves the whole scope (paw-sites
#: writes ``@scope/*``). paw-sites' ``engineToolchainReserved`` (motion, ripple only)
#: needs no twin here: ripple is not in ``DEPENDENCY_ENGINES``.
_RESERVED_RAW: list[str] = VENDORED_ALLOWLIST["toolchainReserved"]
# A '*' anywhere but a trailing '/*' would be a pattern this module cannot honour.
# A raise, not an assert, so it still fires under python -O.
if any("*" in e and not (e.endswith("/*") and e.count("*") == 1) for e in _RESERVED_RAW):
    raise ValueError(f"unsupported toolchainReserved pattern in allowlist: {_RESERVED_RAW}")
TOOLCHAIN_RESERVED: tuple[str, ...] = tuple(entry.removesuffix("*") for entry in _RESERVED_RAW)

#: Every spelling of the Vite config the generator reserves (paw-sites PS-1).
VITE_CONFIG_FILES: tuple[str, ...] = tuple(
    f"vite.config.{ext}" for ext in ("ts", "js", "mjs", "mts", "cjs", "cts")
)

#: Install configuration an author MAY ship at the project root (2026-10-07). It
#: only takes effect in the Daytona sandbox; a host install displaces bunfig.toml
#: with the host floor (``bun_supply_chain.write_host_bunfig``).
AUTHOR_INSTALL_CONFIG_FILES: tuple[str, ...] = ("bunfig.toml", ".npmrc")

#: Lockfiles stay generator-owned: the generator merges the author's package.json
#: with its toolchain, so an authored lockfile would pin a tree that no longer matches.
LOCKFILES: tuple[str, ...] = ("bun.lock", "bun.lockb", "package-lock.json")

#: Every install-config file (the legacy build-shell classifier still recognises all
#: of them in old source maps).
INSTALL_CONFIG_FILES: tuple[str, ...] = (*AUTHOR_INSTALL_CONFIG_FILES, *LOCKFILES)

#: Build-shell files an author may write at the project root on svelte and react
#: (2026-10-07). The paw-sites generator merges them with its toolchain.
AUTHOR_SHELL_FILES: tuple[str, ...] = (
    "package.json",
    *VITE_CONFIG_FILES,
    *AUTHOR_INSTALL_CONFIG_FILES,
)

#: Every spelling of the SvelteKit config. Only ``svelte.config.js`` is authorable,
#: but the sandbox-routing check matches them all so a spelling cannot slip past it.
SVELTE_CONFIG_FILES: tuple[str, ...] = tuple(
    f"svelte.config.{ext}" for ext in ("js", "ts", "mjs", "mts", "cjs", "cts")
)

#: Root files whose presence in a source map forces the build into the sandbox
#: (:func:`requires_sandbox`): every author-writable build-shell file. Each one
#: changes what ``bun install`` / ``bun run build`` fetches or executes.
SANDBOX_ONLY_SHELL_FILES: tuple[str, ...] = (*AUTHOR_SHELL_FILES, *SVELTE_CONFIG_FILES)
_SANDBOX_ONLY_FOLDED = frozenset(f.casefold() for f in SANDBOX_ONLY_SHELL_FILES)

#: Engines whose authored code can import a declared package. ripple has no
#: authored code, so it refuses a manifest.
DEPENDENCY_ENGINES: tuple[str, ...] = ("svelte", "react", "html")


def jsdelivr_esm_url(name: str, version: str) -> str:
    """The exact ESM URL an html manifest entry must carry (contract §1)."""
    return f"https://cdn.jsdelivr.net/npm/{name}@{version}/+esm"


def is_dependency_manifest_path(path: str) -> bool:
    """True when ``path`` resolves onto the manifest, in any spelling.

    Normalized like the path policies (backslashes, ``.``/``..`` segments) and
    compared case-insensitively: a case-insensitive filesystem would land
    ``PAW.Dependencies.JSON`` on the same file, and a guard a spelling defeats is
    not a guard.
    """
    if not isinstance(path, str):
        return False
    norm = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    return norm.casefold() == DEPENDENCY_MANIFEST_PATH


def npm_name_problem(name: object) -> str | None:
    """Why ``name`` is not a valid npm package name, or ``None`` when it is."""
    if not isinstance(name, str) or not name:
        return "the package name is empty"
    if len(name) > MAX_NAME_LENGTH:
        return f"the package name is longer than {MAX_NAME_LENGTH} characters"
    if name != name.strip():
        return "the package name has leading or trailing spaces"
    if name != name.lower():
        return "npm package names are lowercase"
    if name.startswith((".", "_")):
        return "a package name cannot start with '.' or '_'"
    if name in _BLOCKED_NAMES:
        return f"`{name}` is a reserved or node core module name"
    if not _NAME_RE.match(name):
        return (
            "the package name has characters npm does not allow (use lowercase "
            "letters, digits, '-', '.', '_' and an optional '@scope/')"
        )
    return None


def is_toolchain_reserved(name: str) -> bool:
    """True when ``name`` belongs to the build toolchain (contract §2)."""
    for entry in TOOLCHAIN_RESERVED:
        if entry.endswith("/"):
            if name.startswith(entry):
                return True
        elif name == entry:
            return True
    return False


def parse_manifest(text: object) -> dict[str, dict[str, str]]:
    """The ``packages`` map of a manifest, or ``{}`` for an absent/empty one.

    Raises ``ValueError`` for a manifest that is present but malformed, so a caller
    never mistakes a broken file for "no dependencies".
    """
    if text is None:
        return {}
    if not isinstance(text, str):
        raise ValueError("paw.dependencies.json must be a JSON string")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"paw.dependencies.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("paw.dependencies.json must be a JSON object")
    packages = data.get("packages", {})
    if not isinstance(packages, dict):
        raise ValueError("paw.dependencies.json `packages` must be an object")
    out: dict[str, dict[str, str]] = {}
    for name, entry in packages.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
            raise ValueError(f"paw.dependencies.json entry `{name}` has no version")
        out[str(name)] = {k: v for k, v in entry.items() if isinstance(v, str)}
    return out


def render_manifest(packages: Mapping[str, Mapping[str, str]]) -> str:
    """Serialize ``packages`` as the manifest file's text (sorted, stable)."""
    body = {
        "schema": MANIFEST_SCHEMA,
        "packages": {name: dict(packages[name]) for name in sorted(packages)},
    }
    return json.dumps(body, indent=2, sort_keys=False) + "\n"


def manifest_text(source: Mapping[str, Any] | None) -> str | None:
    """The manifest file's text from a source map, whatever key spelling holds it."""
    if not isinstance(source, Mapping):
        return None
    for key, value in source.items():
        if is_dependency_manifest_path(key) and isinstance(value, str):
            return value
    return None


def has_author_dependencies(source: Mapping[str, Any] | None) -> bool:
    """True when ``source`` carries a manifest that is not provably empty.

    The sandbox-only guard keys on this, so it FAILS CLOSED: a malformed manifest
    counts as "has dependencies". The only way to answer False is to hold no
    manifest, or one that parses to zero packages.
    """
    text = manifest_text(source)
    if text is None:
        return False
    try:
        return bool(parse_manifest(text))
    except ValueError:
        return True


def author_build_shell_files(source: Mapping[str, Any] | None) -> list[str]:
    """The source-map keys that land on an author-writable build-shell file.

    Normalized like :func:`is_dependency_manifest_path` (backslashes, ``.``/``..``
    segments, a leading ``/``) and compared case-insensitively, with trailing dots
    and spaces dropped because Windows drops them too: any spelling that would
    materialize as ``package.json`` at the project root counts.
    """
    if not isinstance(source, Mapping):
        return []
    hits: list[str] = []
    for key in source:
        if not isinstance(key, str):
            continue
        norm = posixpath.normpath(key.replace("\\", "/")).lstrip("/").rstrip(". ")
        if norm.casefold() in _SANDBOX_ONLY_FOLDED:
            hits.append(key)
    return sorted(hits)


def requires_sandbox(source: Mapping[str, Any] | None) -> bool:
    """True when building ``source`` must happen in the Daytona sandbox, never the host.

    The single host-vs-sandbox predicate: the host-install refusal, the publish
    routing and verify all key on it. A source needs the sandbox when it declares
    packages (:func:`has_author_dependencies`, fail-closed) OR carries any authored
    build-shell file (package.json, vite.config.*, svelte.config.*, bunfig.toml,
    .npmrc). Those files steer the install and the build: an authored ``.npmrc``
    can point bun at another registry and send it a token read from the host env.
    """
    return has_author_dependencies(source) or bool(author_build_shell_files(source))


def author_packages(source: Mapping[str, Any] | None) -> dict[str, str]:
    """``{name: exact_version}`` for a source map's manifest, ``{}`` when absent.

    Skips an entry whose version is not exact rather than passing a range along;
    the generator refuses those anyway, and a caller building a package.json from
    this must never float a version.
    """
    try:
        packages = parse_manifest(manifest_text(source))
    except ValueError:
        return {}
    return {
        name: entry["version"]
        for name, entry in packages.items()
        if EXACT_VERSION_RE.match(entry.get("version", "")) and npm_name_problem(name) is None
    }


__all__ = [
    "AUTHOR_INSTALL_CONFIG_FILES",
    "AUTHOR_SHELL_FILES",
    "SANDBOX_ONLY_SHELL_FILES",
    "SVELTE_CONFIG_FILES",
    "DEPENDENCY_ENGINES",
    "LOCKFILES",
    "DEPENDENCY_MANIFEST_PATH",
    "EXACT_VERSION_RE",
    "INSTALL_CONFIG_FILES",
    "VITE_CONFIG_FILES",
    "MANIFEST_SCHEMA",
    "MAX_DECLARED_PACKAGES",
    "TOOLCHAIN_RESERVED",
    "author_build_shell_files",
    "author_packages",
    "has_author_dependencies",
    "is_dependency_manifest_path",
    "is_toolchain_reserved",
    "jsdelivr_esm_url",
    "manifest_text",
    "npm_name_problem",
    "parse_manifest",
    "requires_sandbox",
    "render_manifest",
]
