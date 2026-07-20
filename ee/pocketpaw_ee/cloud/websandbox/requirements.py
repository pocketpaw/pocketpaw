# requirements.py — resolve what a PROJECT NEEDS from a runtime, before any
# runtime boots (RR-2).
# Created 2026-07-20 (feat/code-runtime-requirements).
# Modified 2026-07-21 — ``nativeToolchain`` is now DERIVED from the manifest
# instead of hardcoded true. See the note above ``_NATIVE_TOOLCHAIN_PACKAGES``
# for what the blanket assumption cost: it made every in-tab runtime
# unselectable for every project, which is the whole point of the registry.
#
# WHY THIS EXISTS: Code Mode picks an execution runtime (Daytona cloud VM,
# WebContainers in-tab, …) by matching what a project NEEDS against what each
# runtime CAN DO. That match is only useful if the "needs" side can be answered
# BEFORE anything boots — otherwise the probe is chicken-and-egg. The old
# BrowserPod path proved the point the expensive way: it booted a whole VM and
# seeded an entire repo just to read the root file list, discover the project was
# not Node-shaped, and throw the VM away. The backend already fetches repo source
# (see ``archive.py``), so it is the cheapest place in the system to look.
#
# WHY THE ANSWER CARRIES REASONS: a routing decision that cannot be explained is
# indistinguishable from a guess, and this one is user-visible — it decides
# whether their project opens in a fast in-tab runtime or a slower real VM. Every
# flag we raise carries the EVIDENCE that raised it ("pg in dependencies ->
# rawSockets"), so a wrong route can be debugged from the response alone instead
# of by re-deriving the inference by hand.
#
# WHY UNKNOWN MEANS MOST-CAPABLE: when we cannot tell — no package.json, GitHub
# unreachable, a repo we cannot read — we assume the project needs EVERYTHING and
# route it to the most capable runtime. The asymmetry is deliberate: a slow but
# correct sandbox costs the user time, a fast but broken one costs them the
# session. Failing to inspect must never block opening a project either, so an
# unreachable repo is a defaulted answer with a reason, not a 5xx.
#
# EFFICIENCY: this deliberately does NOT reuse ``fetch_repo_archive`` — that pulls
# up to 100MB of zip to answer a question about one small file. We fetch exactly
# ``package.json`` through GitHub's contents API.
#
# SSRF: identical boundary to ``archive.py``, and reusing its parser rather than
# re-implementing one is the point — ``_parse_github_repo`` extracts owner/name
# and the GitHub URL is BUILT here from those validated parts. No caller-supplied
# string ever reaches httpx as a URL. The ref is likewise validated and then
# passed as an httpx *query param* (encoded by the client), never spliced into a
# path.
from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.websandbox.archive import _parse_github_repo
from pocketpaw_ee.cloud.websandbox.dto import RuntimeRequirementsResponse

logger = logging.getLogger(__name__)

# Per-operation, with a much tighter CONNECT budget. httpx applies a bare float
# to every phase separately, so 15.0 alone let a slow-drip responder hold a
# project open far past 15s. This probe fetches one small file and its fallback
# is both correct and cheap, so failing fast costs nothing.
_FETCH_TIMEOUT = httpx.Timeout(8.0, connect=3.0)

# The probe is UNAUTHENTICATED, which means GitHub's 60 requests/hour limit keyed
# on our egress IP — shared across every tenant on a deploy. Past that, every
# probe 403s, every project defaults to most-capable, and the feature silently
# stops adding value while still reporting success.
#
# This cache is the containment, not the cure: a repo's manifest barely changes,
# so repeat opens (the common case) cost nothing and the quota only pays for
# distinct repos. The cure is authenticating the probe, which raises the limit to
# 5000/hour and unlocks private repos — but archive.py is unauthenticated for the
# same reason, so that is one shared piece of work rather than a fix belonging to
# this module. Tracked as a follow-up.
_CACHE_TTL_SECONDS = 15 * 60
_CACHE_MAX_ENTRIES = 512
_manifest_cache: OrderedDict[tuple[str, str, str], tuple[float, str | None]] = OrderedDict()

# A package.json is a manifest, not a payload. Anything past this is either not a
# manifest or not something we should be parsing on the request path.
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024

