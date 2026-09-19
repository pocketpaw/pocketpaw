# ee/pocketpaw_ee/sites/project_zip.py — assemble a source-engine Paw Site's
# downloadable project as in-memory zip bytes.
#
# BUILT FROM THE STORED POCKET, NEVER FROM A GENERATED TREE. That is the invariant a
# future reader is most likely to break, and there are three reasons for it:
#
#   * The stored ``source`` keeps the capture PLACEHOLDERS and never holds a
#     substituted key (``generator_client.build_generator_input``: the pocket keeps
#     the placeholders either way, which is what keeps ``rotate_signed_key``
#     meaningful). This module substitutes into a COPY, on the way into the zip.
#   * ``build_job.read_generated_tree`` reads a SUBSTITUTED tree, and the scrub next
#     to it (``_scrub_secret_inputs``) covers ``siteConfig`` and not the tree.
#     Reading the pocket removes the need for a scrub rather than adding one that
#     can rot.
#   * It is the only source that exists in prod: the build dir is ephemeral and the
#     Daytona sandbox self-deletes.
#
# The capture key substituted here is NOT a secret. ``cloud/auth/site_keys.py``
# documents it as the Stripe-publishable-key model: world-visible, origin-bound,
# stored and compared in plaintext, defended by the per-site origin allowlist, a
# revocation kill switch and a narrow scope set. It already ships as a hidden
# ``paw_key`` input in published page source, so resolving it so the downloaded
# project's form actually posts is correct rather than a leak.
#
# THE BUILD SHELL IS POCKETPAW'S OWN MINIMAL RECONSTRUCTION, not a copy of the
# generator's scaffold. paw-sites owns scaffolding and is a different repo; mirroring
# ``svelte-scaffold.ts`` / ``react-scaffold.ts`` whole would be a second source of
# truth that rots. The shell is the smallest manifest + config set that makes the
# AUTHORED source install and build locally, and it carries none of the hosting: no
# wrangler.toml, no adapter-cloudflare, no edit bridge, no D1 data layer. Every
# dependency it names is pinned to ``VETTED_DEPENDENCIES`` in
# paw-sites/src/allowlist.ts. A shell file at a generator-RESERVED path (the manifest
# and the configs) overwrites an authored copy, because nothing downstream re-runs
# ``assertAllowed`` and this is the only place that invariant can be held; a shell
# file in the authorable tree is only a default, exactly as the generator treats it.
#
# No lockfile, ever: ``daytona_runner`` records that the generator deliberately emits
# none. The download is buildable, not byte-reproducible. The zip itself IS
# byte-reproducible — sorted entries, fixed timestamps — so equal inputs hash equal.
"""Build the zip of a Paw Site's project from the pocket's stored source map."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Any

from pocketpaw_ee.sites import engines, generator_client, react_paths, svelte_paths

logger = logging.getLogger(__name__)


class ProjectZipError(Exception):
    """A project zip could not be assembled."""


class ProjectZipTooLarge(ProjectZipError):
    """The planned project exceeds a cap."""


class UnsafeSourcePath(ProjectZipError):
    """A source-map key is not a safe relative path inside the project root."""


# Caps, modelled on ``api/v1/files.py::_build_zip_bytes`` (the only other real zip
# writer here) and sized to THIS input rather than to a filesystem walk.
#
# The byte cap is the physical ceiling on what a pocket can hold: ``source`` is one
# field of one Mongo document and BSON caps a document at 16 MiB, so a plan over this
# size cannot have come from a pocket read. Tripping it means an upstream assumption
# changed, which is worth refusing rather than absorbing — the whole archive is held
# in memory.
#
# The file cap bounds per-entry work the byte cap does not: a map of a million empty
# keys is tiny and still writes a million central-directory records. Real maps run
# from single digits to dozens of files (the required-key sets are 4-10), so 1000 is
# ample headroom and still a bound.
MAX_FILES = 1_000
MAX_TOTAL_BYTES = 16 * 1024 * 1024

# A Windows drive-qualified path ("C:/x", "c:x") is absolute even without a leading
# separator, so the leading-slash test alone does not catch it.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

# Mirrored from paw-sites/src/allowlist.ts ``VETTED_DEPENDENCIES`` — only the entries
# a downloadable shell names. The rest of that map is hosting toolchain the generator
# picks from what it emitted (adapter-cloudflare, valibot, @noble/hashes,
# @ripple-ui/svelte), and a project someone takes away does not deploy to our edge.
#
# Pins live here rather than being derived, because paw-sites is a different repo and
# is not importable from this process. Every shell builder looks its names up in this
# map, so adding a dependency without a vetted pin raises instead of shipping an
# unvetted package.
_VETTED_PINS: dict[str, str] = {
    "@sveltejs/adapter-static": "^3.0.10",
    "@sveltejs/kit": "^2.0.0",
    "@sveltejs/vite-plugin-svelte": "^6.0.0",
    "@tailwindcss/vite": "^4.2.2",
    "@vitejs/plugin-react": "^4.3.4",
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "svelte": "^5.0.0",
    "tailwindcss": "^4.2.2",
    "vite": "^6.0.0",
}

# (dependencies, devDependencies) per engine. html is absent on purpose: it has no
# build at all (``engines.static_output_rel("html") == "."`` — its source IS the
# served site), so a manifest here would invent the one thing that engine is defined
# by not having.
_SHELL_DEPENDENCIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "svelte": (
        (),
        (
            "@sveltejs/adapter-static",
            "@sveltejs/kit",
            "@sveltejs/vite-plugin-svelte",
            "@tailwindcss/vite",
            "svelte",
            "tailwindcss",
            "vite",
        ),
    ),
    "react": (("react", "react-dom"), ("@vitejs/plugin-react", "vite")),
}

# Every zip entry carries this timestamp instead of "now", so the same pocket
# assembles to the same bytes twice. A download the operator can hash is worth more
# than an mtime nobody reads.
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class ProjectZip:
    """An assembled project archive, ready for a caller to stream."""

    engine: str
    filename: str
    file_count: int
    data: bytes


def has_downloadable_project(engine: str | None) -> bool:
    """True when this engine keeps a source map there is a project to assemble from.

    Delegates to ``engines.is_source_engine`` rather than restating the engine list:
    that module is the one place the "source map vs rippleSpec" fact is encoded, and
    it normalizes an empty or unknown engine to ripple, so a typo'd engine answers
    False here instead of reaching the assembler with nothing to read.
    """
    return engines.is_source_engine(engine)


def _safe_rel_path(raw: object) -> str:
    """Normalize a source-map key to a relative path inside the project root.

    A source map is authored content, so a path in it is not automatically
    trustworthy. Raises rather than skipping, which is what paw-sites'
    ``assertSafeRelPath`` does with the same input: a map the generator would refuse
    to materialize should not assemble into a download either, and silently dropping
    one entry ships a project missing a file nobody was told about.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeSourcePath(f"source-map key is not a usable path: {raw!r}")
    norm = raw.strip().replace("\\", "/")
    if norm.startswith("/") or _DRIVE_PREFIX.match(norm):
        raise UnsafeSourcePath(f"source-map key is an absolute path: {raw!r}")
    segments = [seg for seg in norm.split("/") if seg not in ("", ".")]
    if not segments:
        raise UnsafeSourcePath(f"source-map key resolves to no path: {raw!r}")
    if ".." in segments:
        raise UnsafeSourcePath(f"source-map key escapes the project root: {raw!r}")
    return "/".join(segments)


