# tests/ee/sites/test_dependency_resolver.py — the author-dependency resolver (PP-1).
#
# Created: 2026-09-24 (feat/sites-author-dependencies). The resolver runs against a
# RECORDED FAKE REGISTRY (an ``httpx.MockTransport`` serving packuments, download
# counts, advisories and jsdelivr bytes) with a fixed clock, so every rejection reason
# is exercised without the network. Each gate test names the mutation that breaks it;
# ``tests/mutations/sites_author_dependencies.json`` applies those mutations.

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pocketpaw_ee.sites import dependency_resolver as dr
from pocketpaw_ee.sites.bun_supply_chain import BUILD_BUNFIG, MINIMUM_RELEASE_AGE_SECONDS

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _packument(name: str, versions: dict[str, dict], times: dict[str, float]) -> dict:
    return {
        "name": name,
        "versions": {v: {"name": name, "version": v, **m} for v, m in versions.items()},
        "time": {v: _ago(d) for v, d in times.items()},
    }


ESM_BYTES = b"export default 42;\n"


class FakeRegistry:
    """A recorded registry. Mutate the dicts in a test to shape one scenario."""

    def __init__(self) -> None:
        self.packuments: dict[str, dict] = {
            "three": _packument(
                "three",
                {"0.169.0": {}, "0.170.0": {}, "0.171.0": {}},
                # 0.171.0 is only 2 days old: the floor must fall back to 0.170.0.
                {"0.169.0": 60, "0.170.0": 20, "0.171.0": 2},
            ),
            "gsap": _packument("gsap", {"3.12.5": {}, "3.13.0": {}}, {"3.12.5": 300, "3.13.0": 90}),
            "@scope/pkg": _packument("@scope/pkg", {"1.0.0": {}}, {"1.0.0": 100}),
            "old-thing": _packument(
                "old-thing", {"1.0.0": {"deprecated": "use new-thing"}}, {"1.0.0": 900}
            ),
            "half-deprecated": _packument(
                "half-deprecated",
                {"1.0.0": {}, "1.1.0": {"deprecated": "broken release"}},
                {"1.0.0": 200, "1.1.0": 100},
            ),
            "scripted": _packument(
                "scripted", {"1.0.0": {"scripts": {"postinstall": "node x.js"}}}, {"1.0.0": 100}
            ),
            "native": _packument("native", {"1.0.0": {"gypfile": True}}, {"1.0.0": 100}),
            "binaryish": _packument(
                "binaryish", {"1.0.0": {"binary": {"module_name": "x"}}}, {"1.0.0": 100}
            ),
            "huge": _packument(
                "huge", {"1.0.0": {"dist": {"unpackedSize": 30 * 1024 * 1024}}}, {"1.0.0": 100}
            ),
            "typosquat": _packument("typosquat", {"1.0.0": {}}, {"1.0.0": 100}),
            "vulnerable": _packument(
                "vulnerable", {"1.0.0": {}, "2.0.0": {}}, {"1.0.0": 400, "2.0.0": 100}
            ),
            "brand-new": _packument("brand-new", {"1.0.0": {}}, {"1.0.0": 1}),
        }
        self.downloads: dict[str, int] = {name: 50_000 for name in self.packuments}
        self.downloads["typosquat"] = 12
        self.advisories: dict[str, list[dict]] = {
            "vulnerable": [
                {
                    "id": 1,
                    "severity": "high",
                    "vulnerable_versions": ">=2.0.0 <2.0.5",
                    "title": "Prototype pollution",
                    "url": "https://github.com/advisories/GHSA-x",
                },
                {"id": 2, "severity": "low", "vulnerable_versions": "*", "title": "meh"},
            ]
        }
        self.down: set[str] = set()  # "registry" | "downloads" | "advisories" | "cdn"
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url.startswith("https://cdn.jsdelivr.net/"):
            if "cdn" in self.down:
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, content=ESM_BYTES)
        if url.startswith("https://api.npmjs.org/downloads/point/last-week/"):
            if "downloads" in self.down:
                return httpx.Response(503)
            name = url.split("/last-week/", 1)[1]
            if name not in self.downloads:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"downloads": self.downloads[name], "package": name})
        if url.endswith(dr.ADVISORIES_BULK_PATH):
            if "advisories" in self.down:
                return httpx.Response(500)
            asked = json.loads(request.content)
            return httpx.Response(200, json={k: self.advisories.get(k, []) for k in asked})
        if url.startswith("https://registry.npmjs.org/"):
            if "registry" in self.down:
                raise httpx.ConnectTimeout("slow", request=request)
            name = httpx.URL(url).path.lstrip("/").replace("%2F", "/").replace("%2f", "/")
            if name not in self.packuments:
                return httpx.Response(404, json={"error": "Not found"})
            return httpx.Response(200, json=self.packuments[name])
        return httpx.Response(599)


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


