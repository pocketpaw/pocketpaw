# tests/ee/sites/test_preview_build_lane.py — SP-2: the DRAFT PREVIEW builds in the
# ephemeral Daytona lane instead of shelling out to bun in the API container.
# Created 2026-08-24.
#
# WHAT BROKE, AND WHAT THIS PINS. ``get_native_artifact``'s cold miss used to call
# ``generator.build`` → ``bun``. The deployed API container has no toolchain, so every
# cold preview raised and reached the user as ``sites.generator_failed``. The PUBLISH
# path had already solved this (SL-3: scaffold locally, build in a sandbox, verify the
# bytes); preview never adopted it. It does now, and the properties worth pinning are:
#
#   * A CACHE HIT IS STILL SYNCHRONOUS AND STILL FREE. This is the one that costs money
#     if it regresses: a miss now spends a Daytona sandbox (react 8.70s, svelte 14.67s
#     in-sandbox, before create / upload / teardown), so a bypassed cache check bills one
#     per keystroke in the editor. ``tests/mutations/site_preview_daytona.json`` mutates
#     the check away and this file is what must fail.
#   * A COLD MISS QUEUES AND SAYS SO — the build-pending shape, in the PUBLISH LANE'S
#     vocabulary (``queued`` / ``building`` / ``failed``), not a second one.
#   * A FAILED ENQUEUE IS AN ERROR, NOT A PENDING BUILD. Handing back a job id for a job
#     nobody will run makes a client poll forever while the endpoint reports progress —
#     strictly worse than a 503 the user can retry.
#   * THE LANE IS SINGLE-FLIGHT ON THE CONTENT HASH. The job id IS the hash, so a client
#     polling a 15s build every 2s reads the SAME job instead of opening a sandbox per
#     poll. A render that already FINISHED and left the store empty reports ``failed``
#     rather than spinning the poller or rebuilding identical inputs on a loop.
#   * THE JOB WRITES THE ARTIFACT THE ENDPOINT READS. The worker unpacks the tar and
#     stores ``{body_html, css}`` under the SAME content hash the enqueue used, which is
#     the whole reason the next call is a hit.
#
# The publish lane (``run_site_build`` / ``enqueue_site_build``) is untouched and is
# covered by test_build_job.py; nothing here should need to change if it does.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.faults import FaultyDaytonaClient, tar_bytes
from tests.ee.sites.test_build_job import FakeRunner

# asyncio_mode = "auto" is set project-wide, so async tests need no per-test mark and a
# sync one (the worker-registration check below) stays sync.
_ORIGIN = "https://dash.paw.example"

_SVELTE_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/lib/components/Hero.svelte": "<section class='hero'><h1>Bright Smile</h1></section>",
    "src/app.css": ":root{--brand:#0A84FF}",
}

# What a real armed build leaves in the static output dir, packed the way
# ``artifact_tar_command`` packs it: rooted AT the output dir's contents, so the members
# are ``./index.html`` and not ``./dist/index.html``.
_ARMED_INDEX = (
    b"<!DOCTYPE html><html><head>"
    b'<link rel="stylesheet" href="./assets/page.css" />'
    b"<style>.inline-critical{margin:0}</style>"
    b"</head><body>"
    b'<section data-paw-section="Hero"><h1 data-uid="Hero:headline:0">Bright Smile</h1></section>'
    b'<script id="paw-edit-manifest" type="application/json">{"leaves":[]}</script>'
    b"</body></html>"
)
_ARMED_CSS = b".hero{color:#0A84FF}"


def armed_artifact() -> bytes:
    """A clean build's tar carrying a prerendered ARMED page and its stylesheet."""
    return tar_bytes({"./index.html": _ARMED_INDEX, "./assets/page.css": _ARMED_CSS})


class FakePool:
    """An arq pool that records the enqueue instead of performing it.

    ``refuse=True`` is arq's duplicate-id answer (a ``None`` return), which is this
    lane's single-flight signal rather than an error.
    """

    def __init__(self, *, error: BaseException | None = None, refuse: bool = False) -> None:
        self.error = error
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None, **kw: Any):
        self.calls.append({"function": function, "args": args, "job_id": _job_id, "kwargs": kw})
        if self.error is not None:
            raise self.error
        return None if self.refuse else object()


