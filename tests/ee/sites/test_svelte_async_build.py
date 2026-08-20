# tests/ee/sites/test_svelte_async_build.py — the STATIC svelte track joins the
# ephemeral build lane.
#
# Created 2026-08-21. Until now ``service.build_runs_async`` answered ``engine ==
# "react"`` and its docstring gave the reason svelte was held back: a static svelte site
# (adapter-static) IS self-sufficient, but "which adapter ran is a property of the built
# SITE and is not knowable at enqueue time — only after the build". That premise is what
# these tests retire. It IS knowable: the svelte track's static/dynamic fork is decided by
# ``paw-sites/src/bindings.ts::parseBindings`` from ``objects``/``sources``/``actions``/
# ``auth``, which ride the publish as sibling keys on the ``source`` envelope
# (``generator_client._SVELTE_BINDING_KEYS``) and are therefore in the caller's hand
# BEFORE the queue is spent.
#
# THE THREE THINGS THAT HAD TO MOVE TOGETHER, one class each below:
#
#   1. THE GATE reads the bindings, not just the engine string. A dynamic svelte site
#      must NOT enter the lane: adapter-cloudflare's ``_worker.js`` imports
#      ``./../output/server/index.js``, which sits OUTSIDE the tarred directory, so its
#      artifact cannot execute (canary-verified 2026-08-10, and the reason ``truth_lane``
#      refuses to even preview one).
#   2. THE TAR targets the adapter that actually ran. The wrapper baked
#      ``static_output_rel("svelte")`` == ``.svelte-kit/cloudflare``; adapter-static writes
#      ``build``. Left alone, every static svelte build would tar a directory that does
#      not exist and settle as ``artifact_empty``.
#   3. THE UNPACK puts the tree where the deployer will look for it, using the SAME
#      resolved rel the tar used — one helper, so the two cannot drift.
#
# WHY A DISAGREEMENT IS SAFE RATHER THAN SILENT: if our classifier and the generator's
# ever disagree, the tar's include-list points at a directory the build did not write, so
# the artifact is EMPTY and the lane fails loudly before anything deploys. That is
# ``artifact_tar_command``'s stated design ("a wrong include-list ships NOTHING, LOUDLY")
# doing its job, not a hole.
"""The static svelte track's entry into the ephemeral build lane."""

from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from pocketpaw_ee.sites import daytona_build as db
from pocketpaw_ee.sites import engines as engines_mod
from pocketpaw_ee.sites import generator_client as gen_mod
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.faults import tar_is_available, write_project_tree

# A minimal svelte source envelope. The file map is what materializeSource writes; the
# binding keys (absent here) are what decides the adapter.
_STATIC_SOURCE: dict[str, Any] = {
    "src/routes/+page.svelte": "<h1>hi</h1>",
    "src/routes/+layout.ts": "export const prerender = true;",
}


# ---------------------------------------------------------------------------
# 1. The gate
# ---------------------------------------------------------------------------