async def _resolve(registry: FakeRegistry, *specs, engine: str = "svelte", already=()):
    requests, bad = dr.coerce_requests(list(specs))
    assert not bad
    async with httpx.AsyncClient(transport=httpx.MockTransport(registry.handler)) as client:
        return await dr.resolve_dependencies(
            requests, engine, already_declared=already, client=client, now=lambda: NOW
        )


def _codes(result: dr.ResolveResult) -> dict[str, str]:
    return {r.name: r.code for r in result.rejected}


# ---------------------------------------------------------------------------
# The floor constant is the bunfig's floor
# ---------------------------------------------------------------------------


def test_the_resolver_floor_is_the_sandbox_bunfig_floor():
    """The version the agent is told about must be one the sandbox will install."""
    assert MINIMUM_RELEASE_AGE_SECONDS == 7 * 24 * 3600
    assert f"minimumReleaseAge = {MINIMUM_RELEASE_AGE_SECONDS}" in BUILD_BUNFIG


# ---------------------------------------------------------------------------
# semver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rng", "version", "ok"),
    [
        ("^1.2.3", "1.9.0", True),
        ("^1.2.3", "2.0.0", False),
        ("^0.2.3", "0.2.9", True),
        ("^0.2.3", "0.3.0", False),
        ("^0.0.3", "0.0.4", False),
        ("~1.2.3", "1.2.9", True),
        ("~1.2.3", "1.3.0", False),
        ("1.x", "1.4.0", True),
        ("1.2", "1.3.0", False),
        (">=1 <2", "1.99.0", True),
        (">=1 <2", "2.0.0", False),
        (">= 1.2.0", "1.2.0", True),
        ("1.0.0 - 1.5", "1.5.9", True),
        ("1.0.0 - 1.5", "1.6.0", False),
        ("<1.2", "1.1.9", True),
        ("^1 || ^3", "3.1.0", True),
        ("^1 || ^3", "2.1.0", False),
        ("latest", "9.9.9", True),
        ("*", "0.0.1", True),
        ("^1.0.0", "1.1.0-beta.1", False),
        ("^1.1.0-beta.0", "1.1.0-beta.1", True),
        ("0.170.0", "0.170.0", True),
    ],
)
def test_ranges(rng: str, version: str, ok: bool):
    parsed = dr.parse_version(version)
    assert parsed is not None
    assert dr.parse_range(rng).satisfied_by(parsed) is ok


@pytest.mark.parametrize("bad", ["next", "not a range", "^^1", "1.2.3.4"])
def test_unreadable_ranges_raise(bad: str):
    with pytest.raises(ValueError):
        dr.parse_range(bad)


def test_prerelease_orders_below_its_release():
    a, b = dr.parse_version("1.0.0-rc.1"), dr.parse_version("1.0.0")
    assert a is not None and b is not None and a < b


# ---------------------------------------------------------------------------
# The happy path and the floor fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolves_the_newest_version_old_enough_to_install(registry):
    """0.171.0 is 2 days old, so the floor falls back to 0.170.0.

    Mutation: drop the ``p[2] <= cutoff`` filter → 0.171.0 is picked.
    """
    result = await _resolve(registry, {"name": "three"})
    assert result.rejected == []
    assert result.packages["three"].version == "0.170.0"
    assert result.packages["three"].manifest_entry() == {"version": "0.170.0"}


