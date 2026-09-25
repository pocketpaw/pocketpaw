# tests/ee/sites/test_verify_jobs.py — PP-2's worker-side verify work and the service
# seams around it. Created 2026-09-24 (PP-2, feat/sites-verify-pipeline).
#
#   * the preview job runs the browser harness in the SAME sandbox after a clean build,
#     keeps the artifact when the browser fails, parses + scrubs build diagnostics, and
#     writes its report to the verify store under the content hash;
#   * the html verify job skips the build and browser-checks the generated tree;
#   * a structured generator refusal names its code in the ``scaffold_failed`` rung on
#     both lanes (PP-4's leftover gap) and its message reaches the agent diagnostics;
#   * ``run_static_check`` maps every CLI outcome (contract §3);
#   * the background pre-warm does not queue a legacy build-shell pocket (PP-4 gap);
#   * a dynamic svelte site refuses npm packages at declaration time (item 6).
from __future__ import annotations

import json
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import browser_check as bc
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import generator_client as gc
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites import verify_store

from tests.ee.sites.faults import FaultyDaytonaClient, ok_sentinel
from tests.ee.sites.test_build_job import FakeRunner
from tests.ee.sites.test_preview_build_lane import (
    MemoryArtifactStore,
    _preview_input,
    _sandbox,
)
from tests.ee.sites.test_verify_pipeline import MemoryVerifyStore

_KEY = "site_key_" + "Z9y8X7w6V5u4T3s2R1q0P9o8"


