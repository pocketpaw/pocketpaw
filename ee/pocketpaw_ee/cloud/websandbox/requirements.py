# requirements.py — resolve what a PROJECT NEEDS from a runtime, before any
# runtime boots (RR-2).
# Created 2026-07-20 (feat/code-runtime-requirements).
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
# is the product here. An earlier version emitted "better-sqlite3 in dependencies
# -> rawSockets", which is simply false — better-sqlite3 is an embedded file
# database that opens no sockets. It still needs a real machine (it is a native
# FFI binding an in-tab runtime cannot load), so the VERDICT was right and the
# EXPLANATION was wrong. That is the worst combination: it survives review and
# then sends the next debugger hunting a network problem that does not exist.
#
# ORMs matter more than drivers. Real Node apps reach Postgres through Prisma or
# Drizzle, and `pg` is then a TRANSITIVE dependency that never appears in
# package.json at all. Matching only bare driver names returned a confident
# `rawSockets: false` for a Prisma+Postgres app — under-provisioning, which this
# module's header says must never happen: it strands the user at first query,
# whereas over-provisioning only costs a slower sandbox.
_SOCKET = "opens a network socket to a database or broker"
_NATIVE_FFI = "is a native FFI binding an in-tab runtime cannot load"
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
    # Native FFI, not sockets. Same verdict, honest reason.
    "sqlite3": _NATIVE_FFI,
    "better-sqlite3": _NATIVE_FFI,
}

# Scoped packages can never match a bare name, so they are matched by prefix.
# `@prisma/client` is the one almost every Prisma project actually declares.
_RAW_SOCKET_PREFIXES: dict[str, str] = {
    "@prisma/": _ORM,
    "@databases/": _ORM,
}

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

    reasons = [
        "package.json present -> install",
        # Worded as the ASSUMPTION it is. We have not resolved the dependency
        # tree — only read the manifest — so we cannot claim a native build step
        # actually occurs; an empty manifest has none. But any non-trivial tree
        # pulls prebuilt native binaries (esbuild, rollup's native bindings,
        # sharp, node-gyp fallbacks), and over-provisioning only costs speed
        # while under-provisioning strands the user.
        "package.json present, dependency tree not resolved; assuming a native "
        "build step -> nativeToolchain",
    ]

    dependencies = _declared_dependencies(manifest)
    raw_socket_hits: list[tuple[str, str]] = []
    for package, section in sorted(dependencies.items()):
        cause = _RAW_SOCKET_PACKAGES.get(package)
        if cause is None:
            cause = next(
                (c for prefix, c in _RAW_SOCKET_PREFIXES.items() if package.startswith(prefix)),
                None,
            )
        if cause is not None:
            raw_socket_hits.append((package, section))
            reasons.append(f"{package} in {section} {cause} -> rawSockets")

    return RuntimeRequirementsResponse(
        install=True,
        nativeToolchain=True,
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