class MemoryArtifactStore:
    """In-memory ``_store`` seam — the read-through logic without disk."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], tuple[str, str]] = {}
        self.writes = 0

    def read(self, pocket_id: str, content_hash: str) -> tuple[str, str] | None:
        return self.data.get((pocket_id, content_hash))

    def write(self, pocket_id: str, content_hash: str, body_html: str, css: str) -> None:
        self.data[(pocket_id, content_hash)] = (body_html, css)
        self.writes += 1


async def _make_pocket(engine: str = "svelte", source: dict[str, str] | None = None) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine=engine,
        source=dict(source if source is not None else _SVELTE_SOURCE),
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


async def _content_hash(pocket_id: str, *, engine: str = "svelte", origin: str = _ORIGIN) -> str:
    """The hash the service will compute for this pocket — the store key AND the job id.

    Derived from the PERSISTED pocket rather than from ``_SVELTE_SOURCE`` so a test that
    seeds the store seeds the key the service actually looks up.
    """
    from pocketpaw_ee.sites import generator_client

    wire = await pockets_service.get(pocket_id, "u1")
    return sites_service._artifact_content_hash(
        source=wire["source"],
        theme={},
        builder_origin=origin,
        gen_version=generator_client.generator_version(),
        engine=engine,
        # Resolved the same way the service resolves it, not hardcoded: the flag joined
        # the hash when the preview lane started carrying it, and a literal here would
        # drift silently the moment the default moves.
        keeps_client_bundle=sites_service._resolve_keeps_client_bundle(wire),
    )


async def _get(pocket_id: str, **kw: Any) -> dict[str, Any]:
    return await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin=kw.pop("builder_origin", _ORIGIN),
        **kw,
    )


# ---------------------------------------------------------------------------
# Arm 1 — the WARM request. Synchronous, and it must not reach the queue.
# ---------------------------------------------------------------------------


class TestACacheHitCostsNothing:
    async def test_a_hit_returns_the_render_and_never_enqueues(self, beanie_test_db) -> None:
        """The acceptance criterion that guards the bill: a warm request returns
        ``{body_html, css}`` with no enqueue and no sandbox.

        The pool is the assertion. A store fake alone would prove the render came from
        the cache but not that nothing was ALSO queued behind it — and a miss that
        happened to serve stale bytes while queueing a rebuild is exactly the regression
        that costs a sandbox per keystroke.
        """
        pocket_id = await _make_pocket()
        store = MemoryArtifactStore()
        store.data[(pocket_id, await _content_hash(pocket_id))] = (
            "<h1>cached</h1>",
            ".x{color:red}",
        )
        pool = FakePool()

        result = await _get(pocket_id, _store=store, _pool=pool)

        assert result["body_html"] == "<h1>cached</h1>"
        assert result["css"] == ".x{color:red}"
        assert pool.calls == [], "a cache hit must not queue a build"
        assert store.writes == 0, "a cache hit must not re-store"

    async def test_a_served_render_is_not_reported_as_a_build(self, beanie_test_db) -> None:
        """``build_status`` is how a client tells the two response shapes apart, so the
        hit branch has to say "not building" rather than leave the field at whatever the
        last miss set. ``"none"`` is the same value ``pocket_status`` gives a pocket that
        has never built."""
        pocket_id = await _make_pocket()
        store = MemoryArtifactStore()
        store.data[(pocket_id, await _content_hash(pocket_id))] = ("<h1>c</h1>", "")

        result = await _get(pocket_id, _store=store, _pool=FakePool())

        assert result["build_status"] == "none"
        assert result["build_job_id"] is None
        assert result["build_reason"] is None


# ---------------------------------------------------------------------------
# Arm 2 — the COLD request. Queues, and says so.
# ---------------------------------------------------------------------------


class TestAColdMissQueuesTheBuild:
    async def test_it_enqueues_and_returns_the_pending_shape(self, beanie_test_db) -> None:
        pocket_id = await _make_pocket()
        pool = FakePool()

        result = await _get(pocket_id, _store=MemoryArtifactStore(), _pool=pool)

        assert len(pool.calls) == 1, "a cold miss must queue exactly one build"
        assert pool.calls[0]["function"] == bj.PREVIEW_ARQ_FUNCTION_NAME
        assert result["build_status"] == "queued"
        assert result["build_job_id"] == pool.calls[0]["job_id"]
        # Empty rather than absent: the field's type must not change under a client that
        # reads it before checking the status.
        assert result["body_html"] == ""
        assert result["css"] == ""

    async def test_the_queued_payload_is_the_armed_build(self, beanie_test_db) -> None:
        """The whole point of the preview build is the ARMING — ``builderOrigin`` is what
        makes the generator stamp ``data-uid`` and embed the ``paw-edit-manifest``. A
        payload that lost it would build a page the native editor cannot select in, and
        the render would look correct."""
        pocket_id = await _make_pocket()
        pool = FakePool()

        await _get(pocket_id, _store=MemoryArtifactStore(), _pool=pool)

        _pocket, _hash, generator_input, engine, _timeout = pool.calls[0]["args"]
        assert engine == "svelte"
        assert generator_input["siteConfig"]["builderOrigin"] == _ORIGIN
        assert (
            generator_input["source"]["src/lib/components/Hero.svelte"]
            == (_SVELTE_SOURCE["src/lib/components/Hero.svelte"])
        )

    async def test_the_job_carries_the_hash_the_store_is_keyed_on(self, beanie_test_db) -> None:
        """The job id and the payload's ``content_hash`` are the SAME value the endpoint
        just looked up, and the worker writes under it. Recomputing it anywhere else is
        how a build's output lands under a key nothing reads."""
        pocket_id = await _make_pocket()
        pool = FakePool()

        result = await _get(pocket_id, _store=MemoryArtifactStore(), _pool=pool)

        expected = await _content_hash(pocket_id)
        assert pool.calls[0]["args"][0] == pocket_id
        assert pool.calls[0]["args"][1] == expected
        assert result["build_job_id"] == bj._preview_job_id(pocket_id, expected)

    async def test_the_capture_key_is_scrubbed_before_it_reaches_redis(
        self, beanie_test_db
    ) -> None:
        """The per-site capture secret never enters a queue payload or a third-party
        sandbox — the obligation ``build_job``'s header records, and the preview enqueue
        is a new way into the same pipe."""
        pocket_id = await _make_pocket()
        pool = FakePool()

        await _get(pocket_id, _store=MemoryArtifactStore(), _pool=pool)

        generator_input = pool.calls[0]["args"][2]
        assert generator_input["siteConfig"]["captureSignedKey"] == ""

    async def test_the_default_origin_falls_back_to_the_configured_one(
        self, beanie_test_db, monkeypatch
    ) -> None:
        """A caller with no request Origin still queues an ARMED build — the same
        ``PAW_SITES_BUILDER_ORIGIN`` precedence ``/editable`` and ``/dev-preview`` use."""
        monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://configured.paw.example")
        pocket_id = await _make_pocket()
        pool = FakePool()

        await _get(pocket_id, builder_origin="", _store=MemoryArtifactStore(), _pool=pool)

        generator_input = pool.calls[0]["args"][2]
        assert generator_input["siteConfig"]["builderOrigin"] == "https://configured.paw.example"