class HarnessStub:
    def __init__(self, result: bc.BrowserCheckResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def __call__(self, client: Any, sandbox_id: str, *, static_dir: str):
        self.calls.append(static_dir)
        return self.result


async def _preview(**kw: Any) -> dict[str, Any]:
    return await bj.run_site_preview_build(
        {},
        kw.pop("pocket_id", "pk1"),
        kw.pop("content_hash", "c0ffee"),
        _preview_input(),
        "react",
        600,
        _runner=kw.pop("runner", FakeRunner()),
        _client=kw.pop("client", _sandbox()),
        _store=kw.pop("store", MemoryArtifactStore()),
        **kw,
    )


class TestPreviewJobVerifies:
    async def test_a_clean_build_is_browser_checked_in_the_same_sandbox(self) -> None:
        harness = HarnessStub(bc.BrowserCheckResult("passed", browser="chromium 140"))
        vstore, store = MemoryVerifyStore(), MemoryArtifactStore()
        result = await _preview(_harness=harness, _verify_store=vstore, store=store)

        assert result["status"] == "built"
        assert harness.calls == ["/home/daytona/paw-build/dist"]
        assert result["layers"]["build"] == {"status": "passed"}
        assert result["layers"]["browser"]["status"] == "passed"
        assert vstore.read("pk1", verify_store.sandbox_key("c0ffee"))["layers"] == result["layers"]
        assert store.writes == 1

    async def test_a_browser_failure_keeps_the_preview_and_reports_it(self) -> None:
        harness = HarnessStub(
            bc.BrowserCheckResult(
                "failed",
                "pageerror",
                [{"layer": "browser", "code": "pageerror", "file": "index.html", "message": _KEY}],
            )
        )
        store = MemoryArtifactStore()
        result = await _preview(_harness=harness, _verify_store=MemoryVerifyStore(), store=store)
        assert result["status"] == "built", "the page compiled; the editor still shows it"
        assert store.writes == 1
        assert result["layers"]["browser"]["status"] == "failed"
        assert _KEY not in json.dumps(result), "diagnostics must be scrubbed before they leave"

    async def test_a_failed_build_carries_parsed_scrubbed_diagnostics(self) -> None:
        tail = f"/home/daytona/paw-build/src/App.tsx:3:7 error: Unexpected token\nleaked {_KEY}\n"
        client = _sandbox(sentinel=ok_sentinel(build_exit=1, stderr_tail=tail))
        harness = HarnessStub(bc.BrowserCheckResult("passed"))
        result = await _preview(client=client, _harness=harness, _verify_store=MemoryVerifyStore())

        assert result["status"] == "failed"
        assert result["reason"].startswith("build_failed:"), "the rung stays a rung"
        assert result["layers"]["build"]["status"] == "failed"
        assert result["layers"]["browser"]["status"] == "skipped"
        assert harness.calls == [], "a failed build is never browser-checked"
        errors = result["diagnostics"]["errors"]
        hit = next(e for e in errors if e.get("line") == 3)
        assert hit["file"] == "src/App.tsx" and hit["col"] == 7
        text = json.dumps(result)
        assert _KEY not in text and "/home/daytona" not in text

    async def test_a_lost_sandbox_is_unverified_not_failed(self) -> None:
        client = FaultyDaytonaClient(fail_at="exec", sentinel=None)
        result = await _preview(client=client, _verify_store=MemoryVerifyStore())
        assert result["layers"]["build"]["status"] == "unverified"
        assert result["layers"]["browser"]["status"] == "unverified"

    async def test_the_ui_outcome_reader_still_sees_only_status_and_reason(self) -> None:
        """``_preview_job_outcome`` is the UI path: it must keep reading the rung only."""

        class _Info:
            success = True
            result = {
                "status": "failed",
                "reason": "build_failed:x",
                "diagnostics": {"errors": [1]},
            }

        class _Job:
            def __init__(self, *_a: Any, **_k: Any) -> None: ...

            async def status(self):
                from arq.jobs import JobStatus

                return JobStatus.complete

            async def result_info(self):
                return _Info()

        import pocketpaw_ee.sites.build_job as mod

        original = mod.Job
        mod.Job = _Job  # type: ignore[assignment]
        try:
            outcome = await bj._preview_job_outcome(object(), "id")
        finally:
            mod.Job = original  # type: ignore[assignment]
        assert outcome == ("failed", "build_failed:x")


class TestStructuredRefusals:
    async def test_the_preview_lane_names_the_refusal_code(self) -> None:
        refusal = gc.GeneratorRefused("reserved_path", 'generator-owned path "vite.config.ts"')
        result = await _preview(
            runner=FakeRunner(raises=refusal), _verify_store=MemoryVerifyStore()
        )
        assert result["reason"] == "scaffold_failed:reserved_path"
        entry = result["diagnostics"]["errors"][0]
        assert entry["layer"] == "static" and entry["code"] == "reserved_path"
        assert "vite.config.ts" in entry["message"]

    async def test_an_unstructured_raise_keeps_the_generic_cause(self) -> None:
        result = await _preview(
            runner=FakeRunner(raises=RuntimeError("stderr secret")),
            _verify_store=MemoryVerifyStore(),
        )
        assert result["reason"] == "scaffold_failed:generator_raised"
        assert "stderr secret" not in json.dumps(result)

    def test_only_closed_set_codes_reach_the_rung(self) -> None:
        assert bj.scaffold_failure_cause(gc.GeneratorRefused("dependency_policy", "m")) == (
            "dependency_policy"
        )
        assert bj.scaffold_failure_cause(gc.GeneratorRefused("made_up", "m")) == "generator_raised"

    async def test_the_publish_lane_records_the_code(self, beanie_test_db) -> None:
        from tests.ee.sites.test_build_job import _insert_site, _reread, _run_job

        site = await _insert_site()
        refusal = gc.GeneratorRefused("dependency_policy", "three@latest is not exact")
        await _run_job(site, runner=FakeRunner(raises=refusal), client=FaultyDaytonaClient())
        fresh = await _reread(site)
        assert fresh.build_reason == "scaffold_failed:dependency_policy"
        assert "three@latest" not in (fresh.build_reason or "")


class TestHtmlVerifyJob:
    async def test_it_skips_the_build_and_checks_the_tree(self) -> None:
        seen: dict[str, Any] = {}

        async def standalone(files: dict[str, Any], *, static_rel: str, client: Any):
            seen["files"] = sorted(files)
            seen["static_rel"] = static_rel
            return bc.BrowserCheckResult("passed")

        vstore = MemoryVerifyStore()
        result = await bj.run_site_html_verify(
            {},
            "pk1",
            "h1",
            {"engine": "html", "source": {"index.html": "<h1>x</h1>"}, "siteConfig": {}},
            _runner=FakeRunner(tree={"index.html": b"<h1>x</h1>"}),
            _harness=standalone,
            _verify_store=vstore,
        )
        assert result["layers"]["build"]["status"] == "skipped"
        assert result["layers"]["browser"]["status"] == "passed"
        assert seen["files"] == ["index.html"]
        assert vstore.read("pk1", verify_store.sandbox_key("h1")) is not None

    async def test_no_sandbox_re_raises(self) -> None:
        async def standalone(*_a: Any, **_k: Any):
            raise RuntimeError("daytona down")

        with pytest.raises(RuntimeError):
            await bj.run_site_html_verify(
                {},
                "pk1",
                "h1",
                {"engine": "html", "source": {}, "siteConfig": {}},
                _runner=FakeRunner(tree={"index.html": b"x"}),
                _harness=standalone,
                _verify_store=MemoryVerifyStore(),
            )

    def test_it_is_registered_on_the_sites_lane(self, monkeypatch) -> None:
        monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")
        from pocketpaw_ee.sites import build_worker

        names = {f.name: f for f in build_worker.WorkerSettings.functions if hasattr(f, "name")}
        fn = names[bj.HTML_VERIFY_ARQ_FUNCTION_NAME]
        assert fn.coroutine is bj.run_site_html_verify
        assert fn.timeout_s == bj.site_preview_job_timeout_seconds()


class _Proc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self._stdout = stdout.encode()

    async def communicate(self):
        return self._stdout, b""

    async def wait(self):
        return self.returncode


def _exec(returncode: int, stdout: str):
    async def run(*argv: Any, **_kw: Any):
        assert "check" in argv
        return _Proc(returncode, stdout)

    return run


class TestStaticCheckRunner:
    async def test_a_ran_check_returns_its_report(self) -> None:
        report = {
            "ok": False,
            "errors": [{"layer": "static", "code": "compile_error"}],
            "warnings": [],
        }
        assert await gc.run_static_check({}, _exec=_exec(0, json.dumps(report))) == report

    async def test_an_author_fixable_refusal_is_a_failed_report(self) -> None:
        line = json.dumps({"error": "bad dep", "code": "dependency_policy"})
        report = await gc.run_static_check({}, _exec=_exec(1, line))
        assert report["ok"] is False
        assert report["errors"][0] == {
            "layer": "static",
            "code": "dependency_policy",
            "message": "bad dep",
        }

    @pytest.mark.parametrize(
        ("code", "stdout", "reason"),
        [
            (1, json.dumps({"error": "x", "code": "internal_error"}), "check_crashed"),
            (1, "", "check_crashed"),
            (2, "usage", "checker_unavailable"),
            (0, "not json", "check_output_unreadable"),
        ],
    )
    async def test_a_check_that_did_not_run_raises(
        self, code: int, stdout: str, reason: str
    ) -> None:
        with pytest.raises(gc.StaticCheckUnavailable) as info:
            await gc.run_static_check({}, _exec=_exec(code, stdout))
        assert info.value.reason == reason

    async def test_no_generator_binary_is_checker_unavailable(self) -> None:
        async def missing(*_a: Any, **_k: Any):
            raise FileNotFoundError("paw-sites-gen")

        with pytest.raises(gc.StaticCheckUnavailable) as info:
            await gc.run_static_check({}, _exec=missing)
        assert info.value.reason == "checker_unavailable"


_SVELTE = {
    "src/routes/+page.svelte": "<h1>x</h1>",
    "src/routes/+layout.svelte": "<slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": "",
    "src/lib/components/Hero.svelte": "<h1>x</h1>",
}


async def _pocket(source: dict[str, Any], pattern: str = "landing") -> str:
    _v, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="S",
        type_="site",
        pattern=pattern,
        ripple_spec=None,
        engine="svelte",
        source=dict(source),
        trusted=True,
    )
    assert err is None
    return pocket_id