@pytest.mark.asyncio
async def test_a_range_is_honoured(registry):
    result = await _resolve(registry, {"name": "three", "range": "~0.169.0"})
    assert result.packages["three"].version == "0.169.0"


@pytest.mark.asyncio
async def test_scoped_names_resolve(registry):
    result = await _resolve(registry, "@scope/pkg@^1")
    assert result.packages["@scope/pkg"].version == "1.0.0"
    assert any("%2F" in u or "%2f" in u for u in registry.calls)


@pytest.mark.asyncio
async def test_a_deprecated_version_is_skipped_for_an_older_good_one(registry):
    result = await _resolve(registry, {"name": "half-deprecated"})
    assert result.packages["half-deprecated"].version == "1.0.0"


# ---------------------------------------------------------------------------
# Every rejection reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_satisfying_version_names_the_newest_eligible(registry):
    result = await _resolve(registry, {"name": "three", "range": "^1.0.0"})
    [rej] = result.rejected
    assert rej.code == dr.NO_ELIGIBLE_VERSION
    assert "0.170.0" in rej.reason  # the newest ELIGIBLE, not the too-new 0.171.0
    assert "three" not in result.packages


@pytest.mark.asyncio
async def test_only_too_new_versions_is_refused_with_the_floor_named(registry):
    """Mutation: treat ``aged == []`` as ok → brand-new@1.0.0 is accepted."""
    result = await _resolve(registry, {"name": "brand-new"})
    [rej] = result.rejected
    assert rej.code == dr.NO_ELIGIBLE_VERSION
    assert "7 days" in rej.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({"name": "Three"}, dr.INVALID_NAME),
        ({"name": ".hidden"}, dr.INVALID_NAME),
        ({"name": "a" * 215}, dr.INVALID_NAME),
        ({"name": "fs"}, dr.INVALID_NAME),
        ({"name": "three", "range": "github:mrdoob/three.js"}, dr.NON_REGISTRY_SPEC),
        ({"name": "three", "range": "git+https://github.com/x/y.git"}, dr.NON_REGISTRY_SPEC),
        ({"name": "three", "range": "file:../three"}, dr.NON_REGISTRY_SPEC),
        ({"name": "three", "range": "npm:other@1"}, dr.NON_REGISTRY_SPEC),
        ({"name": "three", "range": "https://x.test/three.tgz"}, dr.NON_REGISTRY_SPEC),
        ({"name": "three", "range": "mrdoob/three.js"}, dr.NON_REGISTRY_SPEC),
        ({"name": "three", "range": "next"}, dr.INVALID_RANGE),
        ({"name": "svelte"}, dr.TOOLCHAIN_RESERVED),
        ({"name": "@sveltejs/kit"}, dr.TOOLCHAIN_RESERVED),
        ({"name": "react-dom"}, dr.TOOLCHAIN_RESERVED),
        ({"name": "@tailwindcss/vite"}, dr.TOOLCHAIN_RESERVED),
        ({"name": "@ripple-ui/svelte"}, dr.TOOLCHAIN_RESERVED),
        ({"name": "@cloudflare/workers-types"}, dr.TOOLCHAIN_RESERVED),
        ({"name": "valibot"}, dr.TOOLCHAIN_RESERVED),
        ({"name": "does-not-exist"}, dr.NOT_FOUND),
        ({"name": "old-thing"}, dr.DEPRECATED),
        ({"name": "scripted"}, dr.INSTALL_SCRIPTS),
        ({"name": "native"}, dr.NATIVE_BUILD),
        ({"name": "binaryish"}, dr.NATIVE_BUILD),
        ({"name": "huge"}, dr.TOO_LARGE),
        ({"name": "typosquat"}, dr.LOW_DOWNLOADS),
        ({"name": "vulnerable"}, dr.ADVISORY),
    ],
)
async def test_every_rejection_reason(registry, spec, code):
    """One row per gate. Each row's gate is a mutation in the plan."""
    result = await _resolve(registry, spec)
    assert _codes(result) == {spec["name"]: code}, result.rejected
    assert result.packages == {}
    assert result.rejected[0].reason  # actionable text, never empty