def _escape_html(value: str) -> str:
    """Escape a site title for an HTML text node or attribute value."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _package_json(engine: str, site_id: str, dev_script: str) -> str:
    """The engine's manifest, with every dependency pinned from ``_VETTED_PINS``."""
    deps, dev_deps = _SHELL_DEPENDENCIES[engine]
    manifest: dict[str, Any] = {
        "name": f"paw-site-{site_id or 'project'}",
        "private": True,
        "type": "module",
        "scripts": {"dev": dev_script, "build": "vite build", "preview": "vite preview"},
    }
    if deps:
        manifest["dependencies"] = {name: _VETTED_PINS[name] for name in deps}
    manifest["devDependencies"] = {name: _VETTED_PINS[name] for name in dev_deps}
    return json.dumps(manifest, indent=2) + "\n"


def _svelte_shell(*, site_id: str, title: str) -> dict[str, str]:
    """The svelte build shell: adapter-static, Tailwind, and SvelteKit's app shell.

    adapter-STATIC rather than adapter-cloudflare, because a downloaded project is
    not deploying to our edge and adapter-cloudflare's ``_worker.js`` shell imports
    files from outside the output dir (see ``engines._SVELTE_STATIC_OUTPUT_REL``).
    This is the same adapter a static svelte Paw Site already builds on.
    """
    return {
        "package.json": _package_json("svelte", site_id, "vite dev"),
        "svelte.config.js": (
            "// The downloaded project's SvelteKit config: prerendered to static\n"
            "// files, which is what a Paw Site landing page is.\n"
            "//\n"
            "// handleMissingId / handleHttpError warn rather than fail, matching the\n"
            "// published build: a nav link to a page that does not exist yet is the\n"
            "// normal half-built state of a site under edit and must not abort the\n"
            "// whole build.\n"
            "import adapter from '@sveltejs/adapter-static';\n"
            "import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';\n"
            "\n"
            "export default {\n"
            "  preprocess: vitePreprocess(),\n"
            "  kit: {\n"
            "    adapter: adapter(),\n"
            "    prerender: { handleMissingId: 'warn', handleHttpError: 'warn' }\n"
            "  }\n"
            "};\n"
        ),
        "vite.config.ts": (
            "// tailwindcss() must precede sveltekit(), or the Tailwind v4 pipeline\n"
            "// never sees the component markup.\n"
            "import { sveltekit } from '@sveltejs/kit/vite';\n"
            "import tailwindcss from '@tailwindcss/vite';\n"
            "import { defineConfig } from 'vite';\n"
            "\n"
            "export default defineConfig({ plugins: [tailwindcss(), sveltekit()] });\n"
        ),
        "src/app.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="utf-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"    <title>{_escape_html(title)}</title>\n"
            "    %sveltekit.head%\n"
            "  </head>\n"
            "  <body>%sveltekit.body%</body>\n"
            "</html>\n"
        ),
        # Not in the authored map's required keys, and adapter-static refuses to build
        # without it: the published scaffold writes this file and a svelte source map
        # never carries one. ``csr = true`` where the published build may prune the
        # bundle — a project someone took away to develop is the one place the
        # author's onMount / use: code has to actually run.
        "src/routes/+layout.ts": ("export const prerender = true;\nexport const csr = true;\n"),
        # Where a submitted lead lands. The capture endpoint answers a native form
        # POST with a 303 to ``paw_redirect``, so without this route a successful
        # submission redirects to a 404 and reads exactly like a failure. Plain on
        # purpose: it inherits the site's own app.css rather than guessing at a
        # design, which is what the published page does.
        "src/routes/thank-you/+page.svelte": (
            "<svelte:head>\n"
            f"  <title>Thanks — {_escape_html(title)}</title>\n"
            '  <meta name="robots" content="noindex" />\n'
            "</svelte:head>\n"
            "\n"
            '<main class="paw-thanks">\n'
            "  <h1>Thanks — we got your request.</h1>\n"
            "  <p>We'll be in touch shortly.</p>\n"
            '  <p><a href="/">Back to the site</a></p>\n'
            "</main>\n"
            "\n"
            "<style>\n"
            "  .paw-thanks {\n"
            "    max-width: 32rem;\n"
            "    margin: 6rem auto;\n"
            "    padding: 0 1.25rem;\n"
            "    text-align: center;\n"
            "  }\n"
            "  .paw-thanks h1 {\n"
            "    font-size: 1.5rem;\n"
            "    margin: 0 0 0.75rem;\n"
            "  }\n"
            "  .paw-thanks p {\n"
            "    margin: 0 0 0.5rem;\n"
            "    opacity: 0.8;\n"
            "  }\n"
            "</style>\n"
        ),
    }