class TestPrewarmPreflight:
    async def test_a_legacy_build_shell_pocket_is_not_queued(self, beanie_test_db) -> None:
        from tests.ee.sites.test_legacy_build_shell import SVELTE_VITE

        pocket_id = await _pocket({**_SVELTE, "vite.config.ts": SVELTE_VITE})

        class _Pool:
            calls = 0

            async def enqueue_job(self, *_a: Any, **_k: Any):
                _Pool.calls += 1
                return object()

        await sites_service._prewarm_native_artifact(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _store=MemoryArtifactStore(),
            _pool=_Pool(),
        )
        assert _Pool.calls == 0, "a build the generator will refuse must not spend a sandbox"

    async def test_a_clean_pocket_is_still_queued(self, beanie_test_db) -> None:
        pocket_id = await _pocket(_SVELTE)

        class _Pool:
            calls = 0

            async def enqueue_job(self, *_a: Any, **_k: Any):
                _Pool.calls += 1
                return object()

        await sites_service._prewarm_native_artifact(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _store=MemoryArtifactStore(),
            _pool=_Pool(),
        )
        assert _Pool.calls == 1


class TestDynamicSvelteRefusesPackages:
    async def test_declaration_is_refused_with_engine_unsupported(self, beanie_test_db) -> None:
        source = {**_SVELTE, "actions": [{"name": "add", "object": "t"}]}
        pocket_id = await _pocket(source, pattern="dynamic")

        async def _never(*_a: Any, **_k: Any):
            raise AssertionError("the resolver must not run for a dynamic site")

        result = await sites_service.set_site_dependencies(
            user_id="u1", pocket_id=pocket_id, add=[{"name": "three"}], _resolve=_never
        )
        assert result["changed"] is False
        assert result["rejected"] == [
            {
                "name": "three",
                "code": "engine_unsupported",
                "reason": sites_service.DYNAMIC_PACKAGES_REASON,
            }
        ]
        wire = await pockets_service.get(pocket_id, "u1")
        assert "paw.dependencies.json" not in wire["source"]

    async def test_pattern_alone_is_enough(self, beanie_test_db) -> None:
        pocket_id = await _pocket(_SVELTE, pattern="dynamic")
        wire = await pockets_service.get(pocket_id, "u1")
        assert sites_service.site_refuses_author_packages(wire)

    async def test_a_static_svelte_site_is_not_refused(self, beanie_test_db) -> None:
        pocket_id = await _pocket(_SVELTE)
        wire = await pockets_service.get(pocket_id, "u1")
        assert not sites_service.site_refuses_author_packages(wire)

    async def test_create_with_bindings_drops_the_packages(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        source = {**_SVELTE, "sources": [{"name": "r", "kind": "data", "object": "t"}]}
        out, report = await mcp._resolve_create_dependencies("svelte", [{"name": "three"}], source)
        assert "paw.dependencies.json" not in out
        assert report["rejected"][0]["code"] == "engine_unsupported"