# Node packages that open a RAW TCP SOCKET (or load a native driver that does) to
# reach a database or broker. This is the capability line an in-tab runtime cannot
# cross: WebContainers and its peers emulate networking over browser-reachable
# transports (fetch / WebSocket relays) and expose no real ``net.Socket``, so a
# project that talks to Postgres or Redis directly can only run in a real VM.
#
# Each entry carries the CAUSE it raises the flag for, because the reason string
# is the product here. This table must contain ONLY packages that open a real
# network socket — nothing else, however much it might also need a VM.
#
# sqlite3 and better-sqlite3 used to live here (as "native FFI, honest reason"),
# because before ``nativeToolchain`` was derived, rawSockets was the ONLY lever
# that could force an embedded-database project onto a real VM. That is no longer
# true: the native-toolchain table below now catches them for the real reason
# they need a machine (they compile), so keeping them here would emit a
# ``-> rawSockets`` reason for a package that opens no socket — the exact
# mislabel this comment used to describe as "the worst combination: it survives
# review and then sends the next debugger hunting a network problem that does not
# exist". The routing was always safe (a VM either way); the EVIDENCE was wrong,
# and now that a correct lever exists the wrong one is simply removed.
#
# ORMs matter more than drivers. Real Node apps reach Postgres through Prisma or
# Drizzle, and `pg` is then a TRANSITIVE dependency that never appears in
# package.json at all. Matching only bare driver names returned a confident
# `rawSockets: false` for a Prisma+Postgres app — under-provisioning, which this
# module's header says must never happen: it strands the user at first query,
# whereas over-provisioning only costs a slower sandbox.
_SOCKET = "opens a network socket to a database or broker"
_ORM = "is a database client whose driver is a transitive dependency"

_RAW_SOCKET_PACKAGES: dict[str, str] = {
    # Drivers — the direct case.
    "pg": _SOCKET,
    "pg-promise": _SOCKET,
    "mysql": _SOCKET,
    "mysql2": _SOCKET,
    "mssql": _SOCKET,
    "mongodb": _SOCKET,
    "mongoose": _SOCKET,
    "redis": _SOCKET,
    "ioredis": _SOCKET,
    "cassandra-driver": _SOCKET,
    "oracledb": _SOCKET,
    "tedious": _SOCKET,
    "amqplib": _SOCKET,
    "kafkajs": _SOCKET,
    "nats": _SOCKET,
    "mqtt": _SOCKET,
    "ssh2": _SOCKET,
    # SMTP is a raw socket like any other.
    "nodemailer": _SOCKET,
    # ORMs / query builders — the common case, and the one bare-driver matching
    # missed entirely.
    "prisma": _ORM,
    "drizzle-orm": _ORM,
    "sequelize": _ORM,
    "typeorm": _ORM,
    "knex": _ORM,
    # NOTE: sqlite3 / better-sqlite3 are deliberately NOT here — they open no
    # socket. They are in _NATIVE_TOOLCHAIN_PACKAGES, which is the honest reason
    # they need a VM. See the comment above.
}

# Scoped packages can never match a bare name, so they are matched by prefix.
# `@prisma/client` is the one almost every Prisma project actually declares.
_RAW_SOCKET_PREFIXES: dict[str, str] = {
    "@prisma/": _ORM,
    "@databases/": _ORM,
}

