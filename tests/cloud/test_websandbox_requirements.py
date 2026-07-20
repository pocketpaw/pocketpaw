# test_websandbox_requirements.py — tests for the pre-boot runtime requirements
# probe and the per-runtime credential broker (RR-2).
#
# Two contracts are worth defending here, and neither is about happy-path output:
#
#   1. UNKNOWN MUST ROUTE UP, NEVER DOWN. Every way the probe can fail to learn
#      something — no package.json, GitHub down, a 404, a private repo, a
#      corrupt manifest — must resolve to the MOST capable requirements, with a
#      reason. Under-provisioning a runtime breaks the user's session; over-
#      provisioning only slows it. A test that only checked the happy path would
#      let a future refactor quietly invert that.
#
#   2. THE ANSWER MUST BE EXPLAINABLE. Every flag raised to true carries at least
#      one reason naming the evidence. That is the difference between a routing
#      decision and a guess, so it is asserted structurally (for every flag, on
#      every path) rather than by spot-checking one string.
#
# Also pinned: the probe fetches ONE FILE, not the 100MB archive — the old path's
# whole problem was paying VM-and-repo cost to answer a cheap question — and the
# SSRF boundary is the same one archive.py enforces, so no caller-supplied string
# reaches httpx as a URL.
from __future__ import annotations

import json

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.websandbox import requirements, runtimes


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class _FakeClient:
    """Records the URL and params so tests can assert what was actually fetched."""

    last_url: str | None = None
    last_params: dict | None = None

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, headers=None, params=None):
        type(self).last_url = url
        type(self).last_params = params
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture(autouse=True)
def _reset():
    _FakeClient.last_url = None
    _FakeClient.last_params = None
    # The manifest cache is module-level and therefore survives between tests.
    # Without this, a test that primed `octocat/Hello-World` made a later test
    # pass while never touching the network — which is precisely the assertion
    # that later test exists to make. Cross-test leakage through a cache is
    # invisible when it makes things pass, so reset it explicitly.
    requirements._reset_cache_for_tests()


def _patch_client(monkeypatch: pytest.MonkeyPatch, response) -> None:
    monkeypatch.setattr(requirements.httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))


def _manifest(**sections) -> bytes:
    return json.dumps(sections).encode()


def _assert_every_true_flag_has_a_reason(result) -> None:
    """The core contract: a raised flag without evidence is a guess.

    Matches the emitted GRAMMAR (``... -> <flag>``) rather than searching the
    joined text for the flag name. A substring search looked equivalent and was
    not: the nativeToolchain reason contains the word "install", so deleting the
    install reason entirely still passed while ``install`` stayed true — the one
    regression this assertion exists to catch.
    """
    assert result.reasons, "a verdict with no reasons is unexplainable"
    for flag in ("install", "nativeToolchain", "rawSockets"):
        if getattr(result, flag):
            assert any(reason.rstrip().endswith(f"-> {flag}") for reason in result.reasons), (
                f"{flag} is true but no reason concludes in it: {result.reasons}"
            )


# ---------------------------------------------------------------------------
# Inference from a manifest — the pure half.
# ---------------------------------------------------------------------------


def test_package_json_implies_install() -> None:
    result = requirements.infer_from_package_json(json.dumps({"dependencies": {"express": "^4"}}))

    assert result.install is True
    assert result.rawSockets is False
    _assert_every_true_flag_has_a_reason(result)