# ---------------------------------------------------------------------------
# Arm 3 — the enqueue that FAILED. Never a pending build.
# ---------------------------------------------------------------------------


class TestAFailedEnqueueIsNotAPendingBuild:
    async def test_a_dead_queue_is_an_error_not_a_job_id(self, beanie_test_db) -> None:
        """The failure this arm exists to prevent is the SILENT one. Returning the
        pending shape here hands the client a handle for a job nobody will run: it polls
        forever, and the endpoint reports progress the whole time. A 503 is recoverable —
        the user retries and gets a real slot."""
        pocket_id = await _make_pocket()
        pool = FakePool(error=RuntimeError("redis is gone"))

        with pytest.raises(CloudError) as excinfo:
            await _get(pocket_id, _store=MemoryArtifactStore(), _pool=pool)

        assert excinfo.value.code == "sites.preview_build_unavailable"
        assert excinfo.value.status_code >= 500
        # The mapped envelope, not the raw RuntimeError escaping as an opaque 500.
        assert not isinstance(excinfo.value, RuntimeError)

    async def test_nothing_is_written_to_the_store_on_a_failed_enqueue(
        self, beanie_test_db
    ) -> None:
        """A queue failure must not leave a cache entry behind — the next request has to
        be a genuine miss that tries again, not a hit on an artifact that was never
        built."""
        pocket_id = await _make_pocket()
        store = MemoryArtifactStore()

        with pytest.raises(CloudError):
            await _get(pocket_id, _store=store, _pool=FakePool(error=RuntimeError("boom")))

        assert store.writes == 0
        assert store.data == {}