# Packages that genuinely need a NATIVE TOOLCHAIN — a compiler, or a prebuilt
# machine-code binary with no portable fallback.
#
# WHAT THIS REPLACED, because the distinction is the entire fix: this probe used
# to return ``nativeToolchain=True`` for EVERY manifest, on the reasoning that
# "any non-trivial tree pulls prebuilt native binaries (esbuild, rollup's native
# bindings, sharp, node-gyp fallbacks)". That sentence quietly merges two
# different facts under one flag:
#
#   • esbuild and rollup ship prebuilt binaries AND a WASM/JS fallback. They run
#     in an in-tab runtime. Our own 2026-07-18 WebContainers gate run is the
#     evidence: `npm install` exit 0 across 320 packages, then a working Vite
#     dev server on the rollup toolchain. The very packages the old reason cited
#     as proof were the ones already demonstrated to work.
#   • better-sqlite3 and sharp must compile, or load machine code with no
#     portable path. They genuinely cannot.
#
# ``Capabilities.nativeToolchain`` on the client side is defined as the second
# ("compile and run NATIVE code: gcc, node-gyp, native node modules, anything
# that is not portable bytecode or WASM"). So the blanket true was not merely
# cautious — it asserted, of every project, a need most of them do not have.
# And because the WebContainers adapter honestly declares `nativeToolchain:
# false`, the two combined made that runtime unselectable BY CONSTRUCTION: no
# env var, no config, no operator could reach it. A registry that can only ever
# pick one runtime is not a registry.
#
# THE DIRECTION OF ERROR IS STILL ASYMMETRIC, and this table is built for that.
# Under-provisioning strands the user mid-build; over-provisioning only costs a
# slower sandbox. So membership here is generous — anything that plausibly
# compiles belongs — and the UNKNOWN paths (absent manifest, unparseable JSON,
# unreachable GitHub) are untouched by this change and still route to
# most-capable. What changed is only that a manifest we CAN read and that shows
# no evidence of compiling now gets to say so.
_COMPILES = "compiles native code at install time"
_MACHINE_CODE = "loads a prebuilt machine-code binary with no WASM fallback"

_NATIVE_TOOLCHAIN_PACKAGES: dict[str, str] = {
    # node-gyp addons — the classic case.
    "better-sqlite3": _COMPILES,
    "sqlite3": _COMPILES,
    "bcrypt": _COMPILES,
    "argon2": _COMPILES,
    "canvas": _COMPILES,
    "node-sass": _COMPILES,
    "zeromq": _COMPILES,
    "serialport": _COMPILES,
    "usb": _COMPILES,
    "node-hid": _COMPILES,
    "leveldown": _COMPILES,
    "robotjs": _COMPILES,
    "node-pty": _COMPILES,
    "grpc": _COMPILES,
    "libxmljs": _COMPILES,
    # The build tooling itself. Its presence in a manifest is a direct statement
    # that something here gets compiled.
    "node-gyp": _COMPILES,
    "node-pre-gyp": _COMPILES,
    "@mapbox/node-pre-gyp": _COMPILES,
    "cmake-js": _COMPILES,
    "prebuild": _COMPILES,
    "prebuild-install": _COMPILES,
    "nan": _COMPILES,
    "node-addon-api": _COMPILES,
    "ffi-napi": _COMPILES,
    "re2": _COMPILES,
    "keytar": _COMPILES,
    # Prebuilt machine code with no portable fallback. sharp wraps libvips;
    # puppeteer and playwright each download a real browser binary; electron and
    # sass-embedded each ship a platform binary with no WASM path (sass-embedded
    # is the Dart binary — the plain `sass` package is the JS/WASM one and is NOT
    # here).
    "sharp": _MACHINE_CODE,
    "puppeteer": _MACHINE_CODE,
    "playwright": _MACHINE_CODE,
    "sass-embedded": _MACHINE_CODE,
    "electron": _MACHINE_CODE,
    "electron-builder": _MACHINE_CODE,
    "@sentry/profiling-node": _MACHINE_CODE,
    "@tensorflow/tfjs-node": _MACHINE_CODE,
}

# Scoped native-binding families, matched by prefix for the same reason the raw
# socket table has one. `@napi-rs/*` and `@node-rs/*` are Rust addons compiled to
# `.node` files. `@playwright/*` is the canonical way Playwright enters a project
# — `npm init playwright` installs `@playwright/test`, NOT the bare `playwright`
# above — and every one of them drives a real downloaded browser binary, so the
# prefix is what actually closes the common case; the bare name alone would strand
# nearly every Playwright user on an in-tab runtime.
_NATIVE_TOOLCHAIN_PREFIXES: dict[str, str] = {
    "@napi-rs/": _MACHINE_CODE,
    "@node-rs/": _MACHINE_CODE,
    "@playwright/": _MACHINE_CODE,
}

