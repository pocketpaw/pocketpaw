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


def _patch_client(monkeypatch: pytest.MonkeyPatch, response) -> None:
    monkeypatch.setattr(requirements.httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))


def _manifest(**sections) -> bytes:
    return json.dumps(sections).encode()


def _assert_every_true_flag_has_a_reason(result) -> None:
    """The core contract: a raised flag without evidence is a guess."""
    joined = " ".join(result.reasons).lower()
    assert result.reasons, "a verdict with no reasons is unexplainable"
    for flag in ("install", "nativeToolchain", "rawSockets"):
        if getattr(result, flag):
            assert flag.lower() in joined, (
                f"{flag} is true but no reason mentions it: {result.reasons}"
            )


# ---------------------------------------------------------------------------
# Inference from a manifest — the pure half.
# ---------------------------------------------------------------------------


def test_package_json_implies_install_and_native_toolchain() -> None:
    result = requirements.infer_from_package_json(json.dumps({"dependencies": {"express": "^4"}}))

    assert result.install is True
    # An npm install of any non-trivial tree fetches or builds native binaries.
    assert result.nativeToolchain is True
    assert result.rawSockets is False
    _assert_every_true_flag_has_a_reason(result)


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
