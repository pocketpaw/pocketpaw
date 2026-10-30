# tests/ee/sites/test_browser_check.py — the browser layer's sandbox half
# (pocketpaw_ee.sites.browser_check) and the run_build ``after_build`` / ``image`` seams
# it rides. Created 2026-09-24 (PP-2).
#
# Pinned: every harness exit code maps to the contract §4 verdict (3 and a stalled
# launch are ``unverified`` / ``browser_unavailable`` — never passed); the in-sandbox
# script installs from the frozen lockfile and only then tries a chromium install;
# the standalone (html) path always deletes its sandbox; ``run_build`` passes ``image``
# only when set, runs ``after_build`` only on a clean build, and a raising hook cannot
# cost the build its artifact.
from __future__ import annotations

import json
from typing import Any

import pytest
from pocketpaw_ee.sites import browser_check as bc
from pocketpaw_ee.sites import daytona_runner as dr

from tests.ee.sites.faults import FaultyDaytonaClient, ok_sentinel

_OK = json.dumps(
    {
        "ok": True,
        "pages": [{"path": "index.html", "errors": []}],
        "harness": {"version": 1, "browser": "chromium 140"},
    }
)
_FAIL = json.dumps(
    {
        "ok": False,
        "pages": [{"path": "index.html", "errors": [{"kind": "pageerror", "message": "boom"}]}],
        "harness": {"version": 1},
    }
)


class TestParse:
    def test_ok(self) -> None:
        result = bc.parse_harness_output(0, _OK)
        assert result.status == "passed"
        assert result.browser == "chromium 140"

    def test_errors_fail(self) -> None:
        result = bc.parse_harness_output(0, _FAIL)
        assert result.status == "failed"
        assert result.reason == "pageerror"
        assert result.errors[0]["file"] == "index.html"

    @pytest.mark.parametrize(
        ("code", "stdout", "reason"),
        [
            (3, '{"error":"browser_unavailable"}', "browser_unavailable"),
            (124, "", "timeout"),
            (10, "", "harness_install_failed"),
            (11, "", "harness_unavailable"),
            (1, '{"error":"no .html pages found under build"}', "no_static_pages"),
            (1, '{"error":"kaboom"}', "harness_crashed"),
            (None, "", "harness_lost"),
            (-1, "", "harness_lost"),
            (0, "not json", "harness_output_unreadable"),
        ],
    )
    def test_everything_else_is_unverified(self, code: Any, stdout: str, reason: str) -> None:
        result = bc.parse_harness_output(code, stdout)
        assert result.status == "unverified"
        assert result.reason == reason


class TestScript:
    def test_the_harness_installs_frozen_and_retries_after_a_browser_install(self) -> None:
        script = bc.browser_check_script("/home/daytona/paw-build/build")
        assert "bun install --frozen-lockfile" in script
        assert "--dir /home/daytona/paw-build/build" in script
        assert "install --with-deps chromium" in script
        # The chromium install only runs after a browser_unavailable (exit 3).
        assert script.index("-eq 3") < script.index("install --with-deps chromium")
        assert script.rstrip().endswith("exit 0")


class _HarnessClient(FaultyDaytonaClient):
    """A Daytona fake that also answers the harness's result files."""

    def __init__(self, *, harness_exit: str = "0", harness_out: str = _OK, **kw: Any) -> None:
        super().__init__(**kw)
        self.harness_exit = harness_exit
        self.harness_out = harness_out
        self.commands: list[str] = []

    async def execute_command(self, sandbox_id: str, command: str, timeout: int = 30) -> Any:
        self.commands.append(command)
        return await super().execute_command(sandbox_id, command, timeout)

    async def download_file(self, sandbox_id: str, remote_path: str) -> bytes:
        if remote_path == bc.SANDBOX_EXIT_PATH:
            return self.harness_exit.encode()
        if remote_path == bc.SANDBOX_RESULT_PATH:
            return self.harness_out.encode()
        return await super().download_file(sandbox_id, remote_path)


_FILES = {"browser-check.mjs": b"//", "package.json": b"{}", "bun.lock": b"", "bunfig.toml": b""}