# Compiler invocations that, appearing in a `scripts` value, mean the manifest
# itself says it shells out to a compiler — the strongest evidence available
# short of resolving the tree, because the project is describing its own build in
# its own words.
#
# Matched at a COMMAND BOUNDARY, not as a raw substring, and this is load-bearing
# in both directions. Substring matching (the first cut) fired `make` inside
# `cmake` and `prebuild` inside the npm `prebuild` lifecycle-script name — both
# over-provision, which is safe for routing but re-hides the in-tab runtime for a
# project that merely names a script `prebuild`, defeating the point of the whole
# change. It is dropped entirely here: the `prebuild` / `prebuild-install`
# PACKAGES are already in the toolchain table, so the marker only ever added
# false positives. Each alternative below is bounded by a shell separator (start,
# whitespace, `;`, `&`, `|`, or a paren) on both sides so `cmake` is its own
# token and never the tail of another word. `cmake` is listed as its own marker
# so it reports as cmake rather than mislabelling as make.
_NATIVE_BUILD_RE = re.compile(
    r"(?:^|[\s;&|()])"
    r"(node-gyp|node-pre-gyp|cmake-js|cmake|gcc|clang|g\+\+|cargo\s+build|make)"
    r"(?:$|[\s;&|()])",
    re.IGNORECASE,
)

# Kept in sync with archive.py's ref rules: a ref is attacker-influenced text, so
# it is matched against an allowlist before it is used at all.
_REF_RE = re.compile(r"[A-Za-z0-9._/-]{1,255}")


def _validate_ref(ref: str) -> str:
    """Refuse anything that is not a plain git ref.

    The ref reaches GitHub as a query param (httpx encodes it), so path traversal
    is not the threat it is in ``archive.py`` — but the same allowlist applies,
    because "it happens to be safe in this position" is not a property worth
    depending on the next time this string is moved.
    """
    if not _REF_RE.fullmatch(ref) or ".." in ref:
        raise ValidationError("websandbox.invalid_ref", "Invalid git ref")
    return ref


def _most_capable(cause: str) -> RuntimeRequirementsResponse:
    """The unknown-project answer: assume the project needs everything.

    Used whenever inspection could not produce a real verdict. See the module
    header — under-provisioning a runtime breaks the session, over-provisioning
    only slows it.

    ``cause`` is repeated once PER FLAG rather than stated once for the group,
    because the contract is that every raised flag carries its own evidence. A
    reader (or a client rendering "why did this open in a VM?") should be able to
    take any single reason line and understand that flag in isolation, without
    having to infer that an unattributed sentence above it applies to all three.
    """
    return RuntimeRequirementsResponse(
        install=True,
        nativeToolchain=True,
        rawSockets=True,
        reasons=[
            f"{cause} -> install",
            f"{cause} -> nativeToolchain",
            f"{cause} -> rawSockets",
        ],
    )


# Distinct from None, which is a real cached value ("this repo has no manifest").
_CACHE_MISS = object()


def _cache_get(key: tuple[str, str, str]) -> Any:
    """Return the cached manifest (possibly None), or ``_CACHE_MISS``."""
    entry = _manifest_cache.get(key)
    if entry is None:
        return _CACHE_MISS
    stored_at, value = entry
    if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
        _manifest_cache.pop(key, None)
        return _CACHE_MISS
    _manifest_cache.move_to_end(key)
    return value


def _cache_put(key: tuple[str, str, str], value: str | None) -> None:
    """Store an answer GitHub actually gave us, evicting least-recently-used."""
    _manifest_cache[key] = (time.monotonic(), value)
    _manifest_cache.move_to_end(key)
    while len(_manifest_cache) > _CACHE_MAX_ENTRIES:
        _manifest_cache.popitem(last=False)


def _reset_cache_for_tests() -> None:
    """Drop cached manifests. Tests that stub the network call this."""
    _manifest_cache.clear()