def _react_shell(*, site_id: str, title: str) -> dict[str, str]:
    """The react build shell: Vite, the React plugin, and a client entry.

    ``createRoot``, not the published build's ``hydrateRoot``: the deployed artifact
    is prerendered by a generator-owned three-pass SSG whose scripts live in
    paw-sites and not in the pocket, so the downloaded project renders on the client.
    It builds and runs; it is not a byte-for-byte reproduction of what the edge
    serves.
    """
    return {
        "package.json": _package_json("react", site_id, "vite"),
        "vite.config.ts": (
            "import { defineConfig } from 'vite';\n"
            "import react from '@vitejs/plugin-react';\n"
            "\n"
            "export default defineConfig({ plugins: [react()] });\n"
        ),
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="UTF-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"    <title>{_escape_html(title)}</title>\n"
            "  </head>\n"
            "  <body>\n"
            '    <div id="root"></div>\n'
            '    <script type="module" src="/src/paw/entry-client.tsx"></script>\n'
            "  </body>\n"
            "</html>\n"
        ),
        "src/paw/entry-client.tsx": (
            "import { StrictMode } from 'react';\n"
            "import { createRoot } from 'react-dom/client';\n"
            "import App from '../App';\n"
            "\n"
            "const root = document.getElementById('root');\n"
            "if (root) {\n"
            "  createRoot(root).render(\n"
            "    <StrictMode>\n"
            "      <App />\n"
            "    </StrictMode>\n"
            "  );\n"
            "}\n"
        ),
    }