class TestInSandbox:
    async def test_uploads_the_harness_and_reads_the_verdict(self) -> None:
        client = _HarnessClient()
        result = await bc.run_in_sandbox(client, "sb", static_dir="/x/build", files=_FILES)
        assert result.status == "passed"
        uploaded = {dst for _c, dst in client.uploaded}
        assert f"{bc.SANDBOX_HARNESS_DIR}/browser-check.mjs" in uploaded
        assert bc.SANDBOX_SCRIPT_PATH in uploaded

    async def test_no_vendored_harness_is_unverified(self, monkeypatch) -> None:
        monkeypatch.setenv(bc.HARNESS_DIR_ENV, "/definitely/not/here")
        result = await bc.run_in_sandbox(_HarnessClient(), "sb", static_dir="/x")
        assert (result.status, result.reason) == ("unverified", "harness_unavailable")

    async def test_browser_unavailable_is_unverified(self) -> None:
        client = _HarnessClient(harness_exit="3", harness_out='{"error":"browser_unavailable"}')
        result = await bc.run_in_sandbox(client, "sb", static_dir="/x", files=_FILES)
        assert (result.status, result.reason) == ("unverified", "browser_unavailable")

    async def test_the_standalone_path_always_deletes_its_sandbox(self) -> None:
        client = _HarnessClient()
        result = await bc.run_standalone(
            {"index.html": "<h1>x</h1>"}, client=client, harness=_FILES
        )
        assert result.status == "passed"
        assert client.calls[-1] == "delete"

    async def test_the_standalone_path_raises_when_no_sandbox_can_be_made(self) -> None:
        client = _HarnessClient(fail_at="create")
        with pytest.raises(Exception):
            await bc.run_standalone({"index.html": "x"}, client=client, harness=_FILES)

    async def test_the_verify_image_is_used_when_configured(self, monkeypatch) -> None:
        monkeypatch.setenv(bc.VERIFY_IMAGE_ENV, "paw/verify:1.62.1")
        client = _HarnessClient()
        await bc.run_standalone({"index.html": "x"}, client=client, harness=_FILES)
        assert client.create_kwargs["image"] == "paw/verify:1.62.1"


class TestRunBuildSeams:
    async def test_image_is_passed_only_when_set(self) -> None:
        client = FaultyDaytonaClient()
        await dr.run_build({"a": "b"}, engine="react", timeout_seconds=60, client=client)
        assert "image" not in client.create_kwargs
        client = FaultyDaytonaClient()
        await dr.run_build(
            {"a": "b"}, engine="react", timeout_seconds=60, client=client, image="img:1"
        )
        assert client.create_kwargs["image"] == "img:1"

    async def test_after_build_runs_before_teardown_on_a_clean_build(self) -> None:
        client = FaultyDaytonaClient()
        seen: list[tuple[str, list[str]]] = []

        async def hook(c: Any, sandbox_id: str, static_dir: str) -> str:
            seen.append((static_dir, list(client.calls)))
            return "checked"

        result = await dr.run_build(
            {"a": "b"}, engine="react", timeout_seconds=60, client=client, after_build=hook
        )
        assert result.post_build == "checked"
        static_dir, calls_then = seen[0]
        assert static_dir == f"{dr.SANDBOX_PROJECT_DIR}/dist"
        assert "delete" not in calls_then, "the hook must run while the sandbox is alive"

    async def test_after_build_never_runs_on_a_failed_build(self) -> None:
        client = FaultyDaytonaClient(sentinel=ok_sentinel(build_exit=1))
        called: list[bool] = []

        async def hook(*_a: Any) -> None:
            called.append(True)

        result = await dr.run_build(
            {"a": "b"}, engine="react", timeout_seconds=60, client=client, after_build=hook
        )
        assert called == []
        assert result.post_build is None

    async def test_a_raising_hook_cannot_cost_the_build_its_artifact(self) -> None:
        async def hook(*_a: Any) -> None:
            raise RuntimeError("harness blew up")

        result = await dr.run_build(
            {"a": "b"},
            engine="react",
            timeout_seconds=60,
            client=FaultyDaytonaClient(),
            after_build=hook,
        )
        assert result.ok
        assert result.post_build is None
