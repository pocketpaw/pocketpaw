# test_websandbox_archive.py — tests for the repo-archive endpoint (BP-3b).
#
# This endpoint takes a repo reference from the caller and fetches it server-side,
# so the parsing is a security boundary, not a convenience: anything that is not
# a github.com owner/repo must be refused BEFORE any URL is built. These tests
# pin that, plus the size cap and the error mapping.
from __future__ import annotations

import re

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.cloud.websandbox import archive


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class _FakeClient:
    """Records the URL fetched so tests can assert we never fetch caller input."""

    last_url: str | None = None

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, headers=None):
        type(self).last_url = url
        return self._response


@pytest.fixture(autouse=True)
def _reset():
    _FakeClient.last_url = None


def _patch_client(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> None:
    monkeypatch.setattr(archive.httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))


# ---------------------------------------------------------------------------
# Repo parsing — the SSRF boundary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        "octocat/Hello-World",
        "https://github.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World.git",
        "http://www.github.com/octocat/Hello-World",
        "github.com/octocat/Hello-World",
    ],
)
async def test_accepts_github_forms(monkeypatch: pytest.MonkeyPatch, repo: str) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, b"zipbytes"))

    result = await archive.fetch_repo_archive("ws", "user", repo)

    assert result == b"zipbytes"
    assert _FakeClient.last_url == "https://api.github.com/repos/octocat/Hello-World/zipball"


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "   ",
        "not-a-repo",
        "https://evil.com/octocat/Hello-World",
        "https://github.com.evil.com/a/b",
        "http://127.0.0.1:8888/admin",
        "file:///etc/passwd",
        "https://user:pass@github.com/a/b",
        "octocat/Hello-World/../../etc",
        "https://github.com/octocat",
    ],
)
async def test_refuses_anything_not_a_github_repo(repo: str) -> None:
    # Must fail on PARSING, before any URL is constructed or fetched.
    with pytest.raises((ValidationError, CloudError)):
        await archive.fetch_repo_archive("ws", "user", repo)
    assert _FakeClient.last_url is None


async def test_builds_url_from_parsed_parts_not_caller_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, b"z"))

    await archive.fetch_repo_archive("ws", "user", "https://github.com/octocat/Hello-World.git")

    # The fetched URL is assembled by us and contains no caller-supplied scheme
    # or host — the whole point of the boundary. (Assert on the PATH: ".git"
    # appears in "api.github.com" itself, so a naive substring check is a false
    # positive.)
    assert _FakeClient.last_url == "https://api.github.com/repos/octocat/Hello-World/zipball"


# ---------------------------------------------------------------------------
# Refs.
# ---------------------------------------------------------------------------


async def test_ref_is_appended_when_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, b"z"))

    await archive.fetch_repo_archive("ws", "user", "octocat/Hello-World", "main")

    assert _FakeClient.last_url.endswith("/zipball/main")


@pytest.mark.parametrize("ref", ["../../etc", "a b", "main;rm -rf /", "x" * 300])
async def test_rejects_unsafe_refs(monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    _patch_client(monkeypatch, _FakeResponse(200, b"z"))

    with pytest.raises(ValidationError):
        await archive.fetch_repo_archive("ws", "user", "octocat/Hello-World", ref)


# ---------------------------------------------------------------------------
# Upstream failures.
# ---------------------------------------------------------------------------


async def test_404_explains_private_repos_are_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _FakeResponse(404))

    with pytest.raises(CloudError) as excinfo:
        await archive.fetch_repo_archive("ws", "user", "octocat/Nope")

    assert "rivate" in str(excinfo.value) or excinfo.value.status_code == 404


async def test_upstream_error_is_a_clean_502(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeResponse(500))

    with pytest.raises(CloudError):
        await archive.fetch_repo_archive("ws", "user", "octocat/Hello-World")


async def test_oversized_archive_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pod is a browser tab; a giant archive would blow its memory.
    big = b"x" * (archive._MAX_ARCHIVE_BYTES + 1)
    _patch_client(monkeypatch, _FakeResponse(200, big))

    with pytest.raises(CloudError):
        await archive.fetch_repo_archive("ws", "user", "octocat/Hello-World")


def test_owner_regex_is_anchored() -> None:
    # A non-anchored pattern would let "evil.com/github.com/a/b" through.
    assert archive._SHORTHAND.pattern.startswith("^")
    assert archive._SHORTHAND.pattern.endswith("$")
    assert re.match(archive._URL_FORM, "https://evil.com/github.com/a/b") is None
