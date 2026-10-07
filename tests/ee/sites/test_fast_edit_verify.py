# tests/ee/sites/test_fast_edit_verify.py — the edit path's verify (static now, sandbox
# later) and the worker-side pieces that make one edit cost one quick build.
#
#   * ``verify.verify_edit``: static check only, the preview build ENQUEUED and never
#     waited on (``status: pending``, ``build: pending``, ``job_id``); a static failure
#     enqueues nothing; html enqueues nothing (browser on demand); a queue that is down
#     is ``unverified``; an already-built hash answers in full;
#   * ``verify.settled_verdict``: ``None`` while the job runs, the full verdict once its
#     report lands, then never again; a lost job reports ``no_report``; an explicit
#     ``verify_site`` of the same source answers it;
#   * ``/status`` settles a pending edit from the job's report;
#   * the preview job stores the draft BEFORE the browser harness runs;
#   * a newly accepted preview job aborts the pocket's previous one, an aborted render is
#     re-queued rather than read as failed, and a waiter on an aborted job reads
#     ``superseded`` instead of being cancelled itself;
#   * ``service.resolve_armed_builder_origin`` is the one origin for every armed hash.
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pocketpaw_ee.sites import browser_check as bc
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites import verify, verify_store

from tests.ee.sites.test_preview_build_lane import MemoryArtifactStore
from tests.ee.sites.test_verify_jobs import HarnessStub, _preview
from tests.ee.sites.test_verify_pipeline import (
    _HTML,
    Check,
    MemoryVerifyStore,
    Pool,
    _pocket,
    report,
)


