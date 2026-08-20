# tests/ee/sites/test_async_publish.py — SL-3: the static publish path enqueues its build
# instead of running it inline, and the worker finishes the publish.
#
# Created 2026-08-10 (SL-3).
#
# THE TWO FAILURES THIS SUITE EXISTS FOR, and they pull in opposite directions:
#
#   1. A SITE THAT STOPS PUBLISHING. The flip moves the build off the request, so every
#      engine that is NOT flipped must still build and deploy inline, byte-for-byte. Most
#      of this file is that regression guarantee, because the change is worthless if it
#      quietly breaks ripple.
#   2. A PUBLISH THAT REPORTS SUCCESS AND NEVER GOES LIVE. Async publishing means the
#      request returns before the site is up, so every path out of the job has to leave the
#      row saying something true — and `built` must never mean "built but not serving".
#
# WHICH SITES FLIP, and why it is a property of the SITE rather than of the engine name:
# an adapter-cloudflare artifact (ripple, DYNAMIC svelte) has pages rendered by a
# `_worker.js` whose imports sit outside the tarred directory, so the artifact cannot
# serve. That is the same fact `truth_lane` refuses to preview on. react emits a
# prerendered assets-only `dist`, so its tar IS the whole site.
#
# Edited 2026-08-11 (SL-4): STATIC svelte flips too, so the file now drives ONE engine down
# BOTH lanes — see TestTheSvelteTrackForksOnTheSite. SL-3 excluded it on the stated grounds
# that "which adapter ran is not knowable at enqueue time"; the dynamic fork sitting three
# lines above the async fork already disproved that, so the tests that encoded it are gone
# and the dynamic-svelte exclusion is now asserted on its real reason. The
# dynamic-stays-out half is the regression guarantee and is the more important of the two:
# queueing one trades a publish that works for one nothing can deploy.
#
# Mutations in tests/mutations/sl3_publish_flip.json and sl4_svelte_async.json, run and
# caught.

from __future__ import annotations

import tempfile
from typing import Any

import pytest
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import build_state as bs
from pocketpaw_ee.sites import engines as engines_mod
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.faults import FaultyDaytonaClient, clean_artifact, ok_sentinel, tar_bytes

REACT_SOURCE = {"src/App.tsx": "export default function App() { return <p>hi</p>; }"}


class _FakeGenerator:
    """Records whether the INLINE build ran. The whole flip is about that call."""

    def __init__(self) -> None:
        self.build_calls: list[dict[str, Any]] = []

    async def build(self, **kw: Any) -> Any:
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.build_calls.append(kw)
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakePool:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None, **kw: Any):
        self.calls.append({"function": function, "args": args, "job_id": _job_id, "kwargs": kw})
        if self.error is not None:
            raise self.error
        return object()


def _install_pool(monkeypatch: pytest.MonkeyPatch, pool: _FakePool) -> _FakePool:
    """Give the REAL enqueue helper a pool, so the row stamping under test is the
    production one rather than a stub of it."""

    async def _get_pool() -> Any:
        return pool

    monkeypatch.setattr(bj, "_get_pool", _get_pool)
    return pool


async def _seed_pocket(*, workspace_id: str = "ws1", pattern: str | None = None) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(workspace=workspace_id, name="P", owner="u1", type="site", pattern=pattern)
    await doc.insert()
    return str(doc.id)


async def _publish(*, engine: str, pocket_id: str, gen: _FakeGenerator, **over: Any) -> Any:
    kwargs: dict[str, Any] = {
        "workspace_id": "ws1",
        "user_id": "u1",
        "pocket_id": pocket_id,
        "theme": {},
        "name": "x",
        "engine": engine,
        "_generator": gen,
        "_local_deploy": lambda site_id, project_dir: f"http://127.0.0.1/{site_id}/",
    }
    if engine == "ripple":
        kwargs["ripple_spec"] = {"type": "container"}
    else:
        kwargs["source"] = REACT_SOURCE
    kwargs.update(over)
    return await sites_service.publish(**kwargs)


# ---------------------------------------------------------------------------
# Which engines flip
# ---------------------------------------------------------------------------


def _flips(engine: str | None, *, pattern: str | None = None, spec: Any = None) -> bool:
    return sites_service.build_runs_async(engine, pattern=pattern, ripple_spec=spec)


