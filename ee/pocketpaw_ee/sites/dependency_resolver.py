# dependency_resolver.py — turn an author's "I want three" into a vetted, pinned
# ``paw.dependencies.json`` entry, or into a reason the agent can act on.
#
# Created: 2026-09-24 (feat/sites-author-dependencies, PP-1). Authors may now
# declare arbitrary npm packages on the svelte, react and html tracks, guarded by
# policy instead of a four-name allowlist. This module is that policy's UX layer:
# it reads registry METADATA over HTTP and never installs anything. The build-time
# gate is elsewhere (the paw-sites generator re-validates the manifest, and author
# packages install only inside the Daytona sandbox under the bunfig floor).
#
# Every check that needs the network FAILS CLOSED. A registry that is down, slow or
# answering garbage produces a ``registry_unavailable`` rejection, never a silent
# accept, because "we could not look" and "we looked and it was fine" must not
# collapse into the same answer.
"""Resolve author-declared npm packages against the registry and the site policy.

Entry point: :func:`resolve_dependencies`. A request is ``{"name": ..., "range": ...}``
(range optional, default "latest"). For each one the resolver:

1. validates the name and refuses non-registry specs (git, url, file, tarball, alias);
2. refuses toolchain-owned names (contract §2);
3. picks the HIGHEST version that satisfies the range AND was published at least
   :data:`bun_supply_chain.MINIMUM_RELEASE_AGE_SECONDS` ago, skipping deprecated
   versions;
4. refuses that version if it has install scripts, native build files or an
   unpacked size over :data:`MAX_UNPACKED_BYTES`;
5. refuses a package with fewer than :data:`MIN_WEEKLY_DOWNLOADS` weekly downloads
   (the typosquat guard);
6. refuses a version with a moderate-or-worse advisory from npm's bulk endpoint;
7. for html, computes the jsdelivr ``+esm`` URL and its sha384 SRI hash.

The HTTP client and the clock are injectable so tests run against a recorded fake
registry with a fixed "now".
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from pocketpaw_ee.sites.bun_supply_chain import MINIMUM_RELEASE_AGE_SECONDS
from pocketpaw_ee.sites.dependency_manifest import (
    DEPENDENCY_ENGINES,
    MAX_DECLARED_PACKAGES,
    is_toolchain_reserved,
    jsdelivr_esm_url,
    npm_name_problem,
)

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.npmjs.org"
DOWNLOADS_API_URL = "https://api.npmjs.org"
ADVISORIES_BULK_PATH = "/-/npm/v1/security/advisories/bulk"

#: Largest unpacked size a declared package may have (25 MB).
MAX_UNPACKED_BYTES = 25 * 1024 * 1024
#: Fewest weekly downloads a declared package may have. The typosquat guard.
MIN_WEEKLY_DOWNLOADS = 500
#: Advisory severities that block a version.
BLOCKING_SEVERITIES = frozenset({"moderate", "high", "critical"})
#: Lifecycle scripts npm runs on install.
INSTALL_SCRIPT_KEYS = ("preinstall", "install", "postinstall")

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Rejection codes. Stable strings the agent (and tests) can key on.
INVALID_NAME = "invalid_name"
NON_REGISTRY_SPEC = "non_registry_spec"
INVALID_RANGE = "invalid_range"
TOOLCHAIN_RESERVED = "toolchain_reserved"
ENGINE_UNSUPPORTED = "engine_unsupported"
NOT_FOUND = "not_found"
NO_ELIGIBLE_VERSION = "no_eligible_version"
DEPRECATED = "deprecated"
INSTALL_SCRIPTS = "install_scripts"
NATIVE_BUILD = "native_build"
TOO_LARGE = "too_large"
LOW_DOWNLOADS = "low_downloads"
ADVISORY = "advisory"
TOO_MANY = "too_many"
REGISTRY_UNAVAILABLE = "registry_unavailable"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedPackage:
    name: str
    version: str
    esm: str | None = None
    integrity: str | None = None

    def manifest_entry(self) -> dict[str, str]:
        entry = {"version": self.version}
        if self.esm is not None:
            entry["esm"] = self.esm
        if self.integrity is not None:
            entry["integrity"] = self.integrity
        return entry


@dataclass(frozen=True)
class Rejection:
    name: str
    code: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "code": self.code, "reason": self.reason}


@dataclass
class ResolveResult:
    packages: dict[str, ResolvedPackage] = field(default_factory=dict)
    rejected: list[Rejection] = field(default_factory=list)


@dataclass(frozen=True)
class DependencyRequest:
    name: str
    range: str = "latest"


# ---------------------------------------------------------------------------
# A small npm-semver implementation (ranges the registry's users actually write)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_PARTIAL_RE = re.compile(
    r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_COMPARATOR_RE = re.compile(r"^(<=|>=|<|>|=|\^|~>?)?(.*)$")


@dataclass(frozen=True, order=False)
class SemVer:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[Any, ...]:
        pre_key: tuple[Any, ...]
        if not self.pre:
            pre_key = (1,)
        else:
            pre_key = (0, *((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.pre))
        return (self.major, self.minor, self.patch, pre_key)

    def __lt__(self, other: SemVer) -> bool:
        return self.key < other.key

    def __le__(self, other: SemVer) -> bool:
        return self.key <= other.key

    def __gt__(self, other: SemVer) -> bool:
        return self.key > other.key

    def __ge__(self, other: SemVer) -> bool:
        return self.key >= other.key


def parse_version(text: str) -> SemVer | None:
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    return SemVer(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


def _is_wild(part: str | None) -> bool:
    return part is None or part in ("x", "X", "*")


_Comparator = tuple[str, SemVer]  # (op, version); op in <, <=, >, >=, =


def _desugar(op: str, text: str) -> list[_Comparator]:
    """Expand one comparator (caret, tilde, x-range, partial) into primitives."""
    if text in ("", "*", "x", "X"):
        return []
    m = _PARTIAL_RE.match(text)
    if not m:
        raise ValueError(f"`{text}` is not a version")
    ma, mi, pa, pre_s = m.group(1), m.group(2), m.group(3), m.group(4)
    pre = tuple(pre_s.split(".")) if pre_s else ()
    if _is_wild(ma):
        return (
            [] if op in ("", "=", ">=", "<=", "^", "~", "~>") else [("<", SemVer(0, 0, 0, ("0",)))]
        )
    M = int(ma)
    if _is_wild(mi):
        lo, hi = SemVer(M, 0, 0), SemVer(M + 1, 0, 0, ("0",))
        partial = "major"
    elif _is_wild(pa):
        m_ = int(mi)
        lo, hi = SemVer(M, m_, 0), SemVer(M, m_ + 1, 0, ("0",))
        partial = "minor"
    else:
        lo = SemVer(M, int(mi), int(pa), pre)
        hi = None
        partial = ""
    if op == "^":
        if partial == "major":
            return [(">=", lo), ("<", hi)]  # type: ignore[list-item]
        if partial == "minor":
            if M > 0:
                return [(">=", lo), ("<", SemVer(M + 1, 0, 0, ("0",)))]
            return [(">=", lo), ("<", hi)]  # type: ignore[list-item]
        if lo.major > 0:
            upper = SemVer(lo.major + 1, 0, 0, ("0",))
        elif lo.minor > 0:
            upper = SemVer(0, lo.minor + 1, 0, ("0",))
        else:
            upper = SemVer(0, 0, lo.patch + 1, ("0",))
        return [(">=", lo), ("<", upper)]
    if op in ("~", "~>"):
        if partial:
            return [(">=", lo), ("<", hi)]  # type: ignore[list-item]
        return [(">=", lo), ("<", SemVer(lo.major, lo.minor + 1, 0, ("0",)))]
    if op in ("", "="):
        if partial:
            return [(">=", lo), ("<", hi)]  # type: ignore[list-item]
        return [("=", lo)]
    if op == ">":
        return [(">=", hi)] if partial else [(">", lo)]  # type: ignore[list-item]
    if op == ">=":
        return [(">=", lo)]
    if op == "<":
        return [("<", SemVer(lo.major, lo.minor, lo.patch, ("0",)))] if partial else [("<", lo)]
    if op == "<=":
        return [("<", hi)] if partial else [("<=", lo)]  # type: ignore[list-item]
    raise ValueError(f"unknown operator `{op}`")


@dataclass(frozen=True)
class SemverRange:
    sets: tuple[tuple[_Comparator, ...], ...]

    def satisfied_by(self, version: SemVer) -> bool:
        for comparators in self.sets:
            if version.pre and not any(
                c[1].pre
                and (c[1].major, c[1].minor, c[1].patch)
                == (version.major, version.minor, version.patch)
                for c in comparators
            ):
                continue  # npm: a prerelease only matches a range that names its tuple
            if all(_compare(version, op, bound) for op, bound in comparators):
                return True
        return False


def _compare(v: SemVer, op: str, bound: SemVer) -> bool:
    if op == "<":
        return v < bound
    if op == "<=":
        return v <= bound
    if op == ">":
        return v > bound
    if op == ">=":
        return v >= bound
    return v.key == bound.key


def parse_range(text: str) -> SemverRange:
    """Parse an npm range. Raises ``ValueError`` on anything it cannot read."""
    raw = (text or "").strip()
    if raw in ("", "latest", "*"):
        return SemverRange(((),))
    sets: list[tuple[_Comparator, ...]] = []
    for alt in raw.split("||"):
        alt = alt.strip()
        hyphen = re.match(r"^(\S+)\s+-\s+(\S+)$", alt)
        comparators: list[_Comparator] = []
        if hyphen:
            comparators += _desugar(">=", hyphen.group(1))
            comparators += _desugar("<=", hyphen.group(2))
        else:
            alt = re.sub(r"(<=|>=|<|>|=|\^|~>?)\s+", r"\1", alt)
            for token in alt.split():
                m = _COMPARATOR_RE.match(token)
                assert m is not None
                comparators += _desugar(m.group(1) or "", m.group(2))
        sets.append(tuple(comparators))
    return SemverRange(tuple(sets))


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

_NON_REGISTRY_RE = re.compile(
    r"(://|^git[+:@]|^(?:file|link|npm|workspace|github|gitlab|bitbucket|gist|portal|patch):"
    r"|\.t(?:ar\.)?gz$|^[\w.-]+/[\w.-]+(?:#.*)?$)",
    re.IGNORECASE,
)


def _split_spec(spec: str) -> tuple[str, str]:
    """Split ``name@range`` (scoped names keep their leading ``@``)."""
    at = spec.find("@", 1)
    if at == -1:
        return spec, "latest"
    return spec[:at], spec[at + 1 :] or "latest"


def coerce_requests(raw: object) -> tuple[list[DependencyRequest], list[Rejection]]:
    """Accept ``[{name, range?}]`` (the contract) and tolerate ``["name@range"]``."""
    requests: list[DependencyRequest] = []
    rejected: list[Rejection] = []
    if raw is None:
        return requests, rejected
    if not isinstance(raw, list):
        return requests, [
            Rejection("", INVALID_NAME, "`dependencies` must be a list of {name, range} objects")
        ]
    seen: dict[str, int] = {}
    for item in raw:
        if isinstance(item, str):
            name, rng = _split_spec(item.strip())
        elif isinstance(item, Mapping):
            name = item.get("name")
            rng = item.get("range") or item.get("version") or "latest"
        else:
            rejected.append(Rejection(str(item), INVALID_NAME, "each dependency needs a `name`"))
            continue
        if not isinstance(name, str) or not isinstance(rng, str):
            rejected.append(
                Rejection(str(name), INVALID_NAME, "`name` and `range` must be strings")
            )
            continue
        req = DependencyRequest(name=name.strip(), range=rng.strip() or "latest")
        if req.name in seen:
            requests[seen[req.name]] = req  # a later duplicate wins
        else:
            seen[req.name] = len(requests)
            requests.append(req)
    return requests, rejected


def _static_rejection(req: DependencyRequest) -> Rejection | None:
    """The checks that need no network: spec shape, name, range, toolchain."""
    if _NON_REGISTRY_RE.search(req.name) or _NON_REGISTRY_RE.search(req.range):
        return Rejection(
            req.name,
            NON_REGISTRY_SPEC,
            "only packages from the public npm registry can be declared — git, url, "
            "file, tarball, alias (`npm:`) and GitHub shorthand specs are refused. Give "
            "the package name and an optional semver range.",
        )
    if (problem := npm_name_problem(req.name)) is not None:
        return Rejection(req.name, INVALID_NAME, f"`{req.name}` is not a valid npm name: {problem}")
    if is_toolchain_reserved(req.name):
        return Rejection(
            req.name,
            TOOLCHAIN_RESERVED,
            f"`{req.name}` is part of the site's build toolchain, so the generator "
            "provides it at a vetted version. Import it directly; do not declare it.",
        )
    try:
        parse_range(req.range)
    except ValueError:
        return Rejection(
            req.name,
            INVALID_RANGE,
            f"`{req.range}` is not a semver range npm understands. Use an exact "
            "version (`1.2.3`), a range (`^1.2.0`, `~1.2`, `>=1 <2`) or omit it for the "
            "newest eligible version. Dist-tags other than `latest` are not accepted.",
        )
    return None


# ---------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------


class _Unavailable(Exception):
    """A network or registry failure. Always becomes a rejection, never an accept."""


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[int, Any]:
    try:
        resp = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise _Unavailable(f"{type(exc).__name__}") from exc
    if resp.status_code == 404:
        return 404, None
    if resp.status_code != 200:
        raise _Unavailable(f"HTTP {resp.status_code}")
    try:
        return 200, resp.json()
    except ValueError as exc:
        raise _Unavailable("unreadable JSON") from exc


def _unavailable(name: str, what: str, detail: str) -> Rejection:
    return Rejection(
        name,
        REGISTRY_UNAVAILABLE,
        f"could not reach {what} to vet `{name}` ({detail}). Nothing was declared; "
        "try again in a moment.",
    )


async def _vet_one(
    client: httpx.AsyncClient,
    req: DependencyRequest,
    *,
    now: datetime,
    registry: str,
    downloads_api: str,
) -> ResolvedPackage | Rejection:
    """Steps 3-5 for one request: pick the version, then vet it."""
    rng = parse_range(req.range)
    try:
        status, packument = await _get_json(client, f"{registry}/{quote(req.name, safe='@')}")
    except _Unavailable as exc:
        return _unavailable(req.name, "the npm registry", str(exc))
    if status == 404 or not isinstance(packument, dict):
        return Rejection(
            req.name,
            NOT_FOUND,
            f"`{req.name}` does not exist on the npm registry. Check the spelling.",
        )
    versions = packument.get("versions") or {}
    times = packument.get("time") or {}
    if not isinstance(versions, dict) or not isinstance(times, dict):
        return _unavailable(req.name, "the npm registry", "malformed metadata")

    cutoff = now - timedelta(seconds=MINIMUM_RELEASE_AGE_SECONDS)
    parsed: list[tuple[SemVer, str, datetime]] = []
    for text, manifest in versions.items():
        ver = parse_version(text)
        published = _parse_time(times.get(text))
        if ver is None or published is None or not isinstance(manifest, dict):
            continue
        parsed.append((ver, text, published))
    parsed.sort(key=lambda item: item[0].key, reverse=True)

    eligible_any = [p for p in parsed if p[2] <= cutoff and not p[0].pre]
    newest_eligible = eligible_any[0][1] if eligible_any else None
    satisfying = [p for p in parsed if rng.satisfied_by(p[0])]
    if not satisfying:
        hint = (
            f" The newest eligible version is {newest_eligible}."
            if newest_eligible
            else " No version of it has been published long enough ago to use."
        )
        return Rejection(
            req.name,
            NO_ELIGIBLE_VERSION,
            f"no published version of `{req.name}` matches `{req.range}`.{hint}",
        )
    aged = [p for p in satisfying if p[2] <= cutoff]
    if not aged:
        hint = (
            f" The newest eligible version overall is {newest_eligible}." if newest_eligible else ""
        )
        return Rejection(
            req.name,
            NO_ELIGIBLE_VERSION,
            f"every version of `{req.name}` matching `{req.range}` was published less "
            f"than 7 days ago, and the build refuses packages that new (the "
            f"supply-chain release-age floor).{hint}",
        )
    chosen = next((p for p in aged if not versions[p[1]].get("deprecated")), None)
    if chosen is None:
        note = versions[aged[0][1]].get("deprecated")
        return Rejection(
            req.name,
            DEPRECATED,
            f"`{req.name}` is deprecated on npm"
            + (f" (“{str(note)[:200]}”)" if isinstance(note, str) else "")
            + ". Pick a maintained alternative.",
        )
    version = chosen[1]
    manifest = versions[version]

    scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
    lifecycle = [k for k in INSTALL_SCRIPT_KEYS if k in scripts]
    if lifecycle:
        return Rejection(
            req.name,
            INSTALL_SCRIPTS,
            f"`{req.name}@{version}` runs install scripts ({', '.join(lifecycle)}). Site "
            "builds never run lifecycle scripts, so it would install broken. Pick a "
            "pure-JavaScript alternative.",
        )
    if manifest.get("gypfile") or manifest.get("binary"):
        return Rejection(
            req.name,
            NATIVE_BUILD,
            f"`{req.name}@{version}` builds a native addon, which cannot run in a "
            "browser or a static site build. Pick a pure-JavaScript alternative.",
        )
    dist = manifest.get("dist") if isinstance(manifest.get("dist"), dict) else {}
    size = dist.get("unpackedSize")
    if isinstance(size, int) and size > MAX_UNPACKED_BYTES:
        return Rejection(
            req.name,
            TOO_LARGE,
            f"`{req.name}@{version}` unpacks to {size / 1024 / 1024:.1f} MB, over the "
            f"{MAX_UNPACKED_BYTES // 1024 // 1024} MB cap for a site dependency.",
        )

    try:
        status, downloads = await _get_json(
            client, f"{downloads_api}/downloads/point/last-week/{req.name}"
        )
    except _Unavailable as exc:
        return _unavailable(req.name, "the npm downloads API", str(exc))
    count = downloads.get("downloads") if isinstance(downloads, dict) else 0
    count = count if isinstance(count, int) else 0
    if count < MIN_WEEKLY_DOWNLOADS:
        return Rejection(
            req.name,
            LOW_DOWNLOADS,
            f"`{req.name}` had {count} downloads last week, under the "
            f"{MIN_WEEKLY_DOWNLOADS} floor. Low-traffic names are where typosquats "
            "live — check the spelling, or pick a widely used package.",
        )
    return ResolvedPackage(name=req.name, version=version)


async def _advisory_rejections(
    client: httpx.AsyncClient, packages: Sequence[ResolvedPackage], *, registry: str
) -> dict[str, Rejection]:
    """Step 6, one bulk call for every survivor. A failure rejects them all."""
    if not packages:
        return {}
    body = {p.name: [p.version] for p in packages}
    try:
        resp = await client.post(f"{registry}{ADVISORIES_BULK_PATH}", json=body)
        if resp.status_code != 200:
            raise _Unavailable(f"HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise _Unavailable("malformed response")
    except (httpx.HTTPError, ValueError, _Unavailable) as exc:
        detail = str(exc) if isinstance(exc, _Unavailable) else type(exc).__name__
        return {p.name: _unavailable(p.name, "the npm advisory database", detail) for p in packages}
    out: dict[str, Rejection] = {}
    for pkg in packages:
        advisories = data.get(pkg.name) or []
        version = parse_version(pkg.version)
        for adv in advisories if isinstance(advisories, list) else []:
            if not isinstance(adv, dict):
                continue
            if str(adv.get("severity", "")).lower() not in BLOCKING_SEVERITIES:
                continue
            affected = True
            vulnerable = adv.get("vulnerable_versions")
            if isinstance(vulnerable, str) and version is not None:
                try:
                    affected = parse_range(vulnerable).satisfied_by(version)
                except ValueError:
                    affected = True  # unreadable range: assume it applies
            if not affected:
                continue
            title = str(adv.get("title") or "a known vulnerability")[:160]
            url = adv.get("url")
            out[pkg.name] = Rejection(
                pkg.name,
                ADVISORY,
                f"`{pkg.name}@{pkg.version}` has a {adv.get('severity')} security "
                f"advisory: {title}"
                + (f" ({url})" if isinstance(url, str) else "")
                + ". Pin a patched range, or pick another package.",
            )
            break
    return out


async def _with_integrity(
    client: httpx.AsyncClient, pkg: ResolvedPackage
) -> ResolvedPackage | Rejection:
    """Step 7 (html): hash the exact bytes jsdelivr serves for the ``+esm`` URL."""
    url = jsdelivr_esm_url(pkg.name, pkg.version)
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return _unavailable(pkg.name, "the jsdelivr CDN", type(exc).__name__)
    if resp.status_code != 200 or not resp.content:
        return _unavailable(pkg.name, "the jsdelivr CDN", f"HTTP {resp.status_code}")
    digest = base64.b64encode(hashlib.sha384(resp.content).digest()).decode("ascii")
    return ResolvedPackage(
        name=pkg.name, version=pkg.version, esm=url, integrity=f"sha384-{digest}"
    )


async def resolve_dependencies(
    requests: Iterable[DependencyRequest],
    engine: str,
    *,
    already_declared: Iterable[str] = (),
    client: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] | None = None,
    registry: str = REGISTRY_URL,
    downloads_api: str = DOWNLOADS_API_URL,
) -> ResolveResult:
    """Resolve ``requests`` for ``engine``. See the module docstring for the steps.

    ``already_declared`` is the set of names the site keeps regardless of this call;
    it counts toward :data:`MAX_DECLARED_PACKAGES`. A name that is both declared and
    requested is a re-resolve, not a second slot.
    """
    result = ResolveResult()
    reqs = list(requests)
    if engine not in DEPENDENCY_ENGINES:
        for req in reqs:
            result.rejected.append(
                Rejection(
                    req.name,
                    ENGINE_UNSUPPORTED,
                    f"a {engine or 'ripple'} site has no authored code to import a "
                    "package into. npm packages work on the svelte, react and html tracks.",
                )
            )
        return result

    pending: list[DependencyRequest] = []
    for req in reqs:
        if (rej := _static_rejection(req)) is not None:
            result.rejected.append(rej)
        else:
            pending.append(req)

    kept = set(already_declared) - {r.name for r in pending}
    room = MAX_DECLARED_PACKAGES - len(kept)
    if len(pending) > max(room, 0):
        for req in pending[max(room, 0) :]:
            result.rejected.append(
                Rejection(
                    req.name,
                    TOO_MANY,
                    f"a site may declare at most {MAX_DECLARED_PACKAGES} packages. Remove "
                    "one you no longer import before adding another.",
                )
            )
        pending = pending[: max(room, 0)]
    if not pending:
        return result

    clock = now or (lambda: datetime.now(UTC))
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    try:
        at = clock()
        vetted = await asyncio.gather(
            *(
                _vet_one(http, r, now=at, registry=registry, downloads_api=downloads_api)
                for r in pending
            )
        )
        survivors = [v for v in vetted if isinstance(v, ResolvedPackage)]
        result.rejected.extend(v for v in vetted if isinstance(v, Rejection))

        blocked = await _advisory_rejections(http, survivors, registry=registry)
        result.rejected.extend(blocked.values())
        survivors = [p for p in survivors if p.name not in blocked]

        if engine == "html":
            finished = await asyncio.gather(*(_with_integrity(http, p) for p in survivors))
        else:
            finished = survivors
        for item in finished:
            if isinstance(item, Rejection):
                result.rejected.append(item)
            else:
                result.packages[item.name] = item
    finally:
        if owns_client:
            await http.aclose()
    return result


__all__ = [
    "BLOCKING_SEVERITIES",
    "DependencyRequest",
    "MAX_UNPACKED_BYTES",
    "MIN_WEEKLY_DOWNLOADS",
    "Rejection",
    "ResolveResult",
    "ResolvedPackage",
    "SemVer",
    "coerce_requests",
    "parse_range",
    "parse_version",
    "resolve_dependencies",
]