def _readme(engine: str, *, title: str, tokens_resolved: bool) -> str:
    """The note that ships with the archive, saying what it is and what it is not."""
    if engine == "html":
        how = (
            "There is no build step. Open `index.html`, or serve the folder with any\n"
            "static file server. What is here is what the published site serves.\n"
        )
    else:
        how = (
            "Install and build with any npm-compatible package manager:\n"
            "\n"
            "    bun install\n"
            "    bun run dev      # local dev server\n"
            "    bun run build    # production build\n"
            "\n"
            "There is no lockfile, because the generator that built your site does not\n"
            "emit one. Your first install resolves fresh versions inside the pinned\n"
            "ranges, so the build is reproducible in shape and not byte for byte.\n"
        )
    capture = (
        "The lead-capture form posts to Paw's capture API with your site's public\n"
        "capture key filled in, the same way the published page does. That key is\n"
        "origin-bound and revocable, and it is not a secret.\n"
        if tokens_resolved
        else "The lead-capture placeholders are left unresolved: this site has no\n"
        "capture key yet, which is the case until it is first published. Publish it\n"
        "and download again if you want a working form.\n"
    )
    return (
        f"# {title}\n"
        "\n"
        "Your Paw Site's source, as it is stored — the files you and the agent\n"
        "authored, plus the minimum build configuration needed to run them locally.\n"
        "\n"
        "## Running it\n"
        "\n"
        f"{how}"
        "\n"
        "## Lead capture\n"
        "\n"
        f"{capture}"
        "\n"
        "## What is not here\n"
        "\n"
        "The hosting configuration is left out on purpose: no Cloudflare Worker\n"
        "config, no edge deploy scripts, no in-builder edit bridge, and for a\n"
        "live-data site none of the generated database layer. Those belong to the\n"
        "hosted publish, not to a project you run yourself. Deploy this anywhere that\n"
        "serves static files.\n"
    )