class TestOnlyTheDeployableEngineFlips:
    def test_react_builds_asynchronously(self) -> None:
        assert _flips("react") is True

    def test_static_svelte_builds_asynchronously(self) -> None:
        """SL-4. adapter-static emits ``build`` with no server entry, so a static svelte
        tar IS the whole deployable site — the same property that qualified react."""
        assert _flips("svelte", pattern="landing") is True

    @pytest.mark.parametrize("engine", ["ripple", "html"])
    def test_every_other_engine_stays_inline(self, engine: str) -> None:
        """Not an arbitrary allowlist. html runs no build at all; ripple builds on
        adapter-cloudflare and produces an artifact that cannot serve."""
        assert _flips(engine) is False

    def test_an_unknown_engine_stays_inline(self) -> None:
        """Unknown normalises to ripple everywhere in this codebase, and ripple is inline.
        The fallback must not flip a site whose engine we failed to recognise."""
        for value in (None, "", "nope"):
            assert _flips(value) is False

    def test_no_flipped_engine_needs_a_worker_to_serve_its_pages(self) -> None:
        """The property behind the allowlist, checked against engines.py rather than
        restated. An engine whose output is worker-rendered cannot be deployed from a tar
        of its static dir, so it must not be in the async set.

        ``expects_server_worker`` is TRI-STATE and svelte's answer is ``None`` — either
        shape is legitimate from the name alone — so the assertion is "not True" rather
        than "is False". Narrowing it back to ``is False`` would fail on the very site this
        slice added, and would be asserting the engine name can answer a question SL-1
        established it cannot."""
        for engine in ("ripple", "svelte", "html", "react"):
            if _flips(engine, pattern="landing"):
                assert engines_mod.expects_server_worker(engine) is not True, engine
                assert engines_mod.needs_node_build(engine) is True, engine


class TestDynamicSvelteStaysInline:
    """THE REGRESSION GUARANTEE OF SL-4, and the half that matters more than the flip.

    A dynamic svelte site builds on adapter-cloudflare, whose pages are rendered by a
    ``_worker.js`` importing two files from OUTSIDE the tarred directory — the same fact
    ``truth_lane`` refuses to preview one on. Queueing it would trade a publish that works
    for one nothing can deploy, and the deploy-side unpack would silently DROP the worker
    on the way to the edge. So every route by which a site can be dynamic must keep it out.
    """

    def test_the_stamped_pattern_keeps_it_inline(self) -> None:
        assert _flips("svelte", pattern="dynamic") is False

    @pytest.mark.parametrize("key", ["sources", "actions", "auth"])
    def test_an_unstamped_spec_carrying_live_bindings_keeps_it_inline(self, key: str) -> None:
        """The safety net, not a formality: a pocket can carry dynamic bindings without
        having been stamped, and ``_is_dynamic`` is authoritative on both routes."""
        assert _flips("svelte", spec={key: [{"name": "x"}]}) is False

    def test_the_predicate_has_no_default_for_the_site(self) -> None:
        """``pattern`` / ``ripple_spec`` are REQUIRED keyword args. A default would mean
        "assume static", and assuming static about a dynamic svelte site is exactly the
        broken publish above — so there is deliberately no default to get wrong."""
        with pytest.raises(TypeError):
            sites_service.build_runs_async("svelte")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The engines that did NOT flip must be untouched
# ---------------------------------------------------------------------------