# ---------------------------------------------------------------------------
# Single-flight — the job id IS the content hash.
# ---------------------------------------------------------------------------


class TestOneRenderIsOneSandbox:
    async def test_the_same_render_gets_the_same_job_id(self, beanie_test_db) -> None:
        """Two requests for an unchanged draft address the SAME job. This is what makes a
        polling client cost one sandbox instead of one per poll — arq refuses the second
        enqueue because the id is taken."""
        pocket_id = await _make_pocket()
        pool = FakePool()
        store = MemoryArtifactStore()

        first = await _get(pocket_id, _store=store, _pool=pool)
        second = await _get(pocket_id, _store=store, _pool=pool)

        assert first["build_job_id"] == second["build_job_id"]

    async def test_a_source_change_gets_a_different_job(self, beanie_test_db) -> None:
        """The other half of the same property: the guard must not be so sticky that an
        EDITED draft rides the previous render's build."""
        pocket_id = await _make_pocket()
        pool = FakePool()
        store = MemoryArtifactStore()

        before = await _get(pocket_id, _store=store, _pool=pool)
        await pockets_service.set_svelte_source_file(
            pocket_id,
            "u1",
            component_path="src/lib/components/Hero.svelte",
            new_source="<section class='hero'><h1>Changed</h1></section>",
        )
        after = await _get(pocket_id, _store=store, _pool=pool)

        assert before["build_job_id"] != after["build_job_id"]

    async def test_a_refused_enqueue_reports_the_build_already_running(
        self, beanie_test_db, monkeypatch
    ) -> None:
        """arq answers a duplicate id with ``None``. That is the guard firing, not an
        error, and it must read as an in-flight build carrying the SAME handle — a client
        that got a fresh id here would abandon the build it is actually waiting on."""
        pocket_id = await _make_pocket()
        pool = FakePool(refuse=True)

        async def _in_flight(_pool: Any, job_id: str) -> tuple[str, str | None]:
            return "building", None

        monkeypatch.setattr(bj, "_preview_job_outcome", _in_flight)
        result = await _get(pocket_id, _store=MemoryArtifactStore(), _pool=pool)

        assert result["build_status"] == "building"
        assert result["build_job_id"] == bj._preview_job_id(
            pocket_id, await _content_hash(pocket_id)
        )

    async def test_a_render_that_already_failed_reports_failed_not_pending(
        self, beanie_test_db, monkeypatch
    ) -> None:
        """The reason a refused enqueue is INSPECTED rather than assumed to be in flight.
        A completed job holds its id for ``keep_result`` (an hour by default), so
        reporting ``building`` here would spin a client on a build that is over — for an
        hour, on inputs that already failed."""
        pocket_id = await _make_pocket()

        async def _already_failed(_pool: Any, job_id: str) -> tuple[str, str | None]:
            return "failed", "build_failed:exit_1"

        monkeypatch.setattr(bj, "_preview_job_outcome", _already_failed)
        result = await _get(pocket_id, _store=MemoryArtifactStore(), _pool=FakePool(refuse=True))

        assert result["build_status"] == "failed"
        assert result["build_reason"] == "build_failed:exit_1"
        assert result["body_html"] == ""