async def _fetch_package_json(owner: str, name: str, ref: str | None) -> str | None:
    """Return the repo's root ``package.json`` as text, or ``None``.

    ``None`` means "we could not read one" for ANY reason — absent, private,
    GitHub down, oversized, non-200. The caller does not get to distinguish,
    because every one of those cases resolves to the same defaulted verdict and
    pretending otherwise would invite a caller to branch on a distinction that
    carries no routing meaning.
    """
    # Validate BEFORE the cache lookup: a refused ref must never become a cache
    # key, or a rejected input would be silently served from cache next time.
    validated_ref = _validate_ref(ref) if ref else ""
    cache_key = (owner, name, validated_ref)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached

    # Built from validated parts — never from caller input directly.
    url = f"https://api.github.com/repos/{owner}/{name}/contents/package.json"
    params = {"ref": validated_ref} if validated_ref else None
    headers = {
        # Ask for the file body itself rather than the base64-in-JSON envelope.
        "Accept": "application/vnd.github.raw",
        "User-Agent": "pocketpaw-codemode",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_FETCH_TIMEOUT) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        logger.info(
            "websandbox: package.json probe failed for %s/%s: %s",
            owner,
            name,
            exc,
        )
        # Deliberately NOT cached. A transient outage must not pin a defaulted
        # verdict for the whole TTL; only answers GitHub actually gave us are.
        return None

    if response.status_code != 200:
        # Quota exhaustion is not just another non-200: it means EVERY probe from
        # here on defaults, so the feature stops working while still reporting
        # success. That deserves a WARNING with a distinct message, because it is
        # otherwise indistinguishable from "this repo has no package.json".
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            logger.warning(
                "websandbox: GitHub rate limit exhausted (unauthenticated, %s/hr per egress IP). "
                "Every runtime-requirements probe will default to most-capable until it resets "
                "at %s. Projects will still open, on a full VM.",
                response.headers.get("x-ratelimit-limit", "60"),
                response.headers.get("x-ratelimit-reset", "unknown"),
            )
            return None
        logger.info(
            "websandbox: package.json probe for %s/%s returned %s",
            owner,
            name,
            response.status_code,
        )
        # A definite "no manifest here" IS worth caching — it is the answer.
        if response.status_code == 404:
            _cache_put(cache_key, None)
        return None

    content = response.content
    if len(content) > _MAX_MANIFEST_BYTES:
        logger.info(
            "websandbox: package.json probe for %s/%s got %d bytes — ignoring",
            owner,
            name,
            len(content),
        )
        _cache_put(cache_key, None)
        return None
    manifest = content.decode("utf-8", errors="replace")
    _cache_put(cache_key, manifest)
    return manifest


def _declared_dependencies(manifest: dict[str, Any]) -> dict[str, str]:
    """Map each declared package name to the manifest field that declared it.

    The field name is carried because it goes into the human-readable reason
    ("pg in dependencies -> rawSockets"); knowing WHERE the evidence came from is
    most of what makes the reason worth reading.

    devDependencies count: a build step that runs the test suite or a migration
    script hits the same driver the app would, so a raw-socket package there is
    just as disqualifying for an in-tab runtime as one in ``dependencies``.
    ``dependencies`` is checked last so it wins the attribution when a package is
    declared in both.

    ``optionalDependencies`` is deliberately NOT read, and this is load-bearing
    for ``nativeToolchain``. Rollup, esbuild and swc all publish their
    per-platform prebuilt binaries there — ``@rollup/rollup-linux-x64-gnu``,
    ``@esbuild/darwin-arm64`` — and npm installs only the one matching the host,
    skipping the rest. Every one of them has a JS or WASM fallback, which is
    precisely why the 2026-07-18 gate run's Vite app built in an in-tab runtime.
    Scanning that section for native-looking names would mark every modern
    frontend project as needing a compiler and quietly restore the blanket
    behaviour this module was fixed to remove.
    """
    declared: dict[str, str] = {}
    for field in ("devDependencies", "dependencies"):
        section = manifest.get(field)
        if not isinstance(section, dict):
            continue
        for package in section:
            if isinstance(package, str):
                declared[package] = field
    return declared


def _match(
    package: str,
    exact: dict[str, str],
    prefixes: dict[str, str],
) -> str | None:
    """Look a package up by exact name, then by scoped prefix.

    Shared by the raw-socket and native-toolchain tables because they had
    identical lookup logic and one of them would inevitably drift.
    """
    cause = exact.get(package)
    if cause is not None:
        return cause
    return next((c for prefix, c in prefixes.items() if package.startswith(prefix)), None)


