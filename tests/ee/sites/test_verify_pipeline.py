# tests/ee/sites/test_verify_pipeline.py — the three-layer verification pipeline
# (pocketpaw_ee.sites.verify). Created 2026-09-24 (PP-2, feat/sites-verify-pipeline).
#
# WHAT IS PINNED, and the mutation each block is meant to catch
# (tests/mutations/sites_verify_pipeline.json):
#   * each layer's pass / fail maps to the verdict per contract §5 — ``passed`` only
#     when every applicable layer passed;
#   * a static failure spends NO sandbox (the pool is never touched);
#   * every infrastructure failure is ``unverified`` with its reason — queue down,
#     job raised (sandbox unavailable), wait timed out, browser unavailable — never
#     ``passed``;
#   * the svelte / react build rides the preview lane with the SAME content hash the
#     editor's ``get_native_artifact`` uses, so one sandbox serves both;
#   * html rides its own verify job and its build layer is ``skipped``;
#   * the verdict is cached per content hash (a re-verify is free) and a source change
#     re-verifies; ``unverified`` is never served from the cache;
#   * a stored sandbox report (a UI pre-warm's) is reused without an enqueue;
#   * ripple is ``unverified`` / ``engine_not_verifiable``; a dynamic svelte site
#     holding packages is a static failure; a legacy build-shell file is a static
#     failure (PP-4) with no sandbox;
#   * ``status_summary`` (the ``/status`` view) carries counts and never a message.
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites import verify, verify_store
from pocketpaw_ee.sites.generator_client import StaticCheckUnavailable

_SVELTE = {
    "src/routes/+page.svelte": "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>",
    "src/routes/+layout.svelte": "<script>import '../app.css'</script><slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": ":root{--brand:#0A84FF}",
    "src/lib/components/Hero.svelte": "<h1>Bright Smile</h1>",
}
_HTML = {"index.html": "<!doctype html><html><head></head><body><h1>Hi</h1></body></html>"}
_MANIFEST = json.dumps(
    {"schema": 1, "packages": {"three": {"version": "0.170.0"}}}, separators=(",", ":")
)


class MemoryVerifyStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict[str, Any]] = {}

    def read(self, pocket_id: str, key: str) -> dict[str, Any] | None:
        return self.data.get((pocket_id, key))

    def write(self, pocket_id: str, key: str, record: dict[str, Any]) -> None:
        self.data[(pocket_id, key)] = json.loads(json.dumps(record))


class Pool:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None, **kw):
        self.calls.append({"function": function, "args": args, "job_id": _job_id})
        if self.error is not None:
            raise self.error
        return object()


def report(
    build: str = "passed",
    browser: str = "passed",
    *,
    errors: list[dict[str, Any]] | None = None,
    build_reason: str = "",
    browser_reason: str = "",
) -> dict[str, Any]:
    b = {"status": build, **({"reason": build_reason} if build_reason else {})}
    w = {"status": browser, **({"reason": browser_reason} if browser_reason else {})}
    return {
        "status": "built" if build == "passed" else "failed",
        "reason": "completed_ok:ok",
        "layers": {"build": b, "browser": w},
        "diagnostics": {"errors": errors or [], "warnings": []},
        "checked_at": "2026-09-24T00:00:00+00:00",
    }


