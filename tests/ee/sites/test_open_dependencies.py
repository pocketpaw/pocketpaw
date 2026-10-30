# tests/ee/sites/test_open_dependencies.py — the "open everything" author-dependency
# policy (captain decision 2026-10-07; workspace design draft
# docs/design/drafts/2026-10-07-sites-open-deps-real-preview.md).
#
# Reproduction tests: a site author may declare ANY public npm package. No size cap,
# no weekly-downloads floor, no advisory reject (a warning at most), no release-age
# floor (resolver or sandbox bunfig), dist-tags and prereleases resolve, install
# scripts are allowed, no 20-package cap, html needs no jsDelivr SRI, network hiccups
# resolve best-effort instead of rejecting, and the build-shell files (package.json,
# vite.config.*, svelte.config.js, bunfig.toml, .npmrc) are author-writable.
#
# The one boundary that STAYS: author installs never run on the API host
# (``GeneratorClient`` raises ``HostInstallRefused``). The Daytona sandbox is the
# isolation. ``test_author_installs_still_never_run_on_the_api_host`` pins it.
#
# Registry, downloads API, advisory endpoint and jsDelivr are an ``httpx.MockTransport``
# with a fixed clock, the same pattern as test_dependency_resolver.py.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.sites import dependency_manifest as dm
from pocketpaw_ee.sites import dependency_resolver as dr
from pocketpaw_ee.sites import generator_client as gc
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.bun_supply_chain import BUILD_BUNFIG
from pocketpaw_ee.sites.html_paths import html_path_rejection
from pocketpaw_ee.sites.legacy_build_shell import generator_owned_keys
from pocketpaw_ee.sites.react_paths import react_path_rejection
from pocketpaw_ee.sites.svelte_paths import svelte_path_rejection

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
MB = 1024 * 1024


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _packument(
    name: str,
    versions: dict[str, dict],
    times: dict[str, float],
    tags: dict[str, str] | None = None,
) -> dict:
    newest = max(versions, key=lambda v: dr.parse_version(v).key)  # type: ignore[union-attr]
    return {
        "name": name,
        "dist-tags": tags or {"latest": newest},
        "versions": {v: {"name": name, "version": v, **m} for v, m in versions.items()},
        "time": {v: _ago(d) for v, d in times.items()},
    }


