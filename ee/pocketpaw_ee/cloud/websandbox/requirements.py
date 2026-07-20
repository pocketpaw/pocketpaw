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
from typing import Any

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.websandbox.archive import _parse_github_repo
from pocketpaw_ee.cloud.websandbox.dto import RuntimeRequirementsResponse

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 15.0

# A package.json is a manifest, not a payload. Anything past this is either not a
# manifest or not something we should be parsing on the request path.
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024

# Node packages that open a RAW TCP SOCKET (or load a native driver that does) to
# reach a database or broker. This is the capability line an in-tab runtime cannot
# cross: WebContainers and its peers emulate networking over browser-reachable
# transports (fetch / WebSocket relays) and expose no real ``net.Socket``, so a
# project that talks to Postgres or Redis directly can only run in a real VM.
#
# The two sqlite entries are here for a sibling reason rather than sockets: they
# are native FFI bindings, which an in-tab runtime equally cannot load. They land
# on the same "needs a real machine" verdict, so they raise the same flag.
_RAW_SOCKET_PACKAGES = frozenset(
    {
        "pg",
        "mysql",
        "mysql2",
        "mongodb",
        "mongoose",
        "redis",
        "ioredis",
        "sqlite3",
        "better-sqlite3",
        "cassandra-driver",
        "oracledb",
        "tedious",
        "amqplib",
    }
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


async def _fetch_package_json(owner: str, name: str, ref: str | None) -> str | None:
    """Return the repo's root ``package.json`` as text, or ``None``.

    ``None`` means "we could not read one" for ANY reason — absent, private,
    GitHub down, oversized, non-200. The caller does not get to distinguish,
    because every one of those cases resolves to the same defaulted verdict and
    pretending otherwise would invite a caller to branch on a distinction that
    carries no routing meaning.
    """
    # Built from validated parts — never from caller input directly.
    url = f"https://api.github.com/repos/{owner}/{name}/contents/package.json"
    params = {"ref": _validate_ref(ref)} if ref else None
    headers = {
        # Ask for the file body itself rather than the base64-in-JSON envelope.
        "Accept": "application/vnd.github.raw",
        "User-Agent": "pocketpaw-codemode",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_FETCH_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        logger.info(
            "websandbox: package.json probe failed for %s/%s: %s",
            owner,
            name,
            exc,
        )
        return None

    if response.status_code != 200:
        logger.info(
            "websandbox: package.json probe for %s/%s returned %s",
            owner,
            name,
            response.status_code,
        )
        return None

    content = response.content
    if len(content) > _MAX_MANIFEST_BYTES:
        logger.info(
            "websandbox: package.json probe for %s/%s got %d bytes — ignoring",
            owner,
            name,
            len(content),
        )
        return None
    return content.decode("utf-8", errors="replace")


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
        # Not a hypothetical: an npm install of any non-trivial dependency tree
        # downloads or compiles prebuilt native binaries (esbuild, rollup's
        # native bindings, sharp, node-gyp fallbacks). Treating "Node project"
        # as "needs a native toolchain" is the empirically correct default.
        "package.json present -> npm install fetches/builds native binaries -> nativeToolchain",
    ]

    dependencies = _declared_dependencies(manifest)
    raw_socket_hits = sorted(set(dependencies) & _RAW_SOCKET_PACKAGES)
    for package in raw_socket_hits:
        reasons.append(f"{package} in {dependencies[package]} -> rawSockets")

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