class TestThePreviewLaneQueue:
    """backend-perf C1 — the preview enqueue rides the site-build queue too."""

    async def test_a_preview_is_queued_on_the_site_build_lane(self) -> None:
        """Both site lanes move together or the split only half applies.

        A draft preview is the SAME build as a publish — same sandbox, same budget --
        so leaving it on arq's default queue would keep the exact starvation the split
        removes, and would keep it on the lane a user hits by typing, not by publishing.
        """
        pool = FakePool()

        await bj.enqueue_preview_build(
            pocket_id="pk1",
            content_hash="c0ffee",
            engine="react",
            generator_input=_preview_input(),
            _pool_override=pool,
        )

        assert pool.calls[0]["kwargs"]["_queue_name"] == bj.SITE_BUILD_QUEUE_NAME


class TestReadingARefusedEnqueue:
    """``_preview_job_outcome`` against a stand-in for arq's ``Job``."""

    def _patch_job(self, monkeypatch, *, status: Any, info: Any) -> list[dict[str, Any]]:
        """Swap arq's ``Job`` for a stand-in and return the ctor calls it recorded.

        ``_queue_name`` is part of the signature here because it is part of arq's, and
        because it is the argument this read cannot afford to get wrong: arq scopes a job
        id to a queue, so a status read pointed at the wrong one finds nothing, answers
        ``building`` forever, and the client polls a job that ended minutes ago.
        """
        from arq.jobs import JobStatus

        calls: list[dict[str, Any]] = []

        class _FakeJob:
            def __init__(self, job_id: str, pool: Any, _queue_name: str | None = None) -> None:
                self.job_id = job_id
                calls.append({"job_id": job_id, "queue_name": _queue_name})

            async def status(self) -> Any:
                return status

            async def result_info(self) -> Any:
                return info

        monkeypatch.setattr(bj, "Job", _FakeJob)
        assert JobStatus.complete is not None  # the enum the production code compares on
        return calls

    async def test_a_running_job_reads_as_building(self, monkeypatch) -> None:
        from arq.jobs import JobStatus

        self._patch_job(monkeypatch, status=JobStatus.in_progress, info=None)
        assert await bj._preview_job_outcome(object(), "j1") == ("building", None)

    async def test_the_status_read_looks_in_the_queue_the_enqueue_wrote_to(
        self, monkeypatch
    ) -> None:
        """The read and both enqueues share one queue, or the read is blind.

        backend-perf C1 moved site builds off arq's default queue. arq scopes a job id to
        a queue, so a read left on the default would never find the job it just refused to
        enqueue: ``status()`` returns ``not_found``, this function reports ``building``,
        and the client polls a finished build forever. There is no error anywhere on that
        path, which is why it is asserted rather than left to the enqueue tests.
        """
        from arq.jobs import JobStatus

        calls = self._patch_job(monkeypatch, status=JobStatus.in_progress, info=None)
        await bj._preview_job_outcome(object(), "j1")

        assert calls == [{"job_id": "j1", "queue_name": bj.SITE_BUILD_QUEUE_NAME}]

    async def test_a_completed_job_reports_its_own_settlement(self, monkeypatch) -> None:
        from arq.jobs import JobStatus

        class _Info:
            success = True
            result = {"status": "failed", "reason": "scaffold_failed:generator_raised"}

        self._patch_job(monkeypatch, status=JobStatus.complete, info=_Info())
        assert await bj._preview_job_outcome(object(), "j1") == (
            "failed",
            "scaffold_failed:generator_raised",
        )

    async def test_a_job_that_raised_reads_as_a_lost_sandbox(self, monkeypatch) -> None:
        """``run_site_preview_build`` re-raises for exactly one condition — a sandbox it
        never reached. The exception text can name paths, so only the rung travels."""
        from arq.jobs import JobStatus

        class _Info:
            success = False
            result = RuntimeError("Daytona is not configured")

        self._patch_job(monkeypatch, status=JobStatus.complete, info=_Info())
        status, reason = await bj._preview_job_outcome(object(), "j1")
        assert status == "failed"
        assert reason == f"{bj.RUNG_SANDBOX_UNAVAILABLE}:job_raised"
        assert "Daytona" not in (reason or "")

    async def test_an_expired_result_reads_as_building_not_failed(self, monkeypatch) -> None:
        """A result that lapsed between the enqueue and this read is a pure race. The
        next poll enqueues cleanly because the id is free again, so the honest answer is
        "still coming" rather than a failure that never happened."""
        from arq.jobs import JobStatus

        self._patch_job(monkeypatch, status=JobStatus.complete, info=None)
        assert await bj._preview_job_outcome(object(), "j1") == ("building", None)