@pytest.fixture
def svelte_lane_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the staging flag on for the tests that describe the flipped behaviour.

    A fixture rather than a module-level env write, so a test that does NOT ask for it
    observes the shipped default — which is how ``TestTheFlagIsTheDefaultOff`` below can
    assert the default at all."""
    monkeypatch.setenv("PAW_SITES_SVELTE_ASYNC_BUILD", "1")


class TestTheFlagIsTheDefaultOff:
    """The shipped default. Every svelte publish still builds inline until an operator
    turns the lane on, which is what keeps the six suites that encode the inline
    contract — the publish-time smoke gate, the native-artifact pre-warm, the
    builder-origin editability — passing untouched."""

    def test_svelte_is_inline_until_the_flag_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PAW_SITES_SVELTE_ASYNC_BUILD", raising=False)
        assert sites_service.svelte_async_build_enabled() is False
        assert sites_service.build_runs_async("svelte", source=_STATIC_SOURCE) is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_the_truthy_spellings_all_work(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("PAW_SITES_SVELTE_ASYNC_BUILD", raw)
        assert sites_service.svelte_async_build_enabled() is True

    @pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "maybe"])
    def test_anything_else_stays_off(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """Fail CLOSED on a value nobody defined. A flag that reads ``"maybe"`` as on
        turns a typo into a production routing change."""
        monkeypatch.setenv("PAW_SITES_SVELTE_ASYNC_BUILD", raw)
        assert sites_service.svelte_async_build_enabled() is False

    def test_react_does_not_read_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """react has been in the lane since SL-3 and the flag is about svelte only.
        Gating react on it would silently un-ship a shipped path."""
        monkeypatch.delenv("PAW_SITES_SVELTE_ASYNC_BUILD", raising=False)
        assert sites_service.build_runs_async("react") is True


@pytest.mark.usefixtures("svelte_lane_on")
class TestTheGateReadsTheBindingsNotJustTheEngine:
    def test_a_static_svelte_site_now_builds_asynchronously(self) -> None:
        """The point of the change. A hand-written landing site carries no binding keys,
        so the generator picks adapter-static and the artifact is a self-contained tree."""
        assert sites_service.build_runs_async("svelte", source=_STATIC_SOURCE) is True

    def test_react_is_unchanged(self) -> None:
        assert sites_service.build_runs_async("react") is True

    @pytest.mark.parametrize("engine", ["ripple", "html"])
    def test_the_engines_that_did_not_flip_stay_inline(self, engine: str) -> None:
        """html runs no build at all; ripple is adapter-cloudflare unconditionally, so its
        artifact is worker-rendered and cannot serve from a tar."""
        assert sites_service.build_runs_async(engine) is False

    def test_an_unknown_engine_stays_inline(self) -> None:
        """Unknown normalises to ripple everywhere in this codebase, and ripple is inline.
        The fallback must not flip a site whose engine we failed to recognise."""
        for value in (None, "", "nope"):
            assert sites_service.build_runs_async(value) is False

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("sources", [{"name": "posts", "object": "post", "kind": "data"}]),
            ("actions", [{"name": "addPost", "object": "post"}]),
            ("auth", True),
        ],
    )
    def test_a_dynamic_svelte_site_is_refused(self, key: str, value: Any) -> None:
        """THE regression this gate exists to prevent. A dynamic site builds on
        adapter-cloudflare, whose ``_worker.js`` imports a file outside the tarred dir —
        so queueing it would replace a working publish with one nothing can deploy.

        Mutation: drop the binding check and this ships a broken site to the edge."""
        source = dict(_STATIC_SOURCE, **{key: value})
        assert sites_service.build_runs_async("svelte", source=source) is False

    def test_the_dynamic_stamp_alone_is_enough(self) -> None:
        """``pattern == "dynamic"`` is authoritative wherever it is set (the
        create-dynamic-site tool stamps it), and it is checked even when the envelope
        carries no binding keys at all — belt to ``_is_dynamic``'s braces."""
        assert (
            sites_service.build_runs_async("svelte", source=_STATIC_SOURCE, pattern="dynamic")
            is False
        )

    def test_a_non_data_source_does_not_make_a_site_dynamic(self) -> None:
        """Mirrors ``parseBindings``, which filters ``sources`` to ``kind === 'data'``
        before counting them. A classifier that counted every entry would hold back
        static sites forever, which is the failure that looks like nothing happening."""
        source = dict(_STATIC_SOURCE, sources=[{"name": "x", "object": "y", "kind": "static"}])
        assert sites_service.build_runs_async("svelte", source=source) is True

    @pytest.mark.parametrize("truthy", ["yes", "true", 1, ["x"], {"on": True}])
    def test_auth_must_be_exactly_true_not_merely_truthy(self, truthy: Any) -> None:
        """``parseBindings`` reads ``spec.auth === true``. A Python mirror using a plain
        truthiness check diverges the moment a pocket stores the string ``"yes"`` — and it
        diverges in the direction that silently holds a static site out of the lane, which
        is indistinguishable from the feature not working.

        Mutation: ``source.get("auth")`` instead of ``source.get("auth") is True``."""
        source = dict(_STATIC_SOURCE, auth=truthy)
        assert gen_mod.svelte_source_is_dynamic(source) is False
        assert sites_service.build_runs_async("svelte", source=source) is True

    def test_empty_binding_keys_are_not_dynamic(self) -> None:
        """An envelope that carries the keys with nothing in them is a static site. The
        generator counts LENGTH, not presence, and so must this."""
        source = dict(_STATIC_SOURCE, objects=[], sources=[], actions=[], auth=False)
        assert sites_service.build_runs_async("svelte", source=source) is True

    def test_svelte_with_no_source_in_scope_answers_queued(self) -> None:
        """``cloud/surface/handlers/sites.py::_publish_runs_async`` holds a pocket engine
        and no source, and documents that it degrades toward the claim that cannot be
        false: announcing a queued build costs a click, announcing a url that is not live
        yet is a lie. The default matches that policy deliberately."""
        assert sites_service.build_runs_async("svelte") is True

    def test_every_flipped_engine_can_serve_without_a_worker(self) -> None:
        """The property behind the gate, checked against engines.py rather than restated.
        svelte answers ``None`` (either adapter is legitimate), which is exactly why the
        gate needs the bindings and the engine name is not enough."""
        assert engines_mod.expects_server_worker("react") is False
        assert engines_mod.expects_server_worker("svelte") is None
        assert engines_mod.expects_server_worker("ripple") is True