class OpenRegistry:
    """A recorded registry shaped around the packages the old policy refused."""

    def __init__(self) -> None:
        self.packuments: dict[str, dict] = {
            # lucide-react's real unpacked size is ~35 MB: over the old 25 MB cap.
            "lucide-react": _packument(
                "lucide-react",
                {"0.540.0": {"dist": {"unpackedSize": 35 * MB}}},
                {"0.540.0": 30},
            ),
            "tiny-niche-lib": _packument("tiny-niche-lib", {"1.0.0": {}}, {"1.0.0": 60}),
            # The current `latest` was published yesterday; an older one is 40 days old.
            "fresh-latest": _packument(
                "fresh-latest",
                {"2.3.0": {}, "2.4.0": {}},
                {"2.3.0": 40, "2.4.0": 1},
                tags={"latest": "2.4.0"},
            ),
            "tagged": _packument(
                "tagged",
                {"1.0.0": {}, "2.0.0-beta.3": {}},
                {"1.0.0": 90, "2.0.0-beta.3": 3},
                tags={"latest": "1.0.0", "next": "2.0.0-beta.3", "beta": "2.0.0-beta.3"},
            ),
            "rc-only": _packument(
                "rc-only",
                {"0.9.0": {}, "1.0.0-rc.1": {}},
                {"0.9.0": 100, "1.0.0-rc.1": 2},
                tags={"latest": "0.9.0"},
            ),
            "moderately-vulnerable": _packument(
                "moderately-vulnerable", {"3.1.0": {}}, {"3.1.0": 50}
            ),
            "has-postinstall": _packument(
                "has-postinstall",
                {"1.2.0": {"scripts": {"postinstall": "node setup.js"}}},
                {"1.2.0": 50},
            ),
            "three": _packument("three", {"0.170.0": {}}, {"0.170.0": 30}),
        }
        for i in range(25):
            self.packuments[f"pkg-{i}"] = _packument(f"pkg-{i}", {"1.0.0": {}}, {"1.0.0": 30})
        self.downloads: dict[str, int] = {n: 50_000 for n in self.packuments}
        self.downloads["tiny-niche-lib"] = 20
        self.advisories: dict[str, list[dict]] = {
            "moderately-vulnerable": [
                {
                    "id": 7,
                    "severity": "moderate",
                    "vulnerable_versions": "<4.0.0",
                    "title": "ReDoS in a rarely used helper",
                    "url": "https://github.com/advisories/GHSA-mod",
                }
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
            return httpx.Response(200, content=b"export default 1;\n")
        if url.startswith("https://api.npmjs.org/downloads/point/last-week/"):
            if "downloads" in self.down:
                raise httpx.ReadTimeout("downloads API timed out", request=request)
            name = url.split("/last-week/", 1)[1]
            return httpx.Response(200, json={"downloads": self.downloads.get(name, 0)})
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
def registry() -> OpenRegistry:
    return OpenRegistry()


async def _resolve(registry: OpenRegistry, *specs, engine: str = "react", already=()):
    requests, bad = dr.coerce_requests(list(specs))
    assert not bad
    async with httpx.AsyncClient(transport=httpx.MockTransport(registry.handler)) as client:
        return await dr.resolve_dependencies(
            requests, engine, already_declared=already, client=client, now=lambda: NOW
        )


def _rejections(result: dr.ResolveResult) -> list[dict[str, str]]:
    return [r.as_dict() for r in result.rejected]


# ---------------------------------------------------------------------------
# Resolver: every old gate is open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_35_mb_package_like_lucide_react_is_accepted(registry):
    result = await _resolve(registry, {"name": "lucide-react"})
    assert _rejections(result) == []
    assert result.packages["lucide-react"].version == "0.540.0"


@pytest.mark.asyncio
async def test_a_package_with_20_weekly_downloads_is_accepted(registry):
    result = await _resolve(registry, {"name": "tiny-niche-lib"})
    assert _rejections(result) == []
    assert result.packages["tiny-niche-lib"].version == "1.0.0"


@pytest.mark.asyncio
async def test_latest_picks_a_version_published_one_day_ago(registry):
    result = await _resolve(registry, {"name": "fresh-latest"})
    assert _rejections(result) == []
    assert result.packages["fresh-latest"].version == "2.4.0"


@pytest.mark.asyncio
async def test_an_exact_version_published_one_day_ago_resolves(registry):
    result = await _resolve(registry, {"name": "fresh-latest", "range": "2.4.0"})
    assert _rejections(result) == []
    assert result.packages["fresh-latest"].version == "2.4.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["next", "beta"])
async def test_a_dist_tag_resolves_to_its_version(registry, tag):
    result = await _resolve(registry, {"name": "tagged", "range": tag})
    assert _rejections(result) == []
    assert result.packages["tagged"].version == "2.0.0-beta.3"


@pytest.mark.asyncio
async def test_an_explicit_prerelease_resolves(registry):
    result = await _resolve(registry, {"name": "rc-only", "range": "1.0.0-rc.1"})
    assert _rejections(result) == []
    assert result.packages["rc-only"].version == "1.0.0-rc.1"


@pytest.mark.asyncio
async def test_a_moderate_advisory_does_not_reject(registry):
    result = await _resolve(registry, {"name": "moderately-vulnerable"})
    assert _rejections(result) == []
    assert result.packages["moderately-vulnerable"].version == "3.1.0"


@pytest.mark.asyncio
async def test_a_moderate_advisory_surfaces_as_a_warning(registry):
    """Optional half of the advisory decision: tell the agent, don't block it."""
    result = await _resolve(registry, {"name": "moderately-vulnerable"})
    warnings = getattr(result, "warnings", None)
    assert warnings, "ResolveResult should carry a `warnings` list for advisories"
    assert any("moderately-vulnerable" in json.dumps(w, default=str) for w in warnings)


@pytest.mark.asyncio
async def test_a_package_with_install_scripts_is_accepted(registry):
    """ignoreScripts is gone from the sandbox bunfig, so the resolver must not refuse it."""
    result = await _resolve(registry, {"name": "has-postinstall"})
    assert _rejections(result) == []
    assert result.packages["has-postinstall"].version == "1.2.0"


@pytest.mark.asyncio
async def test_twenty_five_packages_are_accepted(registry):
    specs = [{"name": f"pkg-{i}"} for i in range(25)]
    result = await _resolve(registry, *specs)
    assert _rejections(result) == []
    assert len(result.packages) == 25


@pytest.mark.asyncio
async def test_a_25th_package_on_top_of_24_declared_is_accepted(registry):
    already = [f"pkg-{i}" for i in range(24)]
    result = await _resolve(registry, {"name": "three"}, already=already)
    assert _rejections(result) == []
    assert "three" in result.packages


# ---------------------------------------------------------------------------
# Resolver: network hiccups resolve best-effort, never reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_downloads_api_timeout_does_not_reject(registry):
    registry.down.add("downloads")
    result = await _resolve(registry, {"name": "three"})
    assert _rejections(result) == []
    assert result.packages["three"].version == "0.170.0"


@pytest.mark.asyncio
async def test_an_advisory_endpoint_failure_does_not_reject(registry):
    registry.down.add("advisories")
    result = await _resolve(registry, {"name": "three"})
    assert _rejections(result) == []
    assert result.packages["three"].version == "0.170.0"


@pytest.mark.asyncio
async def test_an_unreachable_registry_still_accepts_an_exact_version(registry):
    """Best effort: an exact pin needs no packument to be usable."""
    registry.down.add("registry")
    result = await _resolve(registry, {"name": "three", "range": "0.170.0"})
    assert _rejections(result) == []
    assert result.packages["three"].version == "0.170.0"


# ---------------------------------------------------------------------------
# html: no SRI requirement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_resolution_does_not_need_the_jsdelivr_integrity_hash(registry):
    """jsDelivr down used to reject (it was fetched only to hash for SRI)."""
    registry.down.add("cdn")
    result = await _resolve(registry, {"name": "three"}, engine="html")
    assert _rejections(result) == []
    pkg = result.packages["three"]
    assert pkg.version == "0.170.0"
    assert "integrity" not in pkg.manifest_entry() or pkg.integrity is None


# ---------------------------------------------------------------------------
# Sandbox bunfig: no release-age floor, install scripts allowed
# ---------------------------------------------------------------------------


def test_build_bunfig_has_no_release_age_floor():
    assert "minimumReleaseAge" not in BUILD_BUNFIG


def test_build_bunfig_does_not_ignore_install_scripts():
    assert "ignoreScripts = true" not in BUILD_BUNFIG.replace("  ", " ")
    assert "ignoreScripts=true" not in BUILD_BUNFIG.replace(" ", "")


# ---------------------------------------------------------------------------
# Build-shell files are author-writable
# ---------------------------------------------------------------------------

_SVELTE_PAGE = {"src/routes/+page.svelte": "<h1>Hi</h1>"}
_REACT_PAGE = {"src/App.tsx": "export default function App(){return <h1>Hi</h1>}"}

_SHELL_FILES = {
    "package.json": '{"name":"x","dependencies":{"lucide-react":"latest"}}',
    "vite.config.ts": "import { defineConfig } from 'vite'; export default defineConfig({});",
    "bunfig.toml": "[install]\n",
    ".npmrc": "legacy-peer-deps=true\n",
}


@pytest.mark.parametrize(
    ("engine", "page", "extra"),
    [
        ("svelte", _SVELTE_PAGE, {"svelte.config.js": "export default {};"}),
        ("react", _REACT_PAGE, {}),
    ],
)
def test_a_source_map_with_build_shell_files_is_not_generator_owned(engine, page, extra):
    source = {**page, **_SHELL_FILES, **extra}
    assert generator_owned_keys(engine, source) == []


@pytest.mark.parametrize(
    ("engine", "page", "extra"),
    [
        ("svelte", _SVELTE_PAGE, {"svelte.config.js": "export default {};"}),
        ("react", _REACT_PAGE, {}),
    ],
)
def test_the_preflight_does_not_422_on_build_shell_files(engine, page, extra):
    """``_refuse_generator_owned_source`` raises ``sites.generator_owned_file`` today."""
    sites_service._refuse_generator_owned_source(engine, {**page, **_SHELL_FILES, **extra})


@pytest.mark.parametrize(
    "path",
    ["package.json", "vite.config.ts", "svelte.config.js", "bunfig.toml", ".npmrc"],
)
def test_the_svelte_edit_lane_may_write_build_shell_files(path):
    assert svelte_path_rejection(path) is None


@pytest.mark.parametrize("path", ["package.json", "vite.config.ts", "bunfig.toml", ".npmrc"])
def test_the_react_edit_lane_may_write_build_shell_files(path):
    assert react_path_rejection(path) is None


@pytest.mark.parametrize("path", ["package.json", "bunfig.toml", ".npmrc"])
def test_the_html_edit_lane_may_write_build_shell_files(path):
    assert html_path_rejection(path) is None


# ---------------------------------------------------------------------------
# Agent-facing text no longer repeats the old policy
# ---------------------------------------------------------------------------

_OLD_POLICY_PHRASES = (
    "7 days",
    "weekly downloads",
    "at most 20",
    "size cap",
    "moderate-or-worse",
    "popular enough",
    "no install scripts",
)

_SKILLS_ROOT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "pocketpaw"
    / "bundled_skills"
    / "_bundled"
    / "skills"
)
_SITE_SKILLS = sorted(
    p / "SKILL.md"
    for p in _SKILLS_ROOT.iterdir()
    if p.is_dir()
    and (p.name.startswith("pocketpaw-create-") or p.name.startswith("pocketpaw-edit-"))
    and p.name.endswith("-site")
    and (p / "SKILL.md").is_file()
)


def _tool_texts() -> dict[str, str]:
    from pocketpaw_ee.agent.mcp_servers import sites_create

    captured: dict[str, str] = {}

    def fake_tool(name, description, schema):
        captured[name] = description + "\n" + json.dumps(schema)
        return lambda fn: fn

    sites_create.make_set_site_dependencies_tool(fake_tool)
    captured["DEPENDENCY_REQUESTS_SCHEMA"] = json.dumps(sites_create.DEPENDENCY_REQUESTS_SCHEMA)
    return captured


def test_the_site_skills_were_found():
    assert len(_SITE_SKILLS) >= 4, _SITE_SKILLS


@pytest.mark.parametrize("name", ["set_site_dependencies", "DEPENDENCY_REQUESTS_SCHEMA"])
def test_tool_text_does_not_repeat_the_old_policy(name):
    text = _tool_texts()[name]
    hits = [p for p in _OLD_POLICY_PHRASES if p in text]
    assert hits == [], f"{name} still states the old policy: {hits}"


@pytest.mark.parametrize("skill", _SITE_SKILLS, ids=lambda p: p.parent.name)
def test_site_skills_do_not_repeat_the_old_policy(skill):
    text = skill.read_text(encoding="utf-8")
    hits = [p for p in (*_OLD_POLICY_PHRASES, "with SRI") if p in text]
    assert hits == [], f"{skill.parent.name} still states the old policy: {hits}"


@pytest.mark.parametrize("skill", _SITE_SKILLS, ids=lambda p: p.parent.name)
def test_site_skills_do_not_force_dynamic_import_for_every_package(skill):
    """Component libraries (lucide-react, bits-ui...) import at the top like any module."""
    text = skill.read_text(encoding="utf-8")
    assert "never at the top of" not in text, (
        f"{skill.parent.name} forbids top-level imports of every declared package"
    )


# ---------------------------------------------------------------------------
# The boundary that stays: author installs never run on the API host
# ---------------------------------------------------------------------------


class _RecordingRunner:
    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir
        self.calls: list[str] = []

    async def generate(self, input_json, out_dir):
        self.calls.append("generate")
        return {"projectDir": self.project_dir}

    async def install(self, project_dir):
        self.calls.append("install")
        return True, ""

    async def build_static(self, project_dir, *, gate):
        self.calls.append("build_static")
        return True, ""

    async def smoke(self, project_dir):
        self.calls.append("smoke")
        return True, ""


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["svelte", "react"])
async def test_author_installs_still_never_run_on_the_api_host(tmp_path, engine):
    """GUARD (passes today, must keep passing): the sandbox is the isolation."""
    manifest = dm.render_manifest({"lucide-react": {"version": "0.540.0"}})
    runner = _RecordingRunner(str(tmp_path))
    client = gc.GeneratorClient(_runner=runner)
    with pytest.raises(gc.HostInstallRefused):
        await client.build(
            theme={},
            site_id="s1",
            title="t",
            capture_api_base="http://x",
            capture_signed_key="k",
            engine=engine,
            source={**_REACT_PAGE, dm.DEPENDENCY_MANIFEST_PATH: manifest},
        )
    assert runner.calls == []