# ---------------------------------------------------------------------------
# The worker — what the queued job actually does.
# ---------------------------------------------------------------------------


def _preview_input(builder_origin: str = _ORIGIN) -> dict[str, Any]:
    from pocketpaw_ee.sites.generator_client import build_generator_input

    return build_generator_input(
        engine="react",
        theme={},
        site_id="preview-1",
        title="Bright Smile",
        capture_api_base="http://localhost:8888/api/v1",
        capture_signed_key="",
        ripple_spec={},
        source={"src/App.tsx": "export default () => null"},
        builder_origin=builder_origin,
    )


def _sandbox(artifact: bytes | None = None, **over: Any) -> FaultyDaytonaClient:
    """A Daytona fake whose sentinel PROMISES the artifact it will actually hand back.

    ``ok_sentinel`` defaults its ``artifact_bytes`` to the size of ``clean_artifact()``,
    and ``verify_artifact`` compares the promise against what arrives — so a fake handing
    back a differently-sized tar under the default sentinel either fails as
    ``artifact_truncated`` or passes only because it happened to be bigger. Both make the
    test say something other than what it claims.
    """
    from tests.ee.sites.faults import ok_sentinel

    payload = armed_artifact() if artifact is None else artifact
    sentinel = over.pop("sentinel", None) or ok_sentinel(artifact_bytes=len(payload))
    return FaultyDaytonaClient(artifact=payload, sentinel=sentinel, **over)


async def _run_preview_job(
    *,
    pocket_id: str = "pk1",
    content_hash: str = "c0ffee",
    engine: str = "react",
    client: Any = None,
    runner: Any = None,
    store: Any = None,
) -> dict[str, str]:
    return await bj.run_site_preview_build(
        {},
        pocket_id,
        content_hash,
        _preview_input(),
        engine,
        600,
        _runner=runner or FakeRunner(),
        _client=client if client is not None else _sandbox(),
        _store=store,
    )