# ---------------------------------------------------------------------------
# 2. The tar
# ---------------------------------------------------------------------------


class TestTheTarTargetsTheAdapterThatActuallyRan:
    def test_the_expected_rel_follows_the_bindings(self) -> None:
        static = gen_mod.expected_static_output_rel("svelte", {"source": _STATIC_SOURCE})
        dynamic = gen_mod.expected_static_output_rel(
            "svelte", {"source": _STATIC_SOURCE, "auth": True}
        )
        assert static == "build"
        assert dynamic == ".svelte-kit/cloudflare"

    def test_a_non_svelte_engine_keeps_its_single_output_shape(self) -> None:
        assert gen_mod.expected_static_output_rel("react", {}) == "dist"
        assert gen_mod.expected_static_output_rel("ripple", {}) == ".svelte-kit/cloudflare"

    def test_the_bindings_are_read_as_flat_siblings_too(self) -> None:
        """``build_generator_input`` peels the binding keys OUT of the envelope and spreads
        them as flat siblings on the generator input (``_split_svelte_source``), so by the
        time the lane holds the payload the keys are at the top level, not under
        ``source``. Both shapes must classify the same or the tar aims at the wrong dir."""
        nested = gen_mod.expected_static_output_rel(
            "svelte", {"source": {**_STATIC_SOURCE, "auth": True}}
        )
        flat = gen_mod.expected_static_output_rel(
            "svelte", {"source": _STATIC_SOURCE, "auth": True}
        )
        assert nested == flat == ".svelte-kit/cloudflare"

    def test_the_tar_packs_the_static_output_when_told_to(self) -> None:
        cmd = db.artifact_tar_command(
            "svelte", "/home/daytona/proj", "/tmp/a.tgz", output_rel="build"
        )
        assert "/home/daytona/proj/build" in cmd
        assert ".svelte-kit" not in cmd

    def test_the_default_is_unchanged_for_every_engine(self) -> None:
        """The regression guarantee. Callers that pass no override must render the exact
        command they rendered before this parameter existed."""
        assert db.artifact_tar_command("react", "/p", "/tmp/a.tgz") == db.artifact_tar_command(
            "react", "/p", "/tmp/a.tgz", output_rel=None
        )
        assert "/p/.svelte-kit/cloudflare" in db.artifact_tar_command("svelte", "/p", "/tmp/a.tgz")

    def test_an_override_the_engine_cannot_emit_is_refused(self) -> None:
        """An include-list aimed at a directory this engine never writes packs nothing,
        and an empty artifact is indistinguishable from a build that produced nothing. Fail
        where the caller can still be blamed for it."""
        with pytest.raises(ValueError, match="cannot emit"):
            db.artifact_tar_command("react", "/p", "/tmp/a.tgz", output_rel="build")

    def test_the_project_root_is_still_refused(self) -> None:
        """html's output IS the project dir, so an include-list cannot exclude
        node_modules. Unchanged — and it must stay unreachable through the override."""
        with pytest.raises(ValueError, match="project root"):
            db.artifact_tar_command("html", "/p", "/tmp/a.tgz")

    def test_the_wrapper_records_the_rel_it_actually_tarred(self) -> None:
        """The sentinel's ``artifact_rel`` is evidence, and evidence that says
        ``.svelte-kit/cloudflare`` for a build that wrote ``build`` is worse than none."""
        script = db.build_wrapper_script(
            "svelte",
            "/p",
            timeout_seconds=600,
            artifact_path="/tmp/a.tgz",
            artifact_rel="build",
        )
        # ``shlex.quote`` leaves a bare word unquoted, so this is the literal rendering.
        assert "\nARTIFACT_REL=build\n" in script
        assert "/p/build" in script
        assert ".svelte-kit" not in script

    @pytest.mark.skipif(not tar_is_available(), reason="needs a real tar binary")
    def test_a_real_tar_packs_build_and_leaves_a_stale_adapter_tree_behind(
        self, tmp_path: Path
    ) -> None:
        """Run the real binary over a tree carrying BOTH output dirs — the shape a project
        dir has when a pre-SL-1 adapter-cloudflare build left one behind. Packing the stale
        one serves the old site forever, silently, which no unit assertion on the command
        string would catch."""
        import shlex
        import subprocess

        project = write_project_tree(
            tmp_path / "proj",
            {
                "build/index.html": b"<h1>the current build</h1>",
                "build/_app/immutable/entry.js": b"//js",
                ".svelte-kit/cloudflare/index.html": b"<h1>stale</h1>",
                "node_modules/left-pad/index.js": b"//dep",
            },
        )
        dest = str(tmp_path / "out.tgz").replace("\\", "/")
        command = db.artifact_tar_command("svelte", project, dest, output_rel="build")
        proc = subprocess.run(shlex.split(command), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"tar failed ({proc.returncode}): {proc.stderr.strip()}")
        with tarfile.open(dest) as tar:
            names = sorted(tar.getnames())
        assert "./index.html" in names
        assert not any("stale" in n or ".svelte-kit" in n for n in names)
        assert not any("node_modules" in n for n in names)