async def _edit_verify(pocket_id: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("_store", MemoryVerifyStore())
    kw.setdefault("_pool", Pool())
    kw.setdefault("_check", Check())
    return await verify.verify_edit(workspace_id="ws1", user_id="u1", pocket_id=pocket_id, **kw)


class TestVerifyEdit:
    async def test_static_now_build_enqueued_not_waited(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        pool, store = Pool(), MemoryVerifyStore()

        verdict = await _edit_verify(pocket_id, _pool=pool, _store=store)

        assert verdict["status"] == "pending"
        assert (verdict["static"], verdict["build"]) == ("passed", "pending")
        assert [c["function"] for c in pool.calls] == [bj.PREVIEW_ARQ_FUNCTION_NAME]
        assert verdict["job_id"] == pool.calls[0]["job_id"]
        latest = store.read(pocket_id, verify_store.LATEST_KEY)
        assert latest["job_id"] == verdict["job_id"]
        assert latest["content_hash"] == verdict["content_hash"]
        assert latest["surfaced"] is False

    async def test_a_static_failure_enqueues_nothing(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        pool = Pool()
        check = Check({"ok": False, "errors": [{"code": "svelte_compile", "message": "x"}]})

        verdict = await _edit_verify(pocket_id, _pool=pool, _check=check)

        assert verdict["status"] == "failed"
        assert (verdict["static"], verdict["build"]) == ("failed", "skipped")
        assert pool.calls == []

    async def test_html_runs_no_sandbox_on_the_edit_path(self, beanie_test_db) -> None:
        pocket_id = await _pocket("html", _HTML)
        pool = Pool()

        verdict = await _edit_verify(pocket_id, _pool=pool)

        assert pool.calls == []
        layers = {layer["name"]: layer for layer in verdict["layers"]}
        assert layers["build"]["status"] == "skipped"
        assert layers["browser"] == {
            "name": "browser",
            "status": "unverified",
            "reason": verify.HTML_BROWSER_ON_DEMAND,
        }

    async def test_a_dead_queue_is_unverified(self, beanie_test_db) -> None:
        pocket_id = await _pocket()

        verdict = await _edit_verify(pocket_id, _pool=Pool(error=ConnectionError("redis")))

        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "queue_unavailable"

    async def test_an_already_built_hash_answers_in_full(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store, pool = MemoryVerifyStore(), Pool()
        first = await _edit_verify(pocket_id, _store=store, _pool=pool)
        store.write(pocket_id, verify_store.sandbox_key(first["content_hash"]), report())

        again = await _edit_verify(pocket_id, _store=store, _pool=pool)

        assert again["status"] == "passed"
        assert len(pool.calls) == 1, "the second call read the report, it queued nothing"

    def test_a_half_step_is_skipped_and_says_so(self) -> None:
        verdict = verify.half_step_verdict()
        assert verdict["status"] == "skipped"
        assert verdict["reason"] == verify.HALF_STEP_REASON
        assert verdict["static"] == verdict["build"] == "skipped"


class TestSettledVerdict:
    async def test_it_surfaces_the_background_verdict_once(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        pending = await _edit_verify(pocket_id, _store=store)

        assert verify.settled_verdict(pocket_id, _store=store) is None, "job still running"

        errors = [{"layer": "build", "file": "src/App.svelte", "line": 3, "message": "boom"}]
        store.write(
            pocket_id,
            verify_store.sandbox_key(pending["content_hash"]),
            report("failed", "skipped", errors=errors),
        )
        settled = verify.settled_verdict(pocket_id, _store=store)

        assert settled is not None
        assert settled["status"] == "failed"
        assert settled["build"] == "failed"
        assert settled["job_id"] == pending["job_id"]
        assert settled["content_hash"] == pending["content_hash"]
        assert settled["errors"][0]["message"] == "boom"
        assert verify.settled_verdict(pocket_id, _store=store) is None, "reported once"

    async def test_a_lost_job_is_reported_unverified(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        await _edit_verify(pocket_id, _store=store)
        latest = store.read(pocket_id, verify_store.LATEST_KEY)
        latest["enqueued_at"] = time.time() - verify_store.PENDING_STALE_SECONDS - 1
        store.write(pocket_id, verify_store.LATEST_KEY, latest)

        settled = verify.settled_verdict(pocket_id, _store=store)

        assert settled["status"] == "unverified"
        assert settled["reason"] == "no_report"

    async def test_an_explicit_verify_answers_the_edit(self, beanie_test_db) -> None:
        from tests.ee.sites.test_verify_pipeline import Waiter

        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        await _edit_verify(pocket_id, _store=store)
        await verify.verify_site(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _store=store,
            _pool=Pool(),
            _check=Check(),
            _wait=Waiter(),
        )

        assert verify.settled_verdict(pocket_id, _store=store) is None

    def test_no_edit_means_nothing_to_report(self) -> None:
        assert verify.settled_verdict("p-none", _store=MemoryVerifyStore()) is None

    async def test_status_settles_a_pending_edit(self, beanie_test_db) -> None:
        pocket_id = await _pocket()
        store = MemoryVerifyStore()
        pending = await _edit_verify(pocket_id, _store=store)
        assert (await verify.status_summary(workspace_id="ws1", pocket_id=pocket_id, _store=store))[
            "status"
        ] == "pending"

        store.write(pocket_id, verify_store.sandbox_key(pending["content_hash"]), report())
        summary = await verify.status_summary(workspace_id="ws1", pocket_id=pocket_id, _store=store)

        assert summary["status"] == "passed"
        # /status does not count as handing the verdict to the agent.
        assert verify.settled_verdict(pocket_id, _store=store)["status"] == "passed"


class TestPreviewArtifactBeforeBrowser:
    async def test_the_draft_is_stored_before_the_browser_runs(self) -> None:
        store = MemoryArtifactStore()
        seen: list[int] = []

        class _Harness(HarnessStub):
            async def __call__(self, client: Any, sandbox_id: str, *, static_dir: str):
                seen.append(store.writes)
                return await super().__call__(client, sandbox_id, static_dir=static_dir)

        harness = _Harness(bc.BrowserCheckResult("passed"))
        result = await _preview(_harness=harness, _verify_store=MemoryVerifyStore(), store=store)

        assert result["status"] == "built"
        assert seen == [1], "the artifact must be in the store when the browser starts"
        assert store.writes == 1, "stored once, not again after the browser"


class _PointerPool:
    """Just enough of ArqRedis for the supersede pointer."""

    def __init__(self, previous: str | None = None) -> None:
        self.value = previous
        self.deleted: list[str] = []

    async def getset(self, key: str, value: str) -> Any:
        previous, self.value = self.value, value
        return previous.encode() if previous else None

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1


class TestSupersede:
    async def test_a_new_job_aborts_the_previous_one(self, monkeypatch) -> None:
        aborted: list[str] = []

        async def _abort(self, *, timeout=None, poll_delay=0.5):  # noqa: ANN001
            aborted.append(self.job_id)
            raise TimeoutError

        monkeypatch.setattr(bj.Job, "abort", _abort)
        pool = _PointerPool(previous="site-preview-p1-old")

        out = await bj._supersede_previous_job(pool, "p1", "site-preview-p1-new")

        assert out == "site-preview-p1-old"
        assert aborted == ["site-preview-p1-old"]
        assert pool.value == "site-preview-p1-new"

    async def test_the_same_job_is_never_aborted(self, monkeypatch) -> None:
        async def _abort(self, **_kw):  # noqa: ANN001
            raise AssertionError("must not abort")

        monkeypatch.setattr(bj.Job, "abort", _abort)
        assert await bj._supersede_previous_job(_PointerPool("j1"), "p1", "j1") is None
        assert await bj._supersede_previous_job(_PointerPool(None), "p1", "j1") is None

    async def test_an_accepted_enqueue_supersedes_and_a_refused_one_does_not(
        self, monkeypatch
    ) -> None:
        calls: list[tuple[str, str]] = []

        async def _record(pool: Any, pocket_id: str, job_id: str) -> None:
            calls.append((pocket_id, job_id))

        async def _outcome(pool: Any, job_id: str):
            return "building", None

        async def _not_aborted(pool: Any, job_id: str) -> bool:
            return False

        monkeypatch.setattr(bj, "_supersede_previous_job", _record)
        monkeypatch.setattr(bj, "_preview_job_outcome", _outcome)
        monkeypatch.setattr(bj, "_clear_aborted_result", _not_aborted)

        class _Accepts:
            async def enqueue_job(self, *_a: Any, **_k: Any) -> Any:
                return object()

        class _Refuses:
            async def enqueue_job(self, *_a: Any, **_k: Any) -> Any:
                return None

        kw = {"pocket_id": "p1", "content_hash": "h1", "engine": "svelte", "generator_input": {}}
        await bj.enqueue_preview_build(**kw, _pool_override=_Accepts())
        refused = await bj.enqueue_preview_build(**kw, _pool_override=_Refuses())

        assert calls == [("p1", bj._preview_job_id("p1", "h1"))]
        assert refused.status == "building"

    async def test_an_aborted_render_is_queued_again(self, monkeypatch) -> None:
        class _Info:
            success = False
            result = asyncio.CancelledError()

        async def _result_info(self):  # noqa: ANN001
            return _Info()

        monkeypatch.setattr(bj.Job, "result_info", _result_info)

        async def _noop(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(bj, "_supersede_previous_job", _noop)

        class _Pool(_PointerPool):
            def __init__(self) -> None:
                super().__init__()
                self.enqueues = 0

            async def enqueue_job(self, *_a: Any, **_k: Any) -> Any:
                self.enqueues += 1
                return None if self.enqueues == 1 else object()

        pool = _Pool()
        out = await bj.enqueue_preview_build(
            pocket_id="p1",
            content_hash="h1",
            engine="svelte",
            generator_input={},
            _pool_override=pool,
        )

        assert out.status == "queued"
        assert pool.enqueues == 2
        assert pool.deleted == [bj.result_key_prefix + bj._preview_job_id("p1", "h1")]

    async def test_a_waiter_on_an_aborted_job_reads_superseded(self, monkeypatch) -> None:
        async def _result(self, *, timeout=None, poll_delay=0.5):  # noqa: ANN001
            raise asyncio.CancelledError

        monkeypatch.setattr(bj.Job, "result", _result)

        out = await bj.wait_for_preview_result(object(), "j1", timeout=1)

        assert out["reason"] == bj.SUPERSEDED_REASON
        layers = verify._layers_from_report(out, "svelte")
        assert [layer["status"] for layer in layers] == ["unverified", "unverified"]
        assert {layer["reason"] for layer in layers} == {bj.SUPERSEDED_REASON}

    def test_the_sites_worker_honours_aborts(self, monkeypatch) -> None:
        monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")
        from pocketpaw_ee.sites.build_worker import WorkerSettings

        assert WorkerSettings.__dict__["allow_abort_jobs"] is True


class TestOneOrigin:
    async def test_precedence(self, beanie_test_db, monkeypatch) -> None:
        monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://env.example")
        resolve = sites_service.resolve_armed_builder_origin

        # Nothing recorded: the configured origin.
        assert await resolve("ws1", "p1") == "https://env.example"
        # A browser view wins, and is remembered for callers without a request...
        assert await resolve("ws1", "p1", "https://app.example") == "https://app.example"
        assert await resolve("ws1", "p1") == "https://app.example"
        # ...per pocket.
        assert await resolve("ws1", "p2") == "https://env.example"

    async def test_a_malformed_origin_is_not_remembered(self, beanie_test_db, monkeypatch) -> None:
        monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://env.example")
        resolve = sites_service.resolve_armed_builder_origin

        await resolve("ws1", "p1", "not an origin")
        assert await resolve("ws1", "p1") == "https://env.example"

    async def test_verify_uses_the_same_resolver(self, beanie_test_db, monkeypatch) -> None:
        monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://env.example")
        await sites_service.resolve_armed_builder_origin("ws1", "p1", "https://app.example")
        assert await verify.resolve_builder_origin("ws1", "p1") == "https://app.example"


@pytest.mark.parametrize("stem", [verify_store.LATEST_KEY, verify_store.VIEW_ORIGIN_KEY])
def test_pointer_records_are_never_evicted(tmp_path, monkeypatch, stem: str) -> None:
    monkeypatch.setenv("PAW_SITES_ARTIFACT_DIR", str(tmp_path))
    store = verify_store.FilesystemVerifyStore()
    store.write("p1", stem, {"x": 1})
    for i in range(verify_store.KEEP_PER_POCKET + 3):
        store.write("p1", verify_store.verdict_key(f"h{i}"), {"i": i})
    assert store.read("p1", stem) == {"x": 1}