# ---------------------------------------------------------------------------
# nativeToolchain — EVIDENCE, not assumption.
#
# The probe used to hardcode ``nativeToolchain=True`` for every manifest,
# reasoning that "any non-trivial tree pulls prebuilt native binaries (esbuild,
# rollup's native bindings, sharp, node-gyp fallbacks)". That conflated two
# different things under one flag:
#
#   • a package that ships a prebuilt binary WITH a WASM fallback (esbuild,
#     rollup) — runs fine in an in-tab runtime, which our own 2026-07-18
#     WebContainers gate run demonstrated: npm install exit 0 over 320 packages,
#     then a working Vite dev server on the rollup toolchain.
#   • a package that must COMPILE native code or load a binary with no WASM
#     path (better-sqlite3, node-gyp, sharp) — genuinely cannot.
#
# ``Capabilities.nativeToolchain`` in the client registry is defined as the
# SECOND ("compile and run NATIVE code: gcc, node-gyp, native node modules"), so
# the blanket true made every project claim a need it did not have, and no
# in-tab runtime could ever be selected for anything. The flag is unselectable-
# by-construction in one direction and useless in the other; these tests pin the
# distinction.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dependencies",
    [
        {"express": "^4"},
        # The exact packages the old blanket reason cited. Both ship prebuilt
        # binaries with a WASM/JS fallback, and both are proven to work in an
        # in-tab runtime by the gate run.
        {"vite": "^5", "rollup": "^4"},
        {"esbuild": "^0.20"},
        {"svelte": "^5", "@sveltejs/kit": "^2"},
    ],
)
def test_a_plain_node_project_does_not_need_a_native_toolchain(dependencies: dict) -> None:
    result = requirements.infer_from_package_json(json.dumps({"dependencies": dependencies}))

    assert result.nativeToolchain is False, (
        f"{sorted(dependencies)} compiles nothing native; claiming otherwise makes every "
        f"in-tab runtime unselectable. Reasons: {result.reasons}"
    )
    _assert_every_true_flag_has_a_reason(result)


@pytest.mark.parametrize(
    "package",
    ["better-sqlite3", "sqlite3", "sharp", "canvas", "bcrypt", "node-gyp", "serialport"],
)
def test_a_package_that_compiles_native_code_raises_the_flag(package: str) -> None:
    result = requirements.infer_from_package_json(json.dumps({"dependencies": {package: "^1"}}))

    assert result.nativeToolchain is True
    assert any(package in reason for reason in result.reasons), (
        f"the reason must name the evidence, got: {result.reasons}"
    )
    _assert_every_true_flag_has_a_reason(result)


def test_a_native_dev_dependency_also_counts() -> None:
    # A build step that compiles an addon needs the toolchain just as much as a
    # runtime dependency does.
    result = requirements.infer_from_package_json(
        json.dumps({"devDependencies": {"sharp": "^0.33"}})
    )

    assert result.nativeToolchain is True
    assert any("devDependencies" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "scripts",
    [
        {"install": "node-gyp rebuild"},
        {"postinstall": "node-pre-gyp install --fallback-to-build"},
        {"rebuild": "cmake-js compile"},
        {"build:native": "make -C src/native"},
    ],
)
def test_a_build_script_that_compiles_raises_the_flag(scripts: dict) -> None:
    # The strongest possible evidence: the manifest says, in its own words, that
    # it shells out to a compiler.
    result = requirements.infer_from_package_json(json.dumps({"scripts": scripts}))

    assert result.nativeToolchain is True
    _assert_every_true_flag_has_a_reason(result)


def test_gypfile_raises_the_flag() -> None:
    # npm's own marker that this package carries a binding.gyp.
    result = requirements.infer_from_package_json(json.dumps({"gypfile": True}))

    assert result.nativeToolchain is True


def test_a_scoped_native_package_is_matched_by_prefix() -> None:
    result = requirements.infer_from_package_json(
        json.dumps({"dependencies": {"@napi-rs/canvas": "^0.1"}})
    )

    assert result.nativeToolchain is True


def test_platform_binaries_in_optional_dependencies_do_not_raise_the_flag() -> None:
    """The regression that would silently restore the old behaviour.

    Rollup, esbuild and swc all declare their per-platform prebuilt binaries as
    ``optionalDependencies`` — ``@rollup/rollup-linux-x64-gnu`` and friends. npm
    installs whichever matches the host and SKIPS the rest, and every one of them
    has a JS or WASM fallback, which is exactly why the gate run's Vite app
    worked. Scanning that section for native-looking names would mark every
    modern frontend project as needing a compiler again.
    """
    result = requirements.infer_from_package_json(
        json.dumps(
            {
                "dependencies": {"vite": "^5"},
                "optionalDependencies": {
                    "@rollup/rollup-linux-x64-gnu": "4.9.0",
                    "@esbuild/darwin-arm64": "0.20.0",
                },
            }
        )
    )

    assert result.nativeToolchain is False, (
        f"optionalDependencies are per-platform prebuilds with fallbacks: {result.reasons}"
    )