class Waiter:
    def __init__(self, result: Any = None, *, raises: BaseException | None = None) -> None:
        self.result = result if result is not None else report()
        self.raises = raises
        self.calls: list[str] = []

    async def __call__(self, pool: Any, job_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(job_id)
        if self.raises is not None:
            raise self.raises
        return self.result


class Check:
    def __init__(self, result: Any = None, *, raises: BaseException | None = None) -> None:
        self.result = result if result is not None else {"ok": True, "errors": [], "warnings": []}
        self.raises = raises
        self.inputs: list[dict[str, Any]] = []

    async def __call__(self, generator_input: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(generator_input)
        if self.raises is not None:
            raise self.raises
        return self.result


async def _pocket(
    engine: str = "svelte", source: dict[str, Any] | None = None, pattern: str = "landing"
) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bright Smile",
        type_="site",
        pattern=pattern,
        ripple_spec=None,
        engine=engine,
        source=dict(source if source is not None else _SVELTE),
        trusted=True,
    )
    assert err is None, err
    return pocket_id


async def _verify(pocket_id: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("_store", MemoryVerifyStore())
    kw.setdefault("_pool", Pool())
    kw.setdefault("_check", Check())
    kw.setdefault("_wait", Waiter())
    return await verify.verify_site(workspace_id="ws1", user_id="u1", pocket_id=pocket_id, **kw)


def _layer(verdict: dict[str, Any], name: str) -> dict[str, Any]:
    return next(layer for layer in verdict["layers"] if layer["name"] == name)


# ── each layer ────────────────────────────────────────────────────────────────


class TestLayers:
    async def test_all_three_passing_is_passed(self, beanie_test_db) -> None:
        verdict = await _verify(await _pocket())
        assert verdict["status"] == "passed"
        assert [layer["status"] for layer in verdict["layers"]] == ["passed"] * 3
        assert "reason" not in verdict
        assert verdict["errors"] == []

    async def test_a_static_failure_spends_no_sandbox(self, beanie_test_db) -> None:
        pool, waiter = Pool(), Waiter()
        check = Check(
            {
                "ok": False,
                "errors": [
                    {
                        "layer": "static",
                        "file": "src/lib/components/Hero.svelte",
                        "line": 1,
                        "col": 5,
                        "code": "undeclared_package",
                        "message": '"three" is not declared',
                    }
                ],
                "warnings": [],
            }
        )
        verdict = await _verify(await _pocket(), _pool=pool, _wait=waiter, _check=check)
        assert verdict["status"] == "failed"
        assert _layer(verdict, "static")["status"] == "failed"
        assert _layer(verdict, "build")["status"] == "skipped"
        assert _layer(verdict, "browser")["status"] == "skipped"
        assert pool.calls == [] and waiter.calls == [], "a static failure must not build"
        assert verdict["errors"][0]["code"] == "undeclared_package"
        assert verdict["errors"][0]["line"] == 1

    async def test_a_build_failure_is_failed_with_its_diagnostics(self, beanie_test_db) -> None:
        err = {"layer": "build", "code": "build_error", "file": "src/x.svelte", "message": "m"}
        verdict = await _verify(
            await _pocket(),
            _wait=Waiter(
                report("failed", "skipped", errors=[err], build_reason="build_failed:build_failed")
            ),
        )
        assert verdict["status"] == "failed"
        assert _layer(verdict, "build")["status"] == "failed"
        assert verdict["errors"][0]["file"] == "src/x.svelte"

    async def test_a_browser_failure_is_failed(self, beanie_test_db) -> None:
        err = {"layer": "browser", "code": "pageerror", "file": "index.html", "message": "boom"}
        verdict = await _verify(
            await _pocket(), _wait=Waiter(report("passed", "failed", errors=[err]))
        )
        assert verdict["status"] == "failed"
        assert _layer(verdict, "browser")["status"] == "failed"
        assert verdict["errors"][0]["code"] == "pageerror"

    async def test_a_browser_that_would_not_launch_is_unverified_never_passed(
        self, beanie_test_db
    ) -> None:
        verdict = await _verify(
            await _pocket(),
            _wait=Waiter(report("passed", "unverified", browser_reason="browser_unavailable")),
        )
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "browser_unavailable"

    async def test_a_static_check_that_could_not_run_is_unverified(self, beanie_test_db) -> None:
        verdict = await _verify(
            await _pocket(), _check=Check(raises=StaticCheckUnavailable("checker_unavailable"))
        )
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "checker_unavailable"
        # The sandbox layers still ran and passed; the verdict is still not a pass.
        assert _layer(verdict, "build")["status"] == "passed"


# ── infrastructure → unverified ───────────────────────────────────────────────


class TestInfrastructure:
    async def test_arq_down_is_unverified(self, beanie_test_db) -> None:
        verdict = await _verify(await _pocket(), _pool=Pool(error=ConnectionError("redis")))
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "queue_unavailable"

    async def test_a_wait_that_times_out_is_unverified(self, beanie_test_db) -> None:
        verdict = await _verify(await _pocket(), _wait=Waiter(raises=asyncio.TimeoutError()))
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "timeout"

    async def test_a_job_that_raised_is_sandbox_unavailable(self, beanie_test_db) -> None:
        verdict = await _verify(await _pocket(), _wait=Waiter(raises=RuntimeError("no daytona")))
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "sandbox_unavailable"

    async def test_a_lost_build_is_unverified_not_the_authors_fault(self, beanie_test_db) -> None:
        verdict = await _verify(
            await _pocket(),
            _wait=Waiter(
                report(
                    "unverified",
                    "unverified",
                    build_reason="sandbox_unavailable",
                    browser_reason="sandbox_unavailable",
                )
            ),
        )
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "sandbox_unavailable"


# ── lanes ─────────────────────────────────────────────────────────────────────


class TestLanes:
    async def test_svelte_rides_the_preview_lane_under_the_editors_hash(
        self, beanie_test_db
    ) -> None:
        """One sandbox for the editor and the verdict: the job id is the SAME content
        hash ``get_native_artifact`` computes for this pocket."""
        pocket_id = await _pocket()
        pool = Pool()
        verdict = await _verify(pocket_id, _pool=pool)

        assert pool.calls[0]["function"] == bj.PREVIEW_ARQ_FUNCTION_NAME
        from pocketpaw_ee.sites import generator_client

        wire = await pockets_service.get(pocket_id, "u1")
        editor_hash = sites_service._artifact_content_hash(
            source=wire["source"],
            theme={},
            builder_origin=sites_service._builder_origin(),
            gen_version=generator_client.generator_version(),
            engine="svelte",
            keeps_client_bundle=sites_service._resolve_keeps_client_bundle(wire),
        )
        assert verdict["content_hash"] == editor_hash
        assert pool.calls[0]["job_id"] == bj._preview_job_id(pocket_id, editor_hash)

    async def test_the_static_check_reads_the_same_payload_the_build_gets(
        self, beanie_test_db
    ) -> None:
        pocket_id = await _pocket()
        check, pool = Check(), Pool()
        await _verify(pocket_id, _check=check, _pool=pool)
        _pid, _hash, build_input, _engine, _timeout = pool.calls[0]["args"]
        assert check.inputs[0]["source"] == build_input["source"]
        assert check.inputs[0]["siteConfig"].get("captureSignedKey") in ("", None)

    async def test_html_rides_its_own_job_and_skips_the_build(self, beanie_test_db) -> None:
        pool = Pool()
        html_report = report("skipped", "passed", build_reason="no_build_step")
        verdict = await _verify(await _pocket("html", _HTML), _pool=pool, _wait=Waiter(html_report))
        assert pool.calls[0]["function"] == bj.HTML_VERIFY_ARQ_FUNCTION_NAME
        assert _layer(verdict, "build")["status"] == "skipped"
        assert verdict["status"] == "passed"

    async def test_ripple_is_honestly_unverifiable(self, beanie_test_db) -> None:
        _view, pocket_id, _err = await pockets_service.agent_create(
            workspace_id="ws1",
            owner_id="u1",
            name="Landing",
            type_="site",
            pattern="landing",
            ripple_spec={"ui": {"type": "Stack"}},
            trusted=True,
        )
        pool = Pool()
        verdict = await _verify(pocket_id, _pool=pool)
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "engine_not_verifiable"
        assert pool.calls == []

    async def test_a_dynamic_svelte_site_notes_the_worker_shell(self, beanie_test_db) -> None:
        source = {**_SVELTE, "sources": [{"name": "rows", "kind": "data", "object": "t"}]}
        verdict = await _verify(await _pocket(source=source, pattern="dynamic"))
        assert verdict["note"] == verify.WORKER_RENDERED_NOTE

    async def test_a_dynamic_svelte_site_holding_packages_is_a_static_failure(
        self, beanie_test_db
    ) -> None:
        source = {
            **_SVELTE,
            "actions": [{"name": "add", "object": "t"}],
            "paw.dependencies.json": _MANIFEST,
        }
        pool = Pool()
        verdict = await _verify(await _pocket(source=source, pattern="dynamic"), _pool=pool)
        assert verdict["status"] == "failed"
        assert verdict["errors"][0]["code"] == "engine_unsupported"
        assert pool.calls == []

    async def test_a_legacy_build_shell_file_is_a_static_failure_without_a_sandbox(
        self, beanie_test_db
    ) -> None:
        from tests.ee.sites.test_legacy_build_shell import SVELTE_VITE

        pool = Pool()
        verdict = await _verify(
            await _pocket(source={**_SVELTE, "vite.config.ts": SVELTE_VITE}), _pool=pool
        )
        assert verdict["status"] == "failed"
        assert verdict["errors"][0]["code"] == "reserved_path"
        assert pool.calls == []


# ── cache ─────────────────────────────────────────────────────────────────────


class TestCache:
    async def test_unchanged_source_is_a_free_re_verify(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        await _verify(pocket_id, _store=store)
        check, pool = Check(), Pool()
        again = await _verify(pocket_id, _store=store, _check=check, _pool=pool)
        assert again["status"] == "passed"
        assert again.get("cached") is True
        assert check.inputs == [] and pool.calls == []

    async def test_a_source_change_re_verifies(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        first = await _verify(pocket_id, _store=store)
        await pockets_service.set_svelte_source_file(
            pocket_id,
            "u1",
            component_path="src/lib/components/Hero.svelte",
            new_source="<h1>Changed</h1>",
        )
        check, pool = Check(), Pool()
        second = await _verify(pocket_id, _store=store, _check=check, _pool=pool)
        assert second["content_hash"] != first["content_hash"]
        assert check.inputs and pool.calls, "a changed source must be verified again"

    async def test_unverified_is_never_served_from_the_cache(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        await _verify(pocket_id, _store=store, _pool=Pool(error=ConnectionError("down")))
        check = Check()
        again = await _verify(pocket_id, _store=store, _check=check)
        assert again["status"] == "passed"
        assert check.inputs, "an unverified verdict must be retried, not replayed"

    async def test_a_stored_sandbox_report_is_reused_without_an_enqueue(
        self, beanie_test_db
    ) -> None:
        """A UI pre-warm's job wrote its report under the hash — no second sandbox."""
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        wire = await pockets_service.get(pocket_id, "u1")
        inputs = verify.render_inputs(wire)
        store.write(pocket_id, verify_store.sandbox_key(inputs.content_hash), report())
        pool = Pool()
        verdict = await _verify(pocket_id, _store=store, _pool=pool)
        assert verdict["status"] == "passed"
        assert pool.calls == []

    async def test_force_skips_the_verdict_cache(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        await _verify(pocket_id, _store=store)
        check = Check()
        await _verify(pocket_id, _store=store, _check=check, force=True)
        assert check.inputs


# ── /status summary ───────────────────────────────────────────────────────────


class TestStatusSummary:
    async def test_never_verified_is_none(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        summary = await verify.status_summary(
            workspace_id="ws1", pocket_id=pocket_id, _store=MemoryVerifyStore()
        )
        assert summary["status"] == "none"
        assert summary["error_count"] == 0

    async def test_counts_only_and_no_message_ever(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        secret = "site_key_ABCDEFGHIJKLMNOPQRSTUV and /home/daytona/paw-build/src/x.svelte"
        err = {"layer": "browser", "code": "pageerror", "file": "index.html", "message": secret}
        await _verify(
            pocket_id, _store=store, _wait=Waiter(report("passed", "failed", errors=[err]))
        )
        summary = await verify.status_summary(workspace_id="ws1", pocket_id=pocket_id, _store=store)
        assert summary["status"] == "failed"
        assert summary["error_count"] == 1
        assert set(summary) == {"status", "error_count", "checked_at", "content_hash"}
        assert "message" not in json.dumps(summary) and "pageerror" not in json.dumps(summary)

    async def test_an_edit_makes_the_old_verdict_disappear(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        await _verify(pocket_id, _store=store)
        await pockets_service.set_svelte_source_file(
            pocket_id,
            "u1",
            component_path="src/lib/components/Hero.svelte",
            new_source="<h1>Changed</h1>",
        )
        summary = await verify.status_summary(workspace_id="ws1", pocket_id=pocket_id, _store=store)
        assert summary["status"] == "none"

    async def test_an_in_flight_verify_is_pending_and_a_stale_one_is_not(
        self, beanie_test_db
    ) -> None:
        import time

        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        wire = await pockets_service.get(pocket_id, "u1")
        h = verify.render_inputs(wire).content_hash
        record = {
            "pipeline_version": verify.VERIFY_PIPELINE_VERSION,
            "verdict": {"status": "pending", "content_hash": h},
            "started_at": time.time(),
        }
        store.write(pocket_id, verify_store.verdict_key(h), record)
        summary = await verify.status_summary(workspace_id="ws1", pocket_id=pocket_id, _store=store)
        assert summary["status"] == "pending"
        record["started_at"] = time.time() - verify_store.PENDING_STALE_SECONDS - 1
        store.write(pocket_id, verify_store.verdict_key(h), record)
        summary = await verify.status_summary(workspace_id="ws1", pocket_id=pocket_id, _store=store)
        assert summary["status"] == "none"

    async def test_the_status_endpoint_carries_the_summary_without_messages(
        self, beanie_test_db, monkeypatch
    ) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        err = {"layer": "build", "code": "build_error", "message": "SECRET-DIAGNOSTIC"}
        await _verify(
            pocket_id, _store=store, _wait=Waiter(report("failed", "skipped", errors=[err]))
        )
        monkeypatch.setattr(verify_store, "default_verify_store", lambda: store)

        resp = await sites_service.pocket_status(workspace_id="ws1", pocket_id=pocket_id)
        wire = resp.model_dump()
        assert wire["verification"]["status"] == "failed"
        assert wire["verification"]["error_count"] == 1
        assert "SECRET-DIAGNOSTIC" not in json.dumps(wire)

    async def test_the_site_row_never_carries_diagnostics(self, beanie_test_db) -> None:
        """The verify pipeline writes no Site field: a draft Site row read back after a
        failed verify holds no diagnostic text."""
        pocket_id = await _pocket()
        await sites_service.create_draft_site(
            workspace_id="ws1", user_id="u1", pocket_id=pocket_id, name="Bright Smile"
        )
        err = {"layer": "build", "code": "build_error", "message": "SECRET-DIAGNOSTIC"}
        await _verify(pocket_id, _wait=Waiter(report("failed", "skipped", errors=[err])))
        doc = await sites_service._canonical_site_doc("ws1", pocket_id)
        assert doc is not None
        assert "SECRET-DIAGNOSTIC" not in doc.model_dump_json()