@pytest.mark.asyncio
async def test_a_low_severity_or_unaffected_advisory_does_not_block(registry):
    result = await _resolve(registry, {"name": "vulnerable", "range": "^1"})
    assert result.packages["vulnerable"].version == "1.0.0"


@pytest.mark.asyncio
async def test_toolchain_names_never_reach_the_network(registry):
    await _resolve(registry, {"name": "svelte"})
    assert registry.calls == []


@pytest.mark.asyncio
async def test_more_than_twenty_is_refused_past_the_cap(registry):
    """Mutation: raise MAX_DECLARED_PACKAGES → the 21st is accepted."""
    already = [f"pkg-{i}" for i in range(19)]
    result = await _resolve(registry, {"name": "three"}, {"name": "gsap"}, already=already)
    assert "three" in result.packages
    assert _codes(result) == {"gsap": dr.TOO_MANY}


@pytest.mark.asyncio
async def test_re_resolving_a_declared_name_takes_no_new_slot(registry):
    already = [f"pkg-{i}" for i in range(19)] + ["three"]
    result = await _resolve(registry, {"name": "three"}, already=already)
    assert "three" in result.packages


@pytest.mark.asyncio
async def test_ripple_refuses_everything(registry):
    result = await _resolve(registry, {"name": "three"}, engine="ripple")
    assert _codes(result) == {"three": dr.ENGINE_UNSUPPORTED}
    assert registry.calls == []


# ---------------------------------------------------------------------------
# html: esm URL + SRI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_gets_the_jsdelivr_url_and_a_sha384_of_its_bytes(registry):
    result = await _resolve(registry, {"name": "three"}, engine="html")
    pkg = result.packages["three"]
    assert pkg.esm == "https://cdn.jsdelivr.net/npm/three@0.170.0/+esm"
    expected = "sha384-" + base64.b64encode(hashlib.sha384(ESM_BYTES).digest()).decode()
    assert pkg.integrity == expected
    assert pkg.manifest_entry() == {"version": "0.170.0", "esm": pkg.esm, "integrity": expected}


@pytest.mark.asyncio
async def test_svelte_and_react_get_no_esm_fields(registry):
    for engine in ("svelte", "react"):
        result = await _resolve(registry, {"name": "three"}, engine=engine)
        assert result.packages["three"].esm is None
        assert result.packages["three"].integrity is None


# ---------------------------------------------------------------------------
# Registry down: never a silent accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["registry", "downloads", "advisories"])
async def test_an_unreachable_registry_refuses_rather_than_accepts(registry, which):
    """Mutation: return the package on an advisory failure → accepted unvetted."""
    registry.down.add(which)
    result = await _resolve(registry, {"name": "three"})
    assert result.packages == {}
    assert _codes(result) == {"three": dr.REGISTRY_UNAVAILABLE}


@pytest.mark.asyncio
async def test_an_unreachable_cdn_refuses_an_html_package(registry):
    registry.down.add("cdn")
    result = await _resolve(registry, {"name": "three"}, engine="html")
    assert result.packages == {}
    assert _codes(result) == {"three": dr.REGISTRY_UNAVAILABLE}


# ---------------------------------------------------------------------------
# Request coercion
# ---------------------------------------------------------------------------


def test_coerce_accepts_objects_and_name_at_range_strings():
    reqs, bad = dr.coerce_requests([{"name": "three"}, "gsap@^3", "@scope/pkg@1.0.0", "lenis"])
    assert bad == []
    assert [(r.name, r.range) for r in reqs] == [
        ("three", "latest"),
        ("gsap", "^3"),
        ("@scope/pkg", "1.0.0"),
        ("lenis", "latest"),
    ]


def test_coerce_rejects_a_non_list_and_bad_items():
    _reqs, bad = dr.coerce_requests("three")
    assert bad and bad[0].code == dr.INVALID_NAME
    _reqs, bad = dr.coerce_requests([42, {"name": 1}])
    assert len(bad) == 2