def test_an_unreadable_manifest_still_routes_up() -> None:
    """The fail-up policy is unchanged by this fix, and must stay that way.

    Narrowing ``nativeToolchain`` is only safe because "we could not tell" still
    resolves to most-capable. If a future change ever made the unknown path
    default small, an unreachable GitHub would start routing real projects to a
    runtime that cannot build them.
    """
    result = requirements.infer_from_package_json("{not json at all")

    assert result.nativeToolchain is True


@pytest.mark.parametrize(
    "package",
    ["pg", "mysql2", "mongodb", "mongoose", "ioredis", "better-sqlite3", "amqplib", "tedious"],
)
def test_raw_socket_dependency_raises_the_flag(package: str) -> None:
    result = requirements.infer_from_package_json(json.dumps({"dependencies": {package: "^1"}}))

    assert result.rawSockets is True
    assert any(package in reason for reason in result.reasons), (
        f"the reason must name the evidence, got: {result.reasons}"
    )
    _assert_every_true_flag_has_a_reason(result)


def test_raw_socket_dev_dependency_also_counts() -> None:
    # A build step or test suite hits the same driver the app would.
    result = requirements.infer_from_package_json(json.dumps({"devDependencies": {"pg": "^8"}}))

    assert result.rawSockets is True
    assert any("devDependencies" in reason for reason in result.reasons)


def test_unparseable_manifest_defaults_to_most_capable() -> None:
    result = requirements.infer_from_package_json("{not json at all")

    assert (result.install, result.nativeToolchain, result.rawSockets) == (True, True, True)
    _assert_every_true_flag_has_a_reason(result)


def test_manifest_that_is_not_an_object_defaults_to_most_capable() -> None:
    result = requirements.infer_from_package_json("[1, 2, 3]")

    assert (result.install, result.nativeToolchain, result.rawSockets) == (True, True, True)


# ---------------------------------------------------------------------------
# End to end — the network half, and the unknown-routes-up rule.
# ---------------------------------------------------------------------------


async def test_resolves_from_fetched_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, _manifest(dependencies={"redis": "^4"})))

    result = await requirements.resolve_requirements("ws-1", "u-1", "octocat/Hello-World")

    assert result.rawSockets is True
    _assert_every_true_flag_has_a_reason(result)


async def test_missing_package_json_defaults_to_most_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not Node-shaped. We do not know what it needs, so we assume everything.
    _patch_client(monkeypatch, _FakeResponse(404))

    result = await requirements.resolve_requirements("ws-1", "u-1", "octocat/Hello-World")

    assert (result.install, result.nativeToolchain, result.rawSockets) == (True, True, True)
    _assert_every_true_flag_has_a_reason(result)


async def test_unreachable_github_defaults_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Failing to INSPECT a project must never block OPENING it.
    import httpx

    _patch_client(monkeypatch, httpx.ConnectError("boom"))

    result = await requirements.resolve_requirements("ws-1", "u-1", "octocat/Hello-World")

    assert (result.install, result.nativeToolchain, result.rawSockets) == (True, True, True)
    _assert_every_true_flag_has_a_reason(result)


async def test_oversized_manifest_is_ignored_not_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge = b"x" * (requirements._MAX_MANIFEST_BYTES + 1)
    _patch_client(monkeypatch, _FakeResponse(200, huge))

    result = await requirements.resolve_requirements("ws-1", "u-1", "octocat/Hello-World")

    assert (result.install, result.nativeToolchain, result.rawSockets) == (True, True, True)


# ---------------------------------------------------------------------------
# Efficiency + SSRF — how it fetches, not just what it returns.
# ---------------------------------------------------------------------------


async def test_fetches_only_package_json_not_the_zip_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old path booted a VM and seeded a repo to learn this. Don't regress."""
    _patch_client(monkeypatch, _FakeResponse(200, _manifest(dependencies={})))

    await requirements.resolve_requirements("ws-1", "u-1", "octocat/Hello-World")

    assert _FakeClient.last_url == (
        "https://api.github.com/repos/octocat/Hello-World/contents/package.json"
    )
    assert "zipball" not in (_FakeClient.last_url or "")