# ---------------------------------------------------------------------------
# 2b. The gate the sandbox would otherwise have lost
# ---------------------------------------------------------------------------


class TestTheWrapperKeepsTheSsrMarkerScan:
    """An inline svelte publish fails on a known workerd marker even when the build exits
    ZERO (``paw-sites/src/smoke.ts::interpretBuildOutput`` checks the markers BEFORE it
    checks the exit code). The lane runs bare ``bun install`` + ``bun run build`` and has
    no paw-sites in the sandbox, so without this the check is simply gone — and
    ``window is not defined`` from a top-level browser-only import is the classic Paw Site
    failure, not an exotic one."""

    def test_the_scan_runs_after_a_clean_build(self) -> None:
        script = db.build_wrapper_script(
            "svelte", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz", artifact_rel="build"
        )
        for marker in db.WORKERD_SSR_MARKERS:
            assert f"grep -qF -- '{marker}'" in script
        # AFTER the build, BEFORE the tar: a marker must not be discovered on a log that
        # does not exist yet, and must stop the artifact from being packed at all.
        assert script.index("bun run build") < script.index("grep -qF")
        assert script.index("grep -qF") < script.index("tar -czf")

    def test_a_hit_becomes_a_build_failure_the_user_owns(self) -> None:
        """``BUILD_EXIT=1`` rather than a new sentinel field, so ``classify_build`` routes
        it to ``build_failed`` with ``blames_user`` — which is true, and which puts the
        marker in the stderr tail the user is shown."""
        script = db.build_wrapper_script(
            "svelte", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz", artifact_rel="build"
        )
        scan = script[script.index("grep -qF") : script.index("tar -czf")]
        assert "BUILD_EXIT=1" in scan

    def test_markers_are_matched_as_fixed_strings(self) -> None:
        """``-F``. A marker is prose, and one containing a ``.`` read as a regex matches
        text it does not mean — a false build failure on a site that is fine."""
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz"
        )
        assert "grep -q " not in script
        assert script.count("grep -qF -- ") == len(db.WORKERD_SSR_MARKERS)

    def test_the_markers_match_the_inline_paths_list(self) -> None:
        """Two copies of one rule, so they are asserted equal rather than trusted. The
        inline publish reads ``generator_client._WORKERD_SSR_MARKERS``; a marker added to
        one list and not the other means the lane and the inline path disagree about what
        a broken render looks like."""
        assert set(db.WORKERD_SSR_MARKERS) == set(gen_mod._WORKERD_SSR_MARKERS)

    def test_an_empty_marker_set_renders_no_conditional(self) -> None:
        """Mutation: an empty tuple must not render a bare ``if ; then``, which is a
        syntax error that would fail every build in the lane."""
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz", ssr_markers=()
        )
        assert "grep -qF" not in script
        assert "if ; then" not in script

    @pytest.mark.skipif(not tar_is_available(), reason="needs a POSIX shell")
    def test_the_rendered_scan_is_valid_bash(self) -> None:
        """``bash -n`` over the whole script. The scan is generated shell embedded in
        generated shell, and a quoting mistake in it does not fail a unit assertion — it
        fails every build in the lane, at runtime, as an unexplained exit 2."""
        import shutil
        import subprocess

        bash = shutil.which("bash") or ""
        if not bash:
            pytest.skip("no bash on PATH")
        script = db.build_wrapper_script(
            "svelte", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz", artifact_rel="build"
        )
        proc = subprocess.run([bash, "-n"], input=script, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# 3. The unpack
# ---------------------------------------------------------------------------


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    """A gzipped tar rooted AT the output dir's contents, the shape the sandbox sends."""
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    return buf.getvalue()


class TestTheDeployUnpacksWhereTheDeployerWillLook:
    @pytest.mark.asyncio
    async def test_a_static_svelte_artifact_lands_under_build(self) -> None:
        """``deploy_prebuilt_site`` resolves its source as
        ``<project_dir>/resolve_static_output_rel(...)``, which probes ``build`` FIRST.
        Extracting under the nominal ``.svelte-kit/cloudflare`` would still be found by
        that probe today — but only by accident, and the accident inverts the moment a
        dynamic artifact reaches the same code. Put the tree where it belongs."""
        from pocketpaw_ee.sites import build_job

        seen: dict[str, Any] = {}

        async def _fake_deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> None:
            root = Path(project_dir)
            seen["rel"] = engines_mod.resolve_static_output_rel(root, "svelte")
            seen["index"] = (root / "build" / "index.html").read_bytes()

        await build_job._deploy_built_artifact(
            _tar_bytes({"index.html": b"<h1>live</h1>"}),
            engine="svelte",
            deploy_inputs={"site_id": "s1"},
            deployer=_fake_deploy,
            output_rel="build",
        )
        assert seen["rel"] == "build"
        assert seen["index"] == b"<h1>live</h1>"

    @pytest.mark.asyncio
    async def test_react_is_unchanged(self) -> None:
        from pocketpaw_ee.sites import build_job

        seen: dict[str, Any] = {}

        async def _fake_deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> None:
            seen["index"] = (Path(project_dir) / "dist" / "index.html").read_bytes()

        await build_job._deploy_built_artifact(
            _tar_bytes({"index.html": b"<h1>react</h1>"}),
            engine="react",
            deploy_inputs={"site_id": "s1"},
            deployer=_fake_deploy,
        )
        assert seen["index"] == b"<h1>react</h1>"

    @pytest.mark.asyncio
    async def test_an_artifact_carrying_a_server_entry_is_refused(self) -> None:
        """``unpack_artifact``'s skip list DROPS ``_worker.js`` — written for a preview,
        where a worker is noise. On the way to the edge it is the site. Dropping it
        silently deploys a shell that cannot start, so the lane refuses the artifact
        instead. This discharges the obligation ``_deploy_built_artifact``'s own docstring
        recorded against widening the gate past react."""
        from pocketpaw_ee.sites import build_job

        async def _never(*, project_dir: str, deploy_inputs: dict[str, Any]) -> None:
            raise AssertionError("the deploy must not be reached")

        with pytest.raises(RuntimeError, match="server entry"):
            await build_job._deploy_built_artifact(
                _tar_bytes({"index.html": b"<h1>x</h1>", "_worker.js": b"export default {}"}),
                engine="svelte",
                deploy_inputs={"site_id": "s1"},
                deployer=_never,
                output_rel="build",
            )

    @pytest.mark.asyncio
    async def test_a_chunked_worker_directory_is_refused_too(self) -> None:
        """adapter-cloudflare emits ``_worker.js`` as a DIRECTORY once an app is large
        enough (``_worker.js/chunks/0.js``). ``engines.resolve_emits_server_worker`` had to
        learn the same thing, and for the same reason: a check keyed on a FILE reports "no
        worker" for exactly the biggest sites — the ones most broken by dropping it.

        Mutation: match ``name == "_worker.js"`` instead of a path component and this
        artifact sails through, which is the failure that only shows up in production."""
        from pocketpaw_ee.sites import build_job

        async def _never(*, project_dir: str, deploy_inputs: dict[str, Any]) -> None:
            raise AssertionError("the deploy must not be reached")

        with pytest.raises(RuntimeError, match="server entry"):
            await build_job._deploy_built_artifact(
                _tar_bytes({"index.html": b"<h1>x</h1>", "_worker.js/chunks/0.js": b"//c"}),
                engine="svelte",
                deploy_inputs={"site_id": "s1"},
                deployer=_never,
                output_rel="build",
            )

    @pytest.mark.asyncio
    async def test_an_unreadable_archive_is_left_to_the_unpack_to_report(self) -> None:
        """``verify_artifact`` already rejects a corrupt archive upstream with a reason of
        its own. Re-raising here would relabel a known condition as a server-entry problem
        and send whoever reads the row to look for a worker that was never there."""
        from pocketpaw_ee.sites import build_job

        with pytest.raises(Exception) as caught:
            await build_job._deploy_built_artifact(
                b"not a tar at all",
                engine="svelte",
                deploy_inputs={"site_id": "s1"},
                deployer=None,
                output_rel="build",
            )
        assert "server entry" not in str(caught.value)


# ---------------------------------------------------------------------------
# 4. The whole job, end to end
# ---------------------------------------------------------------------------


class TestTheJobWiresThePredictionToBothEnds:
    """The direct-call tests above hand ``_deploy_built_artifact`` an ``output_rel``, so
    none of them proves ``run_site_build`` PASSES one. That is the seam where the tar and
    the unpack can silently stop agreeing, and where the harm is worst: the artifact lands
    somewhere the deploy target does not read, and a blank site replaces a working one.

    Mutation: ``output_rel=None`` at the job's deploy call, which every other test here
    survives."""

    async def _site(self):
        from pocketpaw_ee.cloud.models.site import Site

        doc = Site(workspace="ws1", pocket_id="pk1", owner="u1", name="x", build_status="queued")
        await doc.insert()
        return doc

    async def test_a_static_svelte_build_deploys_from_build_not_the_adapter_dir(
        self, beanie_test_db
    ) -> None:
        from pocketpaw_ee.sites import build_job as bj

        from tests.ee.sites.faults import FaultyDaytonaClient, clean_artifact
        from tests.ee.sites.test_async_publish import _deploy_inputs, _FakeRunner

        site = await self._site()
        captured: dict[str, Any] = {}

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            root = Path(project_dir)
            captured["under_build"] = (root / "build" / "index.html").is_file()
            captured["under_adapter"] = (root / ".svelte-kit" / "cloudflare").exists()
            captured["resolved"] = engines_mod.resolve_static_output_rel(root, "svelte")
            return None

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "svelte", "siteConfig": {}, "source": dict(_STATIC_SOURCE)},
            "svelte",
            600,
            deploy_inputs=_deploy_inputs(str(site.id)),
            _runner=_FakeRunner(),
            _client=FaultyDaytonaClient(artifact=clean_artifact()),
            _deployer=_deploy,
        )

        assert captured["under_build"] is True, "the artifact must land under adapter-static's dir"
        assert captured["under_adapter"] is False, "nothing may be written to the dynamic dir"
        assert captured["resolved"] == "build"

    async def test_the_sandbox_is_told_to_tar_the_same_directory(self, beanie_test_db) -> None:
        """The other end of the same prediction. What the sandbox EXECUTES has to pack
        ``build``; predicting ``build`` for the unpack while packing
        ``.svelte-kit/cloudflare`` produces an empty artifact rather than a wrong one —
        loud, but still a publish that cannot succeed."""
        from pocketpaw_ee.sites import build_job as bj

        from tests.ee.sites.faults import FaultyDaytonaClient, clean_artifact
        from tests.ee.sites.test_async_publish import _deploy_inputs, _FakeRunner

        class _RecordingClient(FaultyDaytonaClient):
            """``FaultyDaytonaClient`` records that exec HAPPENED, not what it ran. The
            wrapper script is the whole contract with the sandbox, so this keeps it."""

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.commands: list[str] = []

            async def execute_command(self, sandbox_id, command, timeout=30):  # type: ignore[no-untyped-def]
                self.commands.append(str(command))
                return await super().execute_command(sandbox_id, command, timeout)

        site = await self._site()
        client = _RecordingClient(artifact=clean_artifact())

        async def _deploy(*, project_dir: str, deploy_inputs: dict[str, Any]) -> Any:
            return None

        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            {"engine": "svelte", "siteConfig": {}, "source": dict(_STATIC_SOURCE)},
            "svelte",
            600,
            deploy_inputs=_deploy_inputs(str(site.id)),
            _runner=_FakeRunner(),
            _client=client,
            _deployer=_deploy,
        )

        uploaded = "".join(
            contents.decode("utf-8", "replace") if isinstance(contents, bytes) else str(contents)
            for contents, _path in client.uploaded
        )
        haystack = " ".join(client.commands) + uploaded
        assert "/build" in haystack, "the sandbox must be told to tar adapter-static's dir"
        assert ".svelte-kit/cloudflare" not in haystack