class TestTheInlinePathIsUnchanged:
    """The regression guarantee. This is the half that matters most: a flip that breaks
    ripple has cost more than it bought."""

    async def test_a_ripple_publish_still_builds_inline(self, beanie_test_db) -> None:
        pocket_id = await _seed_pocket()
        gen = _FakeGenerator()
        doc = await _publish(engine="ripple", pocket_id=pocket_id, gen=gen)
        assert len(gen.build_calls) == 1
        assert doc.deployed is True
        assert doc.build_status == "none"

    async def test_a_ripple_publish_does_not_enqueue(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        await _publish(engine="ripple", pocket_id=pocket_id, gen=_FakeGenerator())
        assert pool.calls == []

    async def test_a_ripple_publish_still_reaches_the_local_deploy(self, beanie_test_db) -> None:
        """Local mode is the path the smoke tests use; the flip must not disturb it."""
        pocket_id = await _seed_pocket()
        deployed: list[str] = []
        await _publish(
            engine="ripple",
            pocket_id=pocket_id,
            gen=_FakeGenerator(),
            _local_deploy=lambda site_id, project_dir: (
                deployed.append(site_id) or f"http://127.0.0.1/{site_id}/"
            ),
        )
        assert len(deployed) == 1

    async def test_a_dynamic_publish_still_goes_to_the_provision_job(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dynamic fork is checked BEFORE the async fork, so a dynamic site keeps its
        own durable job and never enters the build lane."""
        dispatched: list[dict[str, Any]] = []

        async def _dispatch(**kw: Any) -> dict[str, Any]:
            dispatched.append(kw)
            return {"job_id": "job-1"}

        from pocketpaw_ee.cloud.jobs import service as jobs_service

        monkeypatch.setattr(jobs_service, "dispatch_job", _dispatch)
        pool = _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket(pattern="dynamic")
        gen = _FakeGenerator()

        await _publish(engine="ripple", pocket_id=pocket_id, gen=gen, pattern="dynamic")

        assert len(dispatched) == 1
        assert dispatched[0]["job_name"] == "provision_site"
        assert pool.calls == []
        assert gen.build_calls == []


# ---------------------------------------------------------------------------
# The flip
# ---------------------------------------------------------------------------


class TestAReactPublishEnqueues:
    async def test_it_does_not_build_inline(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        gen = _FakeGenerator()
        await _publish(engine="react", pocket_id=pocket_id, gen=gen)
        assert gen.build_calls == [], "the request still ran a build"

    async def test_the_response_carries_something_to_poll(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without these the client has nothing to watch and the user sees a publish that
        appears to do nothing — the regression the whole two-repo sequence exists to
        prevent."""
        _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        doc = await _publish(engine="react", pocket_id=pocket_id, gen=_FakeGenerator())
        assert doc.build_status == "queued"
        assert doc.build_job_id
        assert doc.build_started_at is not None
        wire = sites_service._to_response(doc)
        assert wire.build_status == "queued"
        assert wire.build_job_id == doc.build_job_id

    async def test_a_first_publish_is_honestly_not_deployed_yet(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        doc = await _publish(engine="react", pocket_id=pocket_id, gen=_FakeGenerator())
        assert doc.deployed is False
        assert doc.url == ""

    async def test_a_rebuild_leaves_the_live_site_serving(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The difference from the dynamic provision path, and it is deliberate: that one
        clears ``deployed``/``url`` while its job runs. Here the PREVIOUS deploy is still
        serving the moment a rebuild is queued, so clearing them would report a working
        site as not-live for the length of a build. The frontend already codes to this —
        "a site can be live and simultaneously mid-rebuild"."""
        _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        site_id = str(sites_service._live_object_id("ws1", pocket_id))
        live = Site(
            id=sites_service._live_object_id("ws1", pocket_id),
            workspace="ws1",
            pocket_id=pocket_id,
            owner="u1",
            name="x",
            script_name=site_id,
            deployed=True,
            url="https://live.example",
        )
        await live.insert()

        doc = await _publish(engine="react", pocket_id=pocket_id, gen=_FakeGenerator())

        assert doc.deployed is True
        assert doc.url == "https://live.example"
        assert doc.build_status == "queued"

    async def test_the_payload_carries_the_deploy_inputs(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker deploys with what the PUBLISH captured, not with whatever the
        pocket's draft has become by the time the build finishes."""
        pool = _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        await _publish(engine="react", pocket_id=pocket_id, gen=_FakeGenerator())
        inputs = pool.calls[0]["kwargs"]["deploy_inputs"]
        assert inputs["engine"] == "react"
        assert inputs["pocket_id"] == pocket_id
        assert inputs["workspace_id"] == "ws1"
        assert inputs["site_id"] == pool.calls[0]["args"][1]

    async def test_a_second_publish_does_not_open_a_second_sandbox(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        await _publish(engine="react", pocket_id=pocket_id, gen=_FakeGenerator())
        await _publish(engine="react", pocket_id=pocket_id, gen=_FakeGenerator())
        assert len(pool.calls) == 1, "single-flight let a second build through"

    async def test_a_failed_enqueue_surfaces_and_leaves_the_site_republishable(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE JUDGEMENT CALL. Returning success for a build that was never queued is the
        worst outcome: the row would read in-progress forever and the UI would poll a job
        that does not exist. So the enqueue failure propagates — the caller turns it into
        the 5xx the user sees — and the row lands terminal so the next publish can run."""
        _install_pool(monkeypatch, _FakePool(error=RuntimeError("redis is down")))
        pocket_id = await _seed_pocket()

        with pytest.raises(RuntimeError, match="redis is down"):
            await _publish(engine="react", pocket_id=pocket_id, gen=_FakeGenerator())

        doc = await Site.find_one({"pocket_id": pocket_id, "workspace": "ws1"})
        assert doc is not None
        assert doc.build_status == "failed"
        assert doc.build_reason == "enqueue_failed:pool_or_enqueue_raised"
        assert bs.should_enqueue(doc, 600) is True
        assert bs.is_in_flight(doc) is False


# ---------------------------------------------------------------------------
# SL-4 — the svelte track, BOTH WAYS through one publish
# ---------------------------------------------------------------------------


SVELTE_SOURCE = {"src/routes/+page.svelte": "<h1>hi</h1>"}


def _svelte_static_artifact() -> bytes:
    """An adapter-STATIC ``build`` tree, as the include-list tar would pack it.

    Shaped from what adapter-static actually emits — a prerendered ``index.html`` per
    route, ``_app/immutable`` for the JS/CSS payload, and whatever the project's
    ``static/`` dir held — and deliberately NOT carrying a ``_worker.js`` or a
    ``_routes.json``, because that is the whole difference from the dynamic shape and the
    reason this tree can be deployed from a tar at all.
    """
    return tar_bytes(
        {
            "./index.html": b"<!doctype html><title>hi</title>",
            "./about/index.html": b"<!doctype html><title>about</title>",
            "./favicon.png": b"\x89PNG\r\n\x1a\n",
            "./_app/immutable/e.js": b"export const x=1",
            "./_app/immutable/assets/style.css": b"body{margin:0}",
            "./_app/version.json": b'{"version":"1"}',
        }
    )


class TestTheSvelteTrackForksOnTheSite:
    """One engine, two publishes, two different lanes — driven end-to-end through
    ``publish`` rather than through the predicate, because the predicate agreeing with
    itself is not the property that matters. What matters is which lane a real publish
    lands in, and the two tests below are deliberately symmetric so a change that flattens
    the fork fails one of them.
    """

    async def test_a_static_svelte_publish_enqueues_and_returns(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()
        gen = _FakeGenerator()

        doc = await _publish(engine="svelte", pocket_id=pocket_id, gen=gen, source=SVELTE_SOURCE)

        assert gen.build_calls == [], "the request still ran a build"
        assert len(pool.calls) == 1
        assert doc.build_status == "queued"
        assert doc.build_job_id
        # The engine reaches the worker as the normalised positional payload, which is
        # how the lane knows to probe for adapter-static's ``build`` instead of the
        # dynamic output dir.
        assert pool.calls[0]["args"][3] == "svelte"

    async def test_a_dynamic_svelte_publish_never_enters_the_build_lane(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE ONE THAT MATTERS MOST. Its artifact's pages come from a ``_worker.js`` whose
        imports sit outside the tar, and the deploy-side unpack DROPS that worker — so
        enqueueing it would replace a working publish with one nothing can deploy. It keeps
        the durable provision job (which owns standing up its D1) and never queues a build.
        """
        dispatched: list[dict[str, Any]] = []

        async def _dispatch(**kw: Any) -> dict[str, Any]:
            dispatched.append(kw)
            return {"job_id": "job-1"}

        from pocketpaw_ee.cloud.jobs import service as jobs_service

        monkeypatch.setattr(jobs_service, "dispatch_job", _dispatch)
        pool = _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket(pattern="dynamic")
        gen = _FakeGenerator()

        await _publish(
            engine="svelte",
            pocket_id=pocket_id,
            gen=gen,
            source=SVELTE_SOURCE,
            pattern="dynamic",
        )

        assert pool.calls == [], "a dynamic svelte publish queued a build"
        assert len(dispatched) == 1
        assert dispatched[0]["job_name"] == "provision_site"
        assert gen.build_calls == []

    async def test_an_unstamped_dynamic_svelte_publish_also_stays_out(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same guarantee via the OTHER route into ``_is_dynamic``: a spec carrying live
        bindings that was never stamped ``pattern="dynamic"``. Checked through publish
        because the stamp is what a caller controls and the spec is what a pocket can drift
        into."""
        dispatched: list[dict[str, Any]] = []

        async def _dispatch(**kw: Any) -> dict[str, Any]:
            dispatched.append(kw)
            return {"job_id": "job-1"}

        from pocketpaw_ee.cloud.jobs import service as jobs_service

        monkeypatch.setattr(jobs_service, "dispatch_job", _dispatch)
        pool = _install_pool(monkeypatch, _FakePool())
        pocket_id = await _seed_pocket()

        await _publish(
            engine="svelte",
            pocket_id=pocket_id,
            gen=_FakeGenerator(),
            source=SVELTE_SOURCE,
            ripple_spec={"actions": [{"name": "signup"}]},
        )

        assert pool.calls == [], "an unstamped dynamic svelte publish queued a build"
        assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# The worker finishes the publish
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(self, tree: dict[str, bytes] | None = None) -> None:
        self.tree = tree or {"package.json": b"{}", "src/App.tsx": b"x"}

    async def generate(self, input_json: dict[str, Any], out_dir: str) -> dict[str, Any]:
        from pathlib import Path

        project = Path(out_dir, "project")
        for rel, contents in self.tree.items():
            target = project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        return {"projectDir": str(project)}


def _deploy_inputs(site_id: str) -> dict[str, Any]:
    return {
        "workspace_id": "ws1",
        "user_id": "u1",
        "pocket_id": "pk1",
        "site_id": site_id,
        "signed_key": "k",
        "site_name": "x",
        "engine": "react",
        "pattern": None,
        "builder_origin": None,
    }


class TestTheWorkerFinishesThePublish:
    async def _site(self) -> Site:
        doc = Site(workspace="ws1", pocket_id="pk1", owner="u1", name="x", build_status="queued")
        await doc.insert()
        return doc

    async def test_a_clean_build_is_deployed_before_the_row_says_built(
        self, beanie_test_db
    ) -> None:
        """Ordering, not decoration: ``built`` must never mean "built but not serving",
        because a client reads that as done."""
        site = await self._site()
        seen: list[str] = []

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            fresh = await Site.get(site.id)
            seen.append(fresh.build_status if fresh else "?")
            return fresh

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "react", "siteConfig": {}},
            "react",
            600,
            deploy_inputs=_deploy_inputs(str(site.id)),
            _runner=_FakeRunner(),
            _client=FaultyDaytonaClient(artifact=clean_artifact()),
            _deployer=_deploy,
        )

        assert seen == ["building"], "the deploy ran after the row already said built"
        fresh = await Site.get(site.id)
        assert fresh is not None
        assert fresh.build_status == "built"

    async def test_the_deploy_receives_the_artifact_under_the_engines_output_dir(
        self, beanie_test_db
    ) -> None:
        """The deploy targets resolve their source as ``<project_dir>/<static_output_rel>``
        while the tar is rooted AT that directory's contents. Extracting flat would deploy
        an empty directory and the site would go live blank."""
        from pathlib import Path

        site = await self._site()
        captured: dict[str, Any] = {}

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            captured["dir"] = project_dir
            captured["index"] = Path(project_dir, "dist", "index.html").is_file()
            captured["nested"] = Path(project_dir, "dist", "assets", "app.js").is_file()
            return None

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "react", "siteConfig": {}},
            "react",
            600,
            deploy_inputs=_deploy_inputs(str(site.id)),
            _runner=_FakeRunner(),
            _client=FaultyDaytonaClient(artifact=clean_artifact()),
            _deployer=_deploy,
        )

        assert captured["index"] is True
        assert captured["nested"] is True

    async def test_a_static_svelte_artifact_lands_where_the_resolver_looks(
        self, beanie_test_db
    ) -> None:
        """SL-4, and the failure it prevents is a BLANK site replacing a working one.

        The nominal ``static_output_rel("svelte")`` is the DYNAMIC shape
        (``.svelte-kit/cloudflare``), which a static build never produces. Extracting there
        and then letting ``resolve_static_output_rel`` probe would happen to agree — but it
        would agree on a lie about which adapter ran, and the honest dir is what the deploy
        target must find. So the extract uses the first PROBE CANDIDATE, and this asserts
        the resolver then lands on the tree that was actually written."""
        from pathlib import Path

        from pocketpaw_ee.sites.engines import resolve_static_output_rel

        site = await self._site()
        captured: dict[str, Any] = {}

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            captured["rel"] = resolve_static_output_rel(project_dir, "svelte")
            captured["index"] = Path(project_dir, "build", "index.html").is_file()
            captured["app"] = Path(project_dir, "build", "_app", "immutable", "e.js").is_file()
            return None

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "svelte", "siteConfig": {}},
            "svelte",
            600,
            deploy_inputs=_deploy_inputs(str(site.id)),
            _runner=_FakeRunner(),
            _client=FaultyDaytonaClient(artifact=_svelte_static_artifact()),
            _deployer=_deploy,
        )

        assert captured["rel"] == "build", "the deploy resolved a dir the unpack never wrote"
        assert captured["index"] is True
        assert captured["app"] is True, "svelte's whole JS payload was dropped"

    async def test_the_preview_shaped_skip_list_drops_nothing_a_static_svelte_site_needs(
        self, beanie_test_db
    ) -> None:
        """The trap SL-3 flagged for exactly this moment. ``unpack_artifact``'s skip list is
        written for a PREVIEW and was safe only because the lane was react-only, so it is
        re-checked here against an adapter-static tree rather than assumed still safe.

        The finding: nothing deployable is dropped. adapter-static emits no ``_worker.js``
        and none of adapter-cloudflare's deploy metadata, and ``_app`` — where svelte's
        entire JS/CSS payload lives — matches no skip rule, because the lists key on exact
        segment names and ``_app`` is not one of them."""
        from pathlib import Path

        from pocketpaw_ee.sites import artifact_preview

        with tempfile.TemporaryDirectory() as dest:
            unpacked = artifact_preview.unpack_artifact(_svelte_static_artifact(), Path(dest))
            assert unpacked.server_entries == ()
            assert unpacked.metadata_entries == ()
            assert unpacked.rejected == ()
            for rel in (
                "index.html",
                "about/index.html",
                "favicon.png",
                "_app/immutable/e.js",
                "_app/immutable/assets/style.css",
            ):
                assert Path(dest, rel).is_file(), rel

    async def test_a_hostile_member_is_refused_rather_than_written(self, beanie_test_db) -> None:
        """The artifact is customer content, so the deploy path unpacks through the SAME
        hardened extractor the preview uses instead of a second one written here. A member
        that escapes its root must not land on the deploy host."""
        from pathlib import Path

        site = await self._site()
        escaped = Path("/tmp/paw-escape-canary.txt")
        if escaped.exists():
            escaped.unlink()
        evil = tar_bytes(
            {
                "./index.html": b"<p>ok</p>",
                "../../../../tmp/paw-escape-canary.txt": b"escaped",
            }
        )
        captured: dict[str, Any] = {}

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            captured["index"] = Path(project_dir, "dist", "index.html").is_file()
            return None

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "react", "siteConfig": {}},
            "react",
            600,
            deploy_inputs=_deploy_inputs(str(site.id)),
            _runner=_FakeRunner(),
            # The sentinel must promise THIS artifact's length, not the default clean
            # one's. ``run_build`` compares the promise against what arrived (2026-08-11),
            # and ``evil`` is a few bytes shorter than ``clean_artifact()`` — so on the
            # default sentinel it would be rejected as a TRUNCATED transfer and never reach
            # the extraction this test is about. The escape has to be the reason it fails,
            # not a byte count that happens to differ.
            _client=FaultyDaytonaClient(
                artifact=evil, sentinel=ok_sentinel(artifact_bytes=len(evil))
            ),
            _deployer=_deploy,
        )

        assert escaped.exists() is False, "a tar member escaped the deploy root"
        # Not vacuous: the innocent member still arrived.
        assert captured["index"] is True

    async def test_a_deploy_failure_after_a_clean_build_is_its_own_rung(
        self, beanie_test_db
    ) -> None:
        """Nothing about the user's build was wrong, so ``build_failed`` would send them to
        debug a site that compiles. It is still terminal, because the site did not go live
        and the row must stay republishable."""
        site = await self._site()

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            raise RuntimeError("wrangler exploded")

        with pytest.raises(RuntimeError, match="wrangler exploded"):
            await bj.run_site_build(
                {},
                "ws1",
                str(site.id),
                {"engine": "react", "siteConfig": {}},
                "react",
                600,
                deploy_inputs=_deploy_inputs(str(site.id)),
                _runner=_FakeRunner(),
                _client=FaultyDaytonaClient(artifact=clean_artifact()),
                _deployer=_deploy,
            )

        fresh = await Site.get(site.id)
        assert fresh is not None
        assert fresh.build_status == "failed"
        assert fresh.build_reason == "deploy_failed:deploy_raised"
        assert bs.should_enqueue(fresh, 600) is True

    async def test_a_failed_build_never_reaches_the_deploy(self, beanie_test_db) -> None:
        site = await self._site()
        deploys: list[str] = []

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            deploys.append(project_dir)
            return None

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "react", "siteConfig": {}},
            "react",
            600,
            deploy_inputs=_deploy_inputs(str(site.id)),
            _runner=_FakeRunner(),
            _client=FaultyDaytonaClient(sentinel=ok_sentinel(build_exit=1)),
            _deployer=_deploy,
        )

        assert deploys == []
        fresh = await Site.get(site.id)
        assert fresh is not None
        assert fresh.build_status == "failed"

    async def test_a_build_with_no_deploy_inputs_still_records_its_verdict(
        self, beanie_test_db
    ) -> None:
        """The slice-2 shape, still reachable: a build queued to VERIFY an artifact
        deploys nothing and is not a broken publish."""
        site = await self._site()
        deploys: list[str] = []

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            deploys.append(project_dir)
            return None

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "react", "siteConfig": {}},
            "react",
            600,
            _runner=_FakeRunner(),
            _client=FaultyDaytonaClient(artifact=clean_artifact()),
            _deployer=_deploy,
        )

        assert deploys == []
        fresh = await Site.get(site.id)
        assert fresh is not None
        assert fresh.build_status == "built"


class TestTheDeploySeamRunsTheInlineTail:
    """``deploy_prebuilt_site`` is where the two paths converge, and nothing above tests it
    (the worker tests inject a fake deployer to isolate the job). This drives the real seam,
    which is what proves the async path deploys through the SAME code the inline path does
    rather than a second implementation that can drift.
    """

    async def test_it_deploys_the_prebuilt_tree_without_rebuilding(self, beanie_test_db) -> None:
        from pathlib import Path

        pocket_id = await _seed_pocket()
        site_id = str(sites_service._live_object_id("ws1", pocket_id))
        project = Path(tempfile.mkdtemp(prefix="paw-seam-"))
        (project / "dist").mkdir()
        (project / "dist" / "index.html").write_text("<p>hi</p>", encoding="utf-8")

        deployed: list[tuple[str, str]] = []

        def _local_deploy(sid: str, project_dir: str) -> str:
            deployed.append((sid, project_dir))
            return f"http://127.0.0.1/{sid}/"

        # The real generator would need bun on PATH; if the seam rebuilt, this would raise
        # rather than silently pass, which is the point of NOT injecting a fake generator.
        doc = await sites_service.deploy_prebuilt_site(
            project_dir=str(project),
            deploy_inputs={
                "workspace_id": "ws1",
                "user_id": "u1",
                "pocket_id": pocket_id,
                "site_id": site_id,
                "signed_key": "k",
                "site_name": "x",
                "engine": "react",
                "pattern": None,
                "builder_origin": None,
            },
            _local_deploy=_local_deploy,
        )

        assert len(deployed) == 1, "the prebuilt tree never reached a deploy target"
        assert deployed[0][1] == str(project)
        assert doc.deployed is True
        assert doc.url.endswith(f"/{site_id}/")

    async def test_the_seam_does_not_re_enter_the_async_fork(self, beanie_test_db) -> None:
        """The worker calls back in with a prebuilt dir for an engine whose publishes are
        async. Without the ``prebuilt_project_dir is None`` guard on the fork, that callback
        would enqueue ANOTHER build — a publish that queues itself forever."""
        from pathlib import Path

        pocket_id = await _seed_pocket()
        site_id = str(sites_service._live_object_id("ws1", pocket_id))
        project = Path(tempfile.mkdtemp(prefix="paw-seam2-"))
        (project / "dist").mkdir()
        (project / "dist" / "index.html").write_text("<p>hi</p>", encoding="utf-8")

        doc = await sites_service.deploy_prebuilt_site(
            project_dir=str(project),
            deploy_inputs={
                "workspace_id": "ws1",
                "user_id": "u1",
                "pocket_id": pocket_id,
                "site_id": site_id,
                "signed_key": "k",
                "site_name": "x",
                "engine": "react",
                "pattern": None,
                "builder_origin": None,
            },
            _local_deploy=lambda sid, d: f"http://127.0.0.1/{sid}/",
        )

        # A re-enqueue would have left the row queued instead of deployed.
        assert doc.build_status != "queued"
        assert doc.deployed is True