def _build_shell(engine: str, *, site_id: str, title: str, tokens_resolved: bool) -> dict[str, str]:
    """Every file the shell contributes for ``engine``, keyed by relative path."""
    shell: dict[str, str] = {}
    if engine == "svelte":
        shell.update(_svelte_shell(site_id=site_id, title=title))
    elif engine == "react":
        shell.update(_react_shell(site_id=site_id, title=title))
    shell["README.md"] = _readme(engine, title=title, tokens_resolved=tokens_resolved)
    return shell


def _is_reserved_path(engine: str, path: str) -> bool:
    """True when the generator OWNS this path and no source map may write it.

    Read from ``svelte_paths`` / ``react_paths`` rather than restated here, because
    those modules already mirror paw-sites' own ``isReservedPath`` and are the
    per-track path policy. This is what decides whether a shell file overrides the
    authored copy or merely supplies one: the reserved build shell (package.json and
    the configs) must win, and a file in the authorable tree — ``src/app.html``,
    ``src/routes/+layout.ts``, the thank-you route, the README — is only a default,
    because the generator lets an authored map override exactly those.
    """
    if engine == "svelte":
        return svelte_paths.is_reserved_svelte_path(path)
    if engine == "react":
        return react_paths.is_reserved_react_path(path)
    return False


def _enforce_caps(planned: dict[str, str]) -> None:
    """Refuse a plan over either cap, naming which one it hit and by how much."""
    if len(planned) > MAX_FILES:
        raise ProjectZipTooLarge(f"project has {len(planned)} files, over the {MAX_FILES} cap")
    total = sum(len(contents.encode("utf-8")) for contents in planned.values())
    if total > MAX_TOTAL_BYTES:
        raise ProjectZipTooLarge(f"project is {total} bytes, over the {MAX_TOTAL_BYTES} cap")


def plan_project_files(
    *,
    engine: str | None,
    source: dict[str, Any] | None,
    site_id: str,
    title: str,
    capture_api_base: str,
    capture_signed_key: str,
) -> dict[str, str]:
    """The exact ``{path: contents}`` the archive will hold, or raise.

    Peels the live-data binding keys off the source envelope with
    ``generator_client._split_svelte_source`` — the existing peeler, not a second copy
    of the key list. They are siblings of the file entries on the same dict, so a
    naive walk writes a file literally named ``objects``.

    Peeled for EVERY source engine rather than only svelte, which is one branch fewer
    than ``build_generator_input`` needs and reaches the same result: the peeler
    returns an unchanged map when no binding key is present, and only a svelte
    envelope ever carries one. The alternative is another ``engine == "svelte"``
    equality check, which is the thing ``engines`` exists to remove.

    Anything non-``str`` surviving that split is a malformed envelope rather than a
    file, and raises: an archive cannot hold a list, and guessing which unknown key
    was meant to be a binding is how a second, drifting key list gets born.
    """
    normalized = engines.normalize_engine(engine)
    files, _bindings = generator_client._split_svelte_source(source)
    tokens_resolved = bool(site_id and capture_signed_key)
    if tokens_resolved:
        # Substituted into a COPY — ``_resolve_capture_tokens`` returns a new map, and
        # the caller's dict is the stored pocket source.
        files = generator_client._resolve_capture_tokens(
            files,
            site_id=site_id,
            capture_api_base=capture_api_base,
            capture_signed_key=capture_signed_key,
        )

    shell = _build_shell(normalized, site_id=site_id, title=title, tokens_resolved=tokens_resolved)
    # A shell file at an AUTHORABLE path goes on first, so the authored copy wins it.
    planned: dict[str, str] = {
        path: contents
        for path, contents in shell.items()
        if not _is_reserved_path(normalized, path)
    }

    for raw, contents in files.items():
        if not isinstance(contents, str):
            raise ProjectZipError(
                f"source entry {raw!r} holds {type(contents).__name__}, not file contents"
            )
        planned[_safe_rel_path(raw)] = contents

    # A shell file at a RESERVED path goes on last, so it wins instead. Nothing
    # downstream re-runs paw-sites' ``assertAllowed``, so letting an authored
    # package.json through would put an arbitrary dependency set into a project we
    # hand out.
    for path, contents in shell.items():
        if not _is_reserved_path(normalized, path):
            continue
        if path in planned:
            logger.info("project zip: the vetted %s replaces the authored copy", path)
        planned[path] = contents

    _enforce_caps(planned)
    return planned