async def test_ref_travels_as_a_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, _manifest(dependencies={})))

    await requirements.resolve_requirements("ws-1", "u-1", "octocat/Hello-World", "main")

    assert _FakeClient.last_params == {"ref": "main"}
    # It must not have been spliced into the path.
    assert "main" not in (_FakeClient.last_url or "")


@pytest.mark.parametrize(
    "repo",
    [
        "https://evil.com/octocat/Hello-World",
        "http://127.0.0.1:8080/a/b",
        "../../etc/passwd",
        "not a repo",
        "",
    ],
)
async def test_non_github_repo_is_refused_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch, repo: str
) -> None:
    """The SSRF boundary is shared with archive.py and must refuse loudly."""
    _patch_client(monkeypatch, _FakeResponse(200, _manifest()))

    with pytest.raises(ValidationError):
        await requirements.resolve_requirements("ws-1", "u-1", repo)

    assert _FakeClient.last_url is None, "refused input must never reach httpx"


@pytest.mark.parametrize("ref", ["../../etc", "feature/../..", "a" * 300])
async def test_bad_ref_is_refused(monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, _manifest()))

    with pytest.raises(ValidationError):
        await requirements.resolve_requirements("ws-1", "u-1", "octocat/Hello-World", ref)


# ---------------------------------------------------------------------------
# Per-runtime credential broker.
# ---------------------------------------------------------------------------


async def test_browserpod_runtime_id_uses_the_existing_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSERPOD_API_KEY", "bp_live_key_123")

    result = await runtimes.get_runtime_credentials("browserpod", "ws-1", "u-1")

    assert result.available is True
    assert result.apiKey == "bp_live_key_123"


async def test_unknown_runtime_reports_unavailable_not_404() -> None:
    """An unconfigured runtime and an unknown one look identical to a client.

    Both mean "route this project somewhere else", so returning 404 for one and
    200 for the other would only force callers to write two branches that
    converge on the same fallback.
    """
    result = await runtimes.get_runtime_credentials("webcontainers", "ws-1", "u-1")

    assert result.available is False
    assert result.apiKey is None


# ---------------------------------------------------------------------------
# The manifest cache. Unauthenticated GitHub allows 60 requests/hour per egress
# IP, shared by every tenant, so a probe that runs on every project open must
# not pay the network for a repo it already looked at.
# ---------------------------------------------------------------------------


async def test_second_probe_for_the_same_repo_skips_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, _manifest(dependencies={"pg": "^8"})))

    first = await requirements.resolve_requirements("ws-1", "u-1", "acme/app")
    _FakeClient.last_url = None
    second = await requirements.resolve_requirements("ws-1", "u-1", "acme/app")

    assert _FakeClient.last_url is None, "the second probe hit the network"
    assert second.rawSockets is True
    assert second.reasons == first.reasons


async def test_a_different_ref_is_a_different_cache_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ref names a different tree, so it must not reuse another ref's answer."""
    _patch_client(monkeypatch, _FakeResponse(200, _manifest(dependencies={"pg": "^8"})))

    await requirements.resolve_requirements("ws-1", "u-1", "acme/app", "main")
    _FakeClient.last_url = None
    await requirements.resolve_requirements("ws-1", "u-1", "acme/app", "next")

    assert _FakeClient.last_url is not None, "a new ref reused the cached answer"


async def test_a_transient_failure_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GitHub outage must not pin a defaulted verdict for the whole TTL.

    Only answers GitHub actually gave us are cached. Caching a 5xx would mean one
    bad minute downgrades every open of that repo for the next fifteen.
    """
    _patch_client(monkeypatch, _FakeResponse(503, b""))
    degraded = await requirements.resolve_requirements("ws-1", "u-1", "acme/app")
    assert degraded.rawSockets is True  # most-capable default

    _patch_client(monkeypatch, _FakeResponse(200, _manifest(dependencies={})))
    recovered = await requirements.resolve_requirements("ws-1", "u-1", "acme/app")

    assert _FakeClient.last_url is not None, "a transient failure was cached"
    assert recovered.rawSockets is False