class TestThePreviewJob:
    async def test_a_clean_build_lands_in_the_store_as_body_and_css(self) -> None:
        """The join the whole slice rests on: the tar the sandbox produced becomes the
        exact ``{body_html, css}`` the endpoint serves, under the SAME content hash the
        enqueue used. Asserted on the ARMED markers, because a job that stored the
        untouched ``index.html`` (or the wrong dir's) would also write two strings."""
        store = MemoryArtifactStore()
        settlement = await _run_preview_job(store=store)

        assert settlement["status"] == "built"
        body_html, css = store.data[("pk1", "c0ffee")]
        # <body> INNER — the stamped leaf and the manifest, without the head chrome.
        assert 'data-uid="Hero:headline:0"' in body_html
        assert 'id="paw-edit-manifest"' in body_html
        assert "<head" not in body_html
        # CSS concatenates the inline critical block AND the linked stylesheet, which is
        # only possible if the tar was unpacked UNDER the engine's output dir — the read
        # resolves the href relative to it.
        assert ".inline-critical{margin:0}" in css
        assert _ARMED_CSS.decode() in css

    async def test_a_failed_build_stores_nothing_and_names_the_rung(self) -> None:
        """A build that did not produce an artifact must leave the cache untouched — a
        stored empty render would be served forever as a successful preview."""
        store = MemoryArtifactStore()
        from tests.ee.sites.faults import ok_sentinel

        settlement = await _run_preview_job(
            client=_sandbox(sentinel=ok_sentinel(build_exit=1, stderr_tail="TS2304")),
            store=store,
        )

        assert settlement["status"] == "failed"
        assert settlement["reason"].startswith("build_failed")
        assert store.data == {}

    async def test_a_scaffold_that_raises_settles_without_a_sandbox(self) -> None:
        """Nothing was billed, so the settlement is the whole outcome. The generator's
        stderr can name paths and carry the user's content, so only the rung travels."""
        client = _sandbox()
        settlement = await _run_preview_job(
            runner=FakeRunner(raises=RuntimeError("bun: not found")),
            client=client,
        )

        assert settlement == {
            "status": "failed",
            "reason": f"{bj.RUNG_SCAFFOLD_FAILED}:generator_raised",
        }
        assert client.calls == [], "a scaffold failure must not create a sandbox"
        assert "bun: not found" not in settlement["reason"]

    async def test_an_empty_scaffold_is_caught_before_a_sandbox_exists(self) -> None:
        client = _sandbox()
        settlement = await _run_preview_job(runner=FakeRunner(tree={}), client=client)

        assert settlement["reason"] == f"{bj.RUNG_SCAFFOLD_EMPTY}:no_files_generated"
        assert client.calls == []

    async def test_a_clean_build_whose_artifact_cannot_be_read_is_its_own_rung(self) -> None:
        """The build compiled; the tar carried no page. Naming this a build failure would
        send the user to debug working code, which is the same reason the deploy path
        gives ``deploy_failed`` its own rung."""
        store = MemoryArtifactStore()
        settlement = await _run_preview_job(
            client=_sandbox(tar_bytes({"./assets/app.js": b"console.log(1)"})),
            store=store,
        )

        assert settlement["status"] == "failed"
        assert settlement["reason"] == f"{bj.RUNG_PREVIEW_UNREADABLE}:read_or_store_raised"
        assert store.data == {}

    async def test_a_lost_sandbox_re_raises_for_the_worker_log(self) -> None:
        """The one condition the job does NOT swallow. ``_preview_job_outcome`` maps the
        failed arq job back to the ``sandbox_unavailable`` rung, so a poller still gets a
        terminal answer — but an operator needs the real exception in the log."""
        with pytest.raises(Exception):
            await _run_preview_job(client=_sandbox(fail_at="create"))

    async def test_an_unbuildable_engine_is_refused_before_a_sandbox(self) -> None:
        """A routing bug, not a build failure. ``get_native_artifact`` gates on
        ``has_native_edit_lane`` and every engine that passes also builds here — checked
        anyway rather than spending a sandbox to discover the day that stops holding."""
        client = _sandbox()
        settlement = await _run_preview_job(engine="html", client=client)

        assert settlement["reason"] == f"{bj.RUNG_ENGINE_NOT_BUILDABLE}:html"
        assert client.calls == []

    async def test_the_enqueue_refuses_an_unbuildable_engine_too(self) -> None:
        """The same guard on the near side, so a routing bug never even reaches Redis."""
        pool = FakePool()
        with pytest.raises(RuntimeError):
            await bj.enqueue_preview_build(
                pocket_id="pk1",
                content_hash="c0ffee",
                engine="html",
                generator_input=_preview_input(),
                _pool_override=pool,
            )
        assert pool.calls == []


class TestTheWorkerRunsThePreviewLane:
    def test_the_preview_job_is_registered_under_the_name_the_enqueue_writes(
        self, monkeypatch
    ) -> None:
        """An enqueue name and a registration name that drift produce a job that sits in
        Redis forever with no worker willing to claim it, and no error anywhere."""
        monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")
        from pocketpaw_ee.cloud.chat.runs import worker as worker_mod

        registered = {f.name: f for f in worker_mod.WorkerSettings.functions if hasattr(f, "name")}

        assert bj.PREVIEW_ARQ_FUNCTION_NAME in registered
        preview = registered[bj.PREVIEW_ARQ_FUNCTION_NAME]
        assert preview.coroutine is bj.run_site_preview_build
        # Same budget as the publish build — it IS the same build; only what happens to
        # the artifact differs.
        assert preview.timeout_s == bj.site_build_job_timeout_seconds()
        # A preview is billed per attempt too, so the retry decision is the caller's.
        assert preview.max_tries == 1