def _write_zip(planned: dict[str, str]) -> bytes:
    """Deflate ``planned`` into archive bytes. Synchronous — the caller offloads it.

    Deliberately synchronous for the reason ``api/v1/files.py::_build_zip_bytes`` is:
    deflating is CPU-bound, so running it inline in an async handler freezes the
    event loop, and therefore every other request, for its whole duration.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(planned):
            info = zipfile.ZipInfo(path, date_time=_FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, planned[path].encode("utf-8"))
    return buf.getvalue()


async def build_project_zip_from_source(
    *,
    engine: str | None,
    source: dict[str, Any] | None,
    site_id: str,
    title: str,
    capture_api_base: str,
    capture_signed_key: str,
    archive_name: str,
) -> ProjectZip | None:
    """Assemble the archive for one already-read pocket, or None for a no-op engine.

    ``None`` is the DEFINED no-op: ripple, and the empty or unknown engine that
    normalizes to it, keep a rippleSpec rather than a source map, so there is no
    project to hand over. It is not an error and not an empty archive — an empty zip
    would look like a download that worked.
    """
    if not has_downloadable_project(engine):
        return None
    planned = plan_project_files(
        engine=engine,
        source=source,
        site_id=site_id,
        title=title,
        capture_api_base=capture_api_base,
        capture_signed_key=capture_signed_key,
    )
    data = await asyncio.to_thread(_write_zip, planned)
    return ProjectZip(
        engine=engines.normalize_engine(engine),
        filename=f"paw-site-{archive_name}.zip",
        file_count=len(planned),
        data=data,
    )


async def build_project_zip(
    *, workspace_id: str, pocket_id: str, user_id: str
) -> ProjectZip | None:
    """Assemble the downloadable project for one pocket, or None for a ripple site.

    Tenancy is the pockets service's public ``get``, which raises NotFound /
    Forbidden for a missing or cross-tenant pocket, so this adds no isolation rules
    of its own.

    A source engine whose ``source`` is missing or empty RAISES rather than returning
    the no-op. ``sites/export.py`` states the rule this follows: something that could
    not read what it was asked for must fail and never come back empty, because a
    caller cannot otherwise tell an honest "ripple has no project" from a broken
    pocket.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import service as sites_service

    pocket = await pockets_service.get(pocket_id, user_id)
    engine = pocket.get("engine")
    if not has_downloadable_project(engine):
        return None

    source = pocket.get("source")
    if not isinstance(source, dict) or not source:
        raise ProjectZipError(
            f"pocket {pocket_id} declares engine={engine!r} but stores no source map"
        )

    site = await sites_service.canonical_site_for_pocket(workspace_id, pocket_id)
    site_id = str(site.id) if site is not None else ""
    signed_key = (site.signed_key or "") if site is not None else ""
    title = (site.name if site is not None else None) or pocket.get("name") or "Paw Site"
    return await build_project_zip_from_source(
        engine=engine,
        source=source,
        site_id=site_id,
        title=title,
        capture_api_base=sites_service._capture_base(),
        capture_signed_key=signed_key,
        archive_name=site_id or pocket_id,
    )
