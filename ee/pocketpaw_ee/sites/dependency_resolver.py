# dependency_resolver.py — turn an author's "I want three" into an exact, pinned
# ``paw.dependencies.json`` entry, or into a reason the agent can act on.
#
# Policy (captain decision 2026-10-07, "open everything"): a site author may declare
# ANY public npm package at any version, range or dist-tag. There is no size cap, no
# downloads floor, no release-age floor, no install-script or native-addon refusal and
# no package-count cap. The isolation boundary is WHERE author packages install: only
# inside the Daytona sandbox, never on the API host (``generator_client`` refuses).
#
# What this module still refuses: specs that are not plain registry packages (git,
# url, file, tarball, ``npm:`` alias, GitHub shorthand), names npm would not accept,
# unreadable ranges, names the registry does not know, and a range/tag that matches
# no published version. Toolchain names (svelte, vite, react...) are accepted: the
# vendored paw-sites allowlist reserves none, so ``is_toolchain_reserved`` is a no-op
# unless a re-vendor brings reservations back.
#
# It FAILS OPEN on the metadata it only uses for advice: advisories become
# ``warnings``, and an advisory or jsDelivr outage is ignored. If the registry itself
# is unreachable, an exact version is accepted as given (with a warning); a range or
# tag needs the registry to resolve, so that one case is a retryable
# ``registry_unavailable`` rejection.
"""Resolve author-declared npm packages against the npm registry.

Entry point: :func:`resolve_dependencies`. A request is ``{"name": ..., "range": ...}``
(range optional, default "latest"). For each one the resolver:

1. validates the name and refuses non-registry specs (git, url, file, tarball, alias);
2. refuses names the vendored allowlist reserves for the toolchain (none today);
3. picks a version: ``latest`` or any other dist-tag resolves through the packument's
   ``dist-tags``; a semver range picks the highest matching version, preferring one
   that is not deprecated (prereleases match only when the range names them, as in
   npm);
4. attaches npm advisories for the chosen version as ``warnings`` (never a refusal);
5. for html, writes the jsDelivr ``+esm`` URL, plus its sha384 SRI hash when the CDN
   answers.

The HTTP client is injectable so tests run against a recorded fake registry.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from pocketpaw_ee.sites.dependency_manifest import (
    DEPENDENCY_ENGINES,
    is_toolchain_reserved,
    jsdelivr_esm_url,
    npm_name_problem,
)

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.npmjs.org"
ADVISORIES_BULK_PATH = "/-/npm/v1/security/advisories/bulk"

#: Advisory severities worth telling the agent about. Lower ones are noise.
WARNING_SEVERITIES = frozenset({"moderate", "high", "critical"})

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

#: Most outbound registry / CDN lookups one resolve keeps in flight. A request list
#: is author-supplied, so the fan-out is bounded rather than one task per package.
MAX_CONCURRENT_LOOKUPS = 12

# Rejection codes. Stable strings the agent (and tests) can key on.
INVALID_NAME = "invalid_name"
NON_REGISTRY_SPEC = "non_registry_spec"
INVALID_RANGE = "invalid_range"
TOOLCHAIN_RESERVED = "toolchain_reserved"
ENGINE_UNSUPPORTED = "engine_unsupported"
NOT_FOUND = "not_found"
NO_ELIGIBLE_VERSION = "no_eligible_version"
REGISTRY_UNAVAILABLE = "registry_unavailable"

# Warning codes (``ResolveResult.warnings``). Never block a package.
DEPRECATED = "deprecated"
ADVISORY = "advisory"
UNVERIFIED = "unverified"


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
    #: ``{name, code, message}`` notes about ACCEPTED packages (advisories,
    #: deprecation, an exact pin accepted while the registry was down).
    warnings: list[dict[str, str]] = field(default_factory=list)


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


# A dist-tag: what npm accepts as a tag name and cannot be read as a range.
_DIST_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


def _is_dist_tag(text: str) -> bool:
    """True for ``latest``, ``next``, ``beta``… (anything tag-shaped, not a range)."""
    if text == "latest":
        return True
    if not _DIST_TAG_RE.match(text):
        return False
    try:
        parse_range(text)
    except ValueError:
        return True
    return False


def _exact_version(text: str) -> str | None:
    """The canonical exact version ``text`` names, or ``None`` for a range/tag."""
    parsed = parse_version(text)
    if parsed is None:
        return None
    core = f"{parsed.major}.{parsed.minor}.{parsed.patch}"
    return f"{core}-{'.'.join(parsed.pre)}" if parsed.pre else core


def _static_rejection(req: DependencyRequest) -> Rejection | None:
    """The checks that need no network: spec shape, name, range, toolchain."""
    if _NON_REGISTRY_RE.search(req.name) or _NON_REGISTRY_RE.search(req.range):
        return Rejection(
            req.name,
            NON_REGISTRY_SPEC,
            "only packages from the public npm registry can be declared — git, url, "
            "file, tarball, alias (`npm:`) and GitHub shorthand specs are refused. Give "
            "the package name and an optional version, range or dist-tag.",
        )
    if (problem := npm_name_problem(req.name)) is not None:
        return Rejection(req.name, INVALID_NAME, f"`{req.name}` is not a valid npm name: {problem}")
    if is_toolchain_reserved(req.name):
        return Rejection(
            req.name,
            TOOLCHAIN_RESERVED,
            f"`{req.name}` is part of the site's build toolchain, so the generator "
            "provides it. Import it directly; do not declare it.",
        )
    if not _is_dist_tag(req.range):
        try:
            parse_range(req.range)
        except ValueError:
            return Rejection(
                req.name,
                INVALID_RANGE,
                f"`{req.range}` is not a version, range or dist-tag npm understands. Use "
                "an exact version (`1.2.3`), a range (`^1.2.0`, `~1.2`, `>=1 <2`), a "
                "dist-tag (`next`, `beta`) or omit it for `latest`.",
            )
    return None


# ---------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------


class _Unavailable(Exception):
    """A network or registry failure."""


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


def _warning(name: str, code: str, message: str) -> dict[str, str]:
    return {"name": name, "code": code, "message": message}


def _registry_down(
    req: DependencyRequest, detail: str, warnings: list[dict[str, str]]
) -> ResolvedPackage | Rejection:
    """Fail open for an exact pin; a range or tag cannot resolve without the registry."""
    exact = _exact_version(req.range)
    if exact is not None:
        warnings.append(
            _warning(
                req.name,
                UNVERIFIED,
                f"the npm registry was unreachable ({detail}), so `{req.name}@{exact}` "
                "was declared as given without checking it exists. The build install "
                "will fail if it does not.",
            )
        )
        return ResolvedPackage(name=req.name, version=exact)
    return Rejection(
        req.name,
        REGISTRY_UNAVAILABLE,
        f"could not reach the npm registry to resolve `{req.name}@{req.range}` "
        f"({detail}). Nothing was declared; try again in a moment, or give an exact "
        "version.",
    )


async def _resolve_one(
    client: httpx.AsyncClient,
    req: DependencyRequest,
    *,
    registry: str,
    warnings: list[dict[str, str]],
) -> ResolvedPackage | Rejection:
    """Step 3 for one request: pick the version the request names."""
    try:
        status, packument = await _get_json(client, f"{registry}/{quote(req.name, safe='@')}")
    except _Unavailable as exc:
        return _registry_down(req, str(exc), warnings)
    if status == 404:
        return Rejection(
            req.name,
            NOT_FOUND,
            f"`{req.name}` does not exist on the npm registry. Check the spelling.",
        )
    versions = packument.get("versions") if isinstance(packument, dict) else None
    if not isinstance(versions, dict):
        return _registry_down(req, "malformed metadata", warnings)
    raw_tags = packument.get("dist-tags")
    tags = raw_tags if isinstance(raw_tags, dict) else {}

    parsed: list[tuple[SemVer, str]] = []
    for text, manifest in versions.items():
        ver = parse_version(text)
        if ver is not None and isinstance(manifest, dict):
            parsed.append((ver, text))
    parsed.sort(key=lambda item: item[0].key, reverse=True)

    def live(items: list[tuple[SemVer, str]]) -> list[tuple[SemVer, str]]:
        return [p for p in items if not versions[p[1]].get("deprecated")] or items

    chosen: str | None
    if _is_dist_tag(req.range):
        tagged = tags.get(req.range)
        if isinstance(tagged, str) and tagged in versions:
            chosen = tagged
        elif req.range == "latest":
            stable = [p for p in parsed if not p[0].pre]
            pool = live(stable or parsed)
            chosen = pool[0][1] if pool else None
        else:
            known = ", ".join(f"`{t}`" for t in sorted(tags)) or "none"
            return Rejection(
                req.name,
                NO_ELIGIBLE_VERSION,
                f"`{req.name}` has no `{req.range}` dist-tag (its tags: {known}).",
            )
        if chosen is None:
            return Rejection(
                req.name, NO_ELIGIBLE_VERSION, f"`{req.name}` has no published versions."
            )
    else:
        rng = parse_range(req.range)
        satisfying = [p for p in parsed if rng.satisfied_by(p[0])]
        if not satisfying:
            latest = tags.get("latest")
            hint = f" Its `latest` is {latest}." if isinstance(latest, str) else ""
            return Rejection(
                req.name,
                NO_ELIGIBLE_VERSION,
                f"no published version of `{req.name}` matches `{req.range}`.{hint}",
            )
        chosen = live(satisfying)[0][1]

    note = versions[chosen].get("deprecated")
    if note:
        warnings.append(
            _warning(
                req.name,
                DEPRECATED,
                f"`{req.name}@{chosen}` is deprecated on npm"
                + (f" (“{str(note)[:200]}”)" if isinstance(note, str) else "")
                + ".",
            )
        )
    return ResolvedPackage(name=req.name, version=chosen)


async def _advisory_warnings(
    client: httpx.AsyncClient, packages: Sequence[ResolvedPackage], *, registry: str
) -> list[dict[str, str]]:
    """Step 4, one bulk call for every resolved package. A failure warns nothing."""
    if not packages:
        return []
    body = {p.name: [p.version] for p in packages}
    try:
        resp = await client.post(f"{registry}{ADVISORIES_BULK_PATH}", json=body)
        if resp.status_code != 200:
            raise _Unavailable(f"HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise _Unavailable("malformed response")
    except (httpx.HTTPError, ValueError, _Unavailable) as exc:
        logger.info("sites.deps: advisory lookup skipped (%s)", exc)
        return []
    out: list[dict[str, str]] = []
    for pkg in packages:
        advisories = data.get(pkg.name) or []
        version = parse_version(pkg.version)
        for adv in advisories if isinstance(advisories, list) else []:
            if not isinstance(adv, dict):
                continue
            if str(adv.get("severity", "")).lower() not in WARNING_SEVERITIES:
                continue
            vulnerable = adv.get("vulnerable_versions")
            if isinstance(vulnerable, str) and version is not None:
                try:
                    if not parse_range(vulnerable).satisfied_by(version):
                        continue
                except ValueError:
                    pass  # unreadable range: mention it anyway
            title = str(adv.get("title") or "a known vulnerability")[:160]
            url = adv.get("url")
            out.append(
                _warning(
                    pkg.name,
                    ADVISORY,
                    f"`{pkg.name}@{pkg.version}` has a {adv.get('severity')} security "
                    f"advisory: {title}"
                    + (f" ({url})" if isinstance(url, str) else "")
                    + ". It was still declared; pick a patched version if one exists.",
                )
            )
    return out


async def _with_esm(client: httpx.AsyncClient, pkg: ResolvedPackage) -> ResolvedPackage:
    """Step 5 (html): the jsDelivr ``+esm`` URL, and its SRI hash when computable."""
    url = jsdelivr_esm_url(pkg.name, pkg.version)
    integrity: str | None = None
    try:
        resp = await client.get(url)
        if resp.status_code == 200 and resp.content:
            digest = base64.b64encode(hashlib.sha384(resp.content).digest()).decode("ascii")
            integrity = f"sha384-{digest}"
    except httpx.HTTPError as exc:
        logger.info("sites.deps: jsDelivr hash skipped for %s (%s)", pkg.name, exc)
    return ResolvedPackage(name=pkg.name, version=pkg.version, esm=url, integrity=integrity)


async def resolve_dependencies(
    requests: Iterable[DependencyRequest],
    engine: str,
    *,
    already_declared: Iterable[str] = (),
    client: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] | None = None,
    registry: str = REGISTRY_URL,
) -> ResolveResult:
    """Resolve ``requests`` for ``engine``. See the module docstring for the steps.

    ``already_declared`` and ``now`` are accepted for callers written against the
    old capped, age-floored policy; neither affects the result any more.
    """
    del already_declared, now
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
    if not pending:
        return result

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    try:
        gate = asyncio.Semaphore(MAX_CONCURRENT_LOOKUPS)

        async def _bounded(coro):
            async with gate:
                return await coro

        resolved = await asyncio.gather(
            *(
                _bounded(_resolve_one(http, r, registry=registry, warnings=result.warnings))
                for r in pending
            )
        )
        survivors = [v for v in resolved if isinstance(v, ResolvedPackage)]
        result.rejected.extend(v for v in resolved if isinstance(v, Rejection))
        result.warnings.extend(await _advisory_warnings(http, survivors, registry=registry))
        if engine == "html":
            survivors = list(
                await asyncio.gather(*(_bounded(_with_esm(http, p)) for p in survivors))
            )
        for pkg in survivors:
            result.packages[pkg.name] = pkg
    finally:
        if owns_client:
            await http.aclose()
    return result


__all__ = [
    "DependencyRequest",
    "Rejection",
    "ResolveResult",
    "ResolvedPackage",
    "SemVer",
    "WARNING_SEVERITIES",
    "coerce_requests",
    "parse_range",
    "parse_version",
    "resolve_dependencies",
]