def _compiling_scripts(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    """Return ``(script name, matched token)`` for every script that shells out to a compiler.

    The matched token is carried through to the reason ("the build script runs
    cmake …") so the evidence names what actually triggered it rather than a
    canonical marker — the difference between "runs cmake" and the old
    "runs make" for a ``cmake .`` command.

    Only the `scripts` block is read. A marker in a dependency's OWN scripts is
    invisible here — we have not resolved the tree — which is the same limit the
    package tables work around by naming known offenders.
    """
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return set()

    hits: set[tuple[str, str]] = set()
    for name, command in scripts.items():
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        match = _NATIVE_BUILD_RE.search(command)
        if match is not None:
            # group(1) is the actual token, whitespace-normalised so
            # "cargo   build" reads as "cargo build" in the reason.
            token = " ".join(match.group(1).lower().split())
            hits.add((name, token))
    return hits


def infer_from_package_json(raw: str) -> RuntimeRequirementsResponse:
    """Derive requirements from a ``package.json`` body.

    Pure and separately testable — the network half lives above it. Unparseable
    JSON is treated as "cannot tell", which is the most-capable default rather
    than an error: a malformed manifest is exactly the case where guessing small
    would strand the user.
    """
    try:
        manifest = json.loads(raw)
    except (ValueError, TypeError):
        return _most_capable("package.json present but unparseable, so nothing can be ruled out")
    if not isinstance(manifest, dict):
        return _most_capable("package.json is not a JSON object, so nothing can be ruled out")

    reasons = ["package.json present -> install"]

    dependencies = _declared_dependencies(manifest)
    raw_socket_hits: list[tuple[str, str]] = []
    native_hits: list[tuple[str, str]] = []
    for package, section in sorted(dependencies.items()):
        cause = _match(package, _RAW_SOCKET_PACKAGES, _RAW_SOCKET_PREFIXES)
        if cause is not None:
            raw_socket_hits.append((package, section))
            reasons.append(f"{package} in {section} {cause} -> rawSockets")

        native_cause = _match(package, _NATIVE_TOOLCHAIN_PACKAGES, _NATIVE_TOOLCHAIN_PREFIXES)
        if native_cause is not None:
            native_hits.append((package, section))
            reasons.append(f"{package} in {section} {native_cause} -> nativeToolchain")

    # npm's own marker that the package carries a `binding.gyp`, i.e. that
    # installing it runs node-gyp. Rare in an application manifest and decisive
    # when present.
    if manifest.get("gypfile") is True:
        native_hits.append(("gypfile", "the manifest root"))
        reasons.append(
            'the manifest sets "gypfile": true, so installing it runs node-gyp -> nativeToolchain'
        )

    for script, token in sorted(_compiling_scripts(manifest)):
        native_hits.append((script, "scripts"))
        reasons.append(
            f'the "{script}" script runs {token}, which needs a compiler -> nativeToolchain'
        )

    return RuntimeRequirementsResponse(
        install=True,
        nativeToolchain=bool(native_hits),
        rawSockets=bool(raw_socket_hits),
        reasons=reasons,
    )


async def resolve_requirements(
    workspace_id: str,
    user_id: str,
    repo: str,
    ref: str | None = None,
) -> RuntimeRequirementsResponse:
    """Answer "what does this project need from a runtime?" without booting one.

    The repo reference is parsed by ``archive._parse_github_repo`` — the same
    SSRF boundary the archive endpoint uses, deliberately shared rather than
    re-implemented — and an unparseable reference is REFUSED loudly rather than
    defaulted, because it is a caller bug the archive path would reject too.
    Every other failure (absent manifest, unreachable GitHub, private repo)
    resolves to the most-capable default so a failed probe never blocks opening a
    project.
    """
    owner, name = _parse_github_repo(repo)

    raw = await _fetch_package_json(owner, name, ref)
    if raw is None:
        result = _most_capable(
            "no readable package.json (absent, private or unreachable), so the "
            "project is not known to be Node-shaped"
        )
    else:
        result = infer_from_package_json(raw)

    logger.info(
        "websandbox: resolved runtime requirements for %s/%s "
        "(install=%s nativeToolchain=%s rawSockets=%s) workspace=%s user=%s",
        owner,
        name,
        result.install,
        result.nativeToolchain,
        result.rawSockets,
        workspace_id,
        user_id,
    )
    return result


__all__ = ["infer_from_package_json", "resolve_requirements"]
