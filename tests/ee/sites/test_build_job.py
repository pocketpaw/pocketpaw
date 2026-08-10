# tests/ee/sites/test_build_job.py — the site-build arq job and its enqueue helper
# (ee/pocketpaw_ee/sites/build_job.py).
#
# Created 2026-08-10 (SL-2 slice 2).
#
# WHAT THESE ARE FOR. The fault ladder (test_fault_ladder_build.py) proved the lane's
# CLASSIFIER: given a sentinel, who is to blame. It could not prove any of the wiring,
# because there was none — and it pinned that absence as a tripwire. This file is the
# other half: given a classification, WHAT LANDS ON THE ROW, and what happens to the row
# when the wiring itself fails.
#
# THE THREE FAILURES THAT WOULD DO REAL DAMAGE, and which these are shaped around:
#
#   1. A ROW PINNED IN FLIGHT. Every path out of the job and the enqueue must leave the
#      row terminal or genuinely in flight — never "queued" with nothing coming. A pinned
#      row makes the site permanently unpublishable with no error anywhere to see, and it
#      has already happened twice in the provision path (two bricked demo sites,
#      2026-07-31).
#   2. A ZERO-BYTE BUILD RECORDED AS ``built``. ``classification.deployable`` is derived
#      from the sentinel alone and stays True when the download delivers nothing, so a
#      caller that gated on it would replace a working site with a blank one. Gating on
#      ``BuildRunResult.ok`` is wiring contract #1 from the SG-7 findings; the test that
#      pins it is ``test_a_cleared_build_with_no_bytes_is_not_recorded_as_built``.
#   3. BUILD STDERR ON THE ROW. ``build_reason`` is surfaced to the user. A build's error
#      text is the user's own code and can carry a token pasted into a config, an absolute
#      path, or customer source. Several tests below inject a marked secret into the
#      stderr tail and assert it reaches the log and not the row.
#
# Mutations in tests/mutations/sl2_build_job.json, run and caught.

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import build_state as bs
from pocketpaw_ee.sites import engines as engines_mod

from tests.ee.sites.faults import (
    EXIT_SIGKILL,
    DaytonaUnavailable,
    FaultyDaytonaClient,
    clean_artifact,
    daytona_unconfigured,
    ok_sentinel,
    sandbox_dies_mid_build,
)

#: A marked value planted in build stderr. If this string ever appears on the Site row,
#: the lane is surfacing the user's own build output — the exact leak ``build_reason``'s
#: fixed vocabulary exists to prevent.
#:
#: DELIBERATELY NOT SHAPED LIKE A REAL CREDENTIAL, and that is a correction rather than a
#: style choice. The first version of this canary was written to look like a live
#: payment-provider key — the obvious instinct when the property under test is "a secret
#: must not leak" — and the repo's own secret scanner failed the branch, because a scanner
#: reading a diff cannot tell a test fixture from a committed key. "Unique enough to search
#: for" and "realistic" are independent properties, and only the first one is needed here.
STDERR_SECRET = "TS2304 near CANARY_MUST_NOT_PERSIST"


#: The engine input a scaffold receives. Only ``siteConfig`` matters to these tests.
def _input(**site_config: Any) -> dict[str, Any]:
    config = {"siteId": "s1", "title": "T", "captureApiBase": "https://api.test"}
    config.update(site_config)
    return {"engine": "react", "theme": {}, "siteConfig": config}


REACT_TREE = {
    "package.json": b'{"name":"site"}',
    "src/App.tsx": b"export default function App() { return <p>hi</p>; }",
}


class FakeRunner:
    """Stands in for the generator's subprocess runner (``generate`` only).

    Writes a real tree to disk, because that is what the step under test consumes: the
    job reads the scaffold off the filesystem, so a runner that returned a dict of files
    would skip the part most likely to be wrong (path shapes, pruning, bytes).

    ``project_subdir`` mirrors the real generator, which returns a ``projectDir`` NESTED
    inside the ``out`` dir it was handed. A fake that returned ``out_dir`` itself would
    let a job that read the wrong one pass.
    """

    def __init__(
        self,
        tree: dict[str, bytes] | None = None,
        *,
        raises: BaseException | None = None,
        project_subdir: str = "project",
        on_generate: Any = None,
    ) -> None:
        self.tree = REACT_TREE if tree is None else tree
        self.raises = raises
        self.project_subdir = project_subdir
        self.on_generate = on_generate
        self.inputs: list[dict[str, Any]] = []

    async def generate(self, input_json: dict[str, Any], out_dir: str) -> dict[str, Any]:
        # Deep-copied so a later mutation of the caller's dict cannot rewrite what this
        # test believes was sent.
        self.inputs.append(copy.deepcopy(input_json))
        if self.on_generate is not None:
            await self.on_generate()
        if self.raises is not None:
            raise self.raises
        project = Path(out_dir, self.project_subdir)
        for rel, contents in self.tree.items():
            target = project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        project.mkdir(parents=True, exist_ok=True)
        return {"projectDir": str(project)}


class FakePool:
    """An arq pool that records the enqueue instead of performing it."""

    def __init__(self, *, error: BaseException | None = None, refuse: bool = False) -> None:
        self.error = error
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None, **kw: Any):
        self.calls.append({"function": function, "args": args, "job_id": _job_id, "kwargs": kw})
        if self.error is not None:
            raise self.error
        # arq itself returns None when a job with the id already exists.
        return None if self.refuse else object()


async def _insert_site(**overrides: Any) -> Site:
    doc = Site(workspace="ws1", pocket_id="pk1", owner="u1", name="Test site", **overrides)
    await doc.insert()
    return doc


async def _reread(site: Site) -> Site:
    """The row as the DB holds it. Every seam writes with a targeted ``set``, so reading
    the in-memory doc would also pass if a write never reached Mongo."""
    fresh = await Site.get(site.id)
    assert fresh is not None
    return fresh


# ---------------------------------------------------------------------------
# The secret scrub
# ---------------------------------------------------------------------------


class TestTheCaptureSecretNeverLeavesThisProcess:
    """``daytona_runner``'s header records this as an obligation owed by the lane's first
    caller. A svelte scaffold substitutes the real per-site capture key into a server
    route, and a canary build found it there and in the compiled bundle; the decision that
    the exposure is acceptable rests on the key living only in a container that is then
    destroyed. Uploading it to a third-party sandbox — and, before that, writing it into a
    Redis payload — is a different question than the one this lane was cleared for.
    """

    def test_the_signed_key_is_blanked(self) -> None:
        scrubbed = bj.scrub_build_input(_input(captureSignedKey="site_key_realsecret"))
        assert scrubbed["siteConfig"]["captureSignedKey"] == ""

    def test_the_callers_input_is_not_mutated(self) -> None:
        """A publish holds the same input for its own local build, which IS entitled to
        the key. Blanking a shared dict in place would strip it from that build too."""
        original = _input(captureSignedKey="site_key_realsecret")
        bj.scrub_build_input(original)
        assert original["siteConfig"]["captureSignedKey"] == "site_key_realsecret"

    def test_everything_else_survives(self) -> None:
        """A scrub that dropped the rest of siteConfig would break every build instead of
        leaking one key — the failure mode that gets a safety measure reverted."""
        scrubbed = bj.scrub_build_input(_input(captureSignedKey="k", d1DatabaseId="d1-uuid"))
        assert scrubbed["siteConfig"]["siteId"] == "s1"
        assert scrubbed["siteConfig"]["d1DatabaseId"] == "d1-uuid"
        assert scrubbed["engine"] == "react"

    def test_an_input_with_no_site_config_is_left_alone(self) -> None:
        """It must not INVENT a blank key on an input that never had a siteConfig — that
        would change the generator payload for a shape this lane does not own."""
        assert bj.scrub_build_input({"engine": "react"}) == {"engine": "react"}

    async def test_the_job_scrubs_too_not_only_the_enqueue(self, beanie_test_db) -> None:
        """Scrubbing only at the enqueue would mean a direct caller — a test, or a future
        re-drive of a stored payload — puts the key in a sandbox."""
        site = await _insert_site()
        runner = FakeRunner()
        client = FaultyDaytonaClient(artifact=clean_artifact())
        await bj.run_site_build(
            {},
            "ws1",
            str(site.id),
            _input(captureSignedKey="site_key_realsecret"),
            "react",
            600,
            _runner=runner,
            _client=client,
        )
        assert runner.inputs[0]["siteConfig"]["captureSignedKey"] == ""

    async def test_the_key_never_enters_the_arq_payload(self, beanie_test_db) -> None:
        site = await _insert_site()
        pool = FakePool()
        await bj.enqueue_site_build(
            site,
            engine="react",
            generator_input=_input(captureSignedKey="site_key_realsecret"),
            _pool_override=pool,
        )
        payload = pool.calls[0]["args"]
        assert "site_key_realsecret" not in repr(payload)


# ---------------------------------------------------------------------------
# Reading the scaffold
# ---------------------------------------------------------------------------


class TestReadingTheScaffold:
    def test_paths_are_posix_relative(self, tmp_path) -> None:
        """The keys are joined onto the sandbox's POSIX project dir. A Windows-separated
        key lands as one file with backslashes in its name and the build then fails
        looking for a directory nobody created."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "App.tsx").write_bytes(b"x")
        assert bj.read_generated_tree(str(tmp_path)) == {"src/App.tsx": b"x"}

    def test_node_modules_is_pruned(self, tmp_path) -> None:
        """The 500 MB-on-the-wire path. The sandbox installs its own dependencies, so a
        cached tree on our disk is pure upload cost — and this lane deliberately uses a
        throwaway dir precisely so there should be none to find."""
        (tmp_path / "node_modules" / "react").mkdir(parents=True)
        (tmp_path / "node_modules" / "react" / "index.js").write_bytes(b"x")
        (tmp_path / "index.html").write_bytes(b"hi")
        assert bj.read_generated_tree(str(tmp_path)) == {"index.html": b"hi"}

    def test_a_nested_node_modules_is_pruned_too(self, tmp_path) -> None:
        (tmp_path / "packages" / "ui" / "node_modules").mkdir(parents=True)
        (tmp_path / "packages" / "ui" / "node_modules" / "dep.js").write_bytes(b"x")
        (tmp_path / "packages" / "ui" / "index.ts").write_bytes(b"ok")
        assert bj.read_generated_tree(str(tmp_path)) == {"packages/ui/index.ts": b"ok"}

    def test_git_is_pruned(self, tmp_path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_bytes(b"[core]")
        (tmp_path / "index.html").write_bytes(b"hi")
        assert bj.read_generated_tree(str(tmp_path)) == {"index.html": b"hi"}

    def test_binary_members_survive_unchanged(self, tmp_path) -> None:
        """A scaffold carries binaries (an imported site's assets, a lockfile). Decoding
        would only create a way to fail on a file nothing needs to read."""
        payload = bytes(range(256))
        (tmp_path / "bun.lockb").write_bytes(payload)
        assert bj.read_generated_tree(str(tmp_path))["bun.lockb"] == payload

    def test_an_empty_tree_reads_as_empty(self, tmp_path) -> None:
        """Not vacuous: the job treats an empty read as a rung of its own, so this is the
        input that has to be distinguishable from a healthy scaffold."""
        assert bj.read_generated_tree(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# Engine gating
# ---------------------------------------------------------------------------


class TestOnlyABuildableEngineReachesASandbox:
    @pytest.mark.parametrize("engine", ["ripple", "svelte", "react"])
    def test_the_build_engines_are_buildable(self, engine: str) -> None:
        assert bj.is_buildable_engine(engine) is True

    def test_html_is_not(self) -> None:
        """html needs no build AND its output IS the project root, so an include-list
        cannot exclude node_modules from it — ``artifact_tar_command`` refuses it
        outright."""
        assert bj.is_buildable_engine("html") is False

    def test_the_engine_list_has_not_drifted(self) -> None:
        """A DRIFT GUARD, not a restatement. ``BUILDABLE_ENGINES`` is hand-written because
        engines.py exposes no roster, so it can silently miss a new engine — and a missing
        entry does not fail loudly: it just sizes the arq timeout off the wrong set of
        budgets. Derive the same set from engines.py and compare."""
        derived = {
            engine
            for engine in engines_mod._STATIC_OUTPUT_REL
            if engines_mod.needs_node_build(engine) and engines_mod.static_output_rel(engine) != "."
        }
        assert set(bj.BUILDABLE_ENGINES) == derived


# ---------------------------------------------------------------------------
# The arq function timeout
# ---------------------------------------------------------------------------


class TestTheJobCarriesItsOwnTimeout:
    """The decisive argument for a dedicated arq function over the workspace-jobs
    registry. If the job's budget is shorter than the sandbox's, arq cancels the job
    before the in-sandbox ``timeout(1)`` fires — so no sentinel is written and a
    healthy-but-slow build is recorded as lost infrastructure.
    """

    def test_it_exceeds_the_widest_in_sandbox_budget_plus_the_exec_slack(self) -> None:
        from pocketpaw_ee.sites.daytona_build import resolve_build_timeout_seconds
        from pocketpaw_ee.sites.daytona_runner import EXEC_TIMEOUT_SLACK_SECONDS

        widest = max(resolve_build_timeout_seconds(e) for e in bj.BUILDABLE_ENGINES)
        assert bj.site_build_job_timeout_seconds() > widest + EXEC_TIMEOUT_SLACK_SECONDS

    def test_it_does_not_fit_the_shared_workspace_jobs_budget(self) -> None:
        """The measurement behind the routing decision, asserted rather than asserted-in-
        prose: at today's defaults a build needs MORE than the registry's shared timeout,
        so riding the registry would clip a maximal healthy build today — not merely after
        some future retune."""
        from pocketpaw_ee.cloud.jobs.domain import job_timeout_seconds

        assert bj.site_build_job_timeout_seconds() > job_timeout_seconds()

    def test_it_follows_a_per_engine_retune(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The coupling that a shared registry timeout cannot provide: lengthening one
        engine's build budget lengthens the job's."""
        before = bj.site_build_job_timeout_seconds()
        monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC_SVELTE", "1800")
        assert bj.site_build_job_timeout_seconds() == before + (1800 - 600)

    def test_the_worker_registers_it_under_its_own_name_and_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registration name that drifts from the enqueue name produces a job that sits
        in Redis forever with no worker willing to claim it, and no error anywhere."""
        monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")
        from pocketpaw_ee.cloud.chat.runs import worker as worker_mod

        registered = {f.name: f for f in worker_mod.WorkerSettings.functions if hasattr(f, "name")}
        assert bj.ARQ_FUNCTION_NAME in registered
        site_build = registered[bj.ARQ_FUNCTION_NAME]
        assert site_build.coroutine is bj.run_site_build
        assert site_build.timeout_s == bj.site_build_job_timeout_seconds()
        assert site_build.timeout_s != registered["execute_workspace_job"].timeout_s
        # A build is billed per attempt in a third-party sandbox, and the retry decision
        # belongs to ``settle`` (which records why it gave up), not to a silent arq retry
        # of a job whose row already reads ``failed``.
        assert site_build.max_tries == 1


# ---------------------------------------------------------------------------
# Settling a finished attempt
# ---------------------------------------------------------------------------


def _result(**kw: Any):
    """A ``BuildRunResult`` built straight from a classification, with no sandbox."""
    from pocketpaw_ee.sites.daytona_build import BuildClassification
    from pocketpaw_ee.sites.daytona_runner import BuildRunResult, BuildTimings

    classification = BuildClassification(
        outcome=kw.pop("outcome"),
        reason=kw.pop("reason"),
        retryable=kw.pop("retryable", False),
        blames_user=kw.pop("blames_user", False),
        stderr_tail=kw.pop("stderr_tail", ""),
    )
    artifact_bytes = kw.pop("artifact_bytes", 0)
    return BuildRunResult(
        classification=classification,
        timings=BuildTimings(0.0, 0.0, 0.0, 0.0, 0.0),
        artifact=b"x" * artifact_bytes if artifact_bytes else None,
        artifact_bytes=artifact_bytes,
        sandbox_id="sb-1",
        sandbox_deleted=True,
    )


class TestSettlingAFinishedAttempt:
    def test_a_healthy_build_settles_as_built(self) -> None:
        got = bj.resolve_build_settlement(
            _result(outcome="completed_ok", reason="ok", artifact_bytes=64)
        )
        assert got.status == "built"
        assert got.reason == "completed_ok:ok"

    def test_a_user_build_failure_settles_as_failed_and_names_its_cause(self) -> None:
        got = bj.resolve_build_settlement(
            _result(outcome="build_failed", reason="install_failed", blames_user=True)
        )
        assert got.status == "failed"
        assert got.reason == "build_failed:install_failed"

    def test_infrastructure_loss_is_distinguishable_from_the_users_build(self) -> None:
        """The whole point of the field. Both settle as ``failed`` today, so the ROW's
        status cannot tell an operator which happened — only the rung can, and the two
        need opposite handling."""
        user = bj.resolve_build_settlement(
            _result(outcome="build_failed", reason="build_failed", blames_user=True)
        )
        ours = bj.resolve_build_settlement(
            _result(outcome="infra_lost", reason="no_sentinel_before_timeout", retryable=True)
        )
        assert user.status == ours.status == "failed"
        assert user.reason.split(":")[0] == "build_failed"
        assert ours.reason.split(":")[0] == "infra_lost"

    def test_a_cleared_build_with_no_bytes_is_not_recorded_as_built(self) -> None:
        """WIRING CONTRACT #1, and the trap it was written for. The sentinel promised
        bytes and the download delivered none: ``deployable`` still reads True while ``ok``
        reads False. Settling that as ``built`` hands a zero-byte artifact to the deploy
        and replaces a working site with a blank one."""
        result = _result(outcome="completed_ok", reason="ok", artifact_bytes=0)
        assert result.classification.deployable is True
        assert result.ok is False
        got = bj.resolve_build_settlement(result)
        assert got.status == "failed"
        assert got.reason == "artifact_missing:download_delivered_no_bytes"

    def test_a_missing_artifact_is_retryable_unlike_a_cleared_build(self) -> None:
        """It gets its OWN rung rather than borrowing ``completed_ok`` because the two
        differ on retryability: a lost transfer is worth another attempt, and a rung that
        lies about that either burns a publish a retry would have fixed or retries
        something no attempt can."""
        got = bj.resolve_build_settlement(
            _result(outcome="completed_ok", reason="ok", artifact_bytes=0), attempts_left=2
        )
        assert got.status is None
        assert got.reason.startswith("artifact_missing:")

    def test_a_retryable_rung_with_attempts_left_stays_in_flight(self) -> None:
        got = bj.resolve_build_settlement(
            _result(outcome="infra_lost", reason="no_sentinel_before_timeout", retryable=True),
            attempts_left=1,
        )
        assert got.status is None

    def test_a_retryable_rung_with_no_attempts_left_settles(self) -> None:
        """Today's real path: no attempt loop exists, so every enqueue passes 0."""
        got = bj.resolve_build_settlement(
            _result(outcome="timed_out", reason="build_exceeded_in_sandbox_timeout", retryable=True)
        )
        assert got.status == "failed"

    def test_a_settled_status_is_always_terminal(self) -> None:
        """The invariant that keeps a finished build from blocking the next publish."""
        for outcome, reason in (
            ("completed_ok", "ok"),
            ("build_failed", "build_failed"),
            ("timed_out", "install_exceeded_in_sandbox_timeout"),
            ("infra_lost", "sentinel_missing_exit_code"),
        ):
            got = bj.resolve_build_settlement(
                _result(outcome=outcome, reason=reason, artifact_bytes=32)
            )
            assert got.status in bs.TERMINAL_STATUSES, (outcome, got)

    def test_the_reason_never_carries_build_stderr(self) -> None:
        """The security property. ``build_reason`` is user-surfaceable; a build's stderr is
        the user's own code and can carry a token pasted into a config."""
        got = bj.resolve_build_settlement(
            _result(
                outcome="build_failed",
                reason="build_failed",
                blames_user=True,
                stderr_tail=STDERR_SECRET,
            )
        )
        assert STDERR_SECRET not in got.reason
        # Also on a FRAGMENT, so a reason that truncated the tail rather than dropping it
        # still fails — a partial leak is a leak.
        assert "CANARY" not in got.reason

    def test_every_reason_is_a_greppable_identifier(self) -> None:
        """Same property F7 asserts over the classifier's reasons, restated over the
        composed ones: these ride logs, metrics and (eventually) a UI, so a reason with
        spaces or capitals becomes a dashboard nobody can group by."""
        composed = [
            bj.resolve_build_settlement(_result(outcome=o, reason=r, artifact_bytes=b)).reason
            for o, r, b in (
                ("completed_ok", "ok", 8),
                ("completed_ok", "ok", 0),
                ("build_failed", "install_failed", 0),
                ("infra_lost", "install_killed_by_signal_137", 0),
            )
        ]
        for reason in composed:
            rung, _, cause = reason.partition(":")
            assert rung and cause, reason
            assert reason.replace("_", "").replace(":", "").isalnum(), reason
            assert reason == reason.lower(), reason


# ---------------------------------------------------------------------------
# The job, end to end against a fake sandbox
# ---------------------------------------------------------------------------


async def _run_job(
    site: Site,
    *,
    engine: str = "react",
    runner: FakeRunner | None = None,
    client: Any = None,
    generator_input: dict[str, Any] | None = None,
    timeout_seconds: int = 600,
) -> None:
    await bj.run_site_build(
        {},
        site.workspace,
        str(site.id),
        generator_input or _input(),
        engine,
        timeout_seconds,
        _runner=runner or FakeRunner(),
        _client=client if client is not None else FaultyDaytonaClient(artifact=clean_artifact()),
    )


class TestTheJobRecordsWhatHappened:
    async def test_a_healthy_build_lands_as_built(self, beanie_test_db) -> None:
        site = await _insert_site()
        await _run_job(site)
        fresh = await _reread(site)
        assert fresh.build_status == "built"
        assert fresh.build_reason == "completed_ok:ok"

    async def test_a_finished_build_never_blocks_the_next_publish(self, beanie_test_db) -> None:
        """A build is the thing a site does many times. Whatever a build records, the
        guard must read the row as free afterwards."""
        site = await _insert_site()
        await _run_job(site)
        assert bs.should_enqueue(await _reread(site), 600) is True

    async def test_the_row_says_building_while_the_build_runs(self, beanie_test_db) -> None:
        """``queued`` exists to separate "waiting behind the cap" from "running", and that
        distinction is worth nothing unless something actually flips the row. Observed
        from inside the scaffold step, i.e. after the job was consumed."""
        site = await _insert_site(build_status="queued", build_started_at=datetime.now(UTC))
        seen: list[str] = []

        async def _peek() -> None:
            seen.append((await _reread(site)).build_status)

        await _run_job(site, runner=FakeRunner(on_generate=_peek))
        assert seen == ["building"]

    async def test_the_build_clock_is_restamped_when_the_job_starts(self, beanie_test_db) -> None:
        """Otherwise the attempt's staleness window is spent on queue wait, and a build
        that waited behind the cap can be declared stale while it is still running — then
        re-enqueued on top of itself, which is the expensive direction."""
        queued_at = datetime.now(UTC) - timedelta(minutes=30)
        site = await _insert_site(build_status="queued", build_started_at=queued_at)
        stamps: list[Any] = []

        async def _peek() -> None:
            stamps.append((await _reread(site)).build_started_at)

        await _run_job(site, runner=FakeRunner(on_generate=_peek))
        restamped = stamps[0]
        assert restamped is not None
        assert restamped.replace(tzinfo=UTC) > queued_at

    async def test_a_users_broken_build_is_recorded_with_its_rung_not_its_stderr(
        self, beanie_test_db
    ) -> None:
        site = await _insert_site()
        client = FaultyDaytonaClient(sentinel=ok_sentinel(build_exit=1, stderr_tail=STDERR_SECRET))
        await _run_job(site, client=client)
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        # The doubled name is the honest cost of one uniform shape: the classifier's rung
        # and its cause happen to coincide for a plain non-zero build.
        assert fresh.build_reason == "build_failed:build_failed"
        assert STDERR_SECRET not in (fresh.build_reason or "")

    async def test_a_lost_sandbox_is_recorded_as_ours_not_the_users(self, beanie_test_db) -> None:
        site = await _insert_site()
        await _run_job(site, client=sandbox_dies_mid_build(), timeout_seconds=100_000)
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        assert (fresh.build_reason or "").startswith("infra_lost:")

    async def test_an_oom_kill_is_recorded_as_infrastructure(self, beanie_test_db) -> None:
        """A signalled death still runs the trap, so it arrives WITH a sentinel and a
        non-zero exit — the shape of a genuine failure. The row must not blame the user."""
        site = await _insert_site()
        client = FaultyDaytonaClient(sentinel=ok_sentinel(build_exit=EXIT_SIGKILL))
        await _run_job(site, client=client)
        fresh = await _reread(site)
        assert fresh.build_reason == f"infra_lost:build_killed_by_signal_{EXIT_SIGKILL}"

    async def test_a_cleared_build_with_no_bytes_is_not_recorded_as_built(
        self, beanie_test_db
    ) -> None:
        """The end-to-end form of wiring contract #1: the sandbox reports success and the
        download delivers nothing.

        The RUNG CHANGED on 2026-08-11 and the property did not. ``run_build`` now verifies
        the downloaded bytes and demotes the classification itself, so this arrives already
        named ``infra_lost:artifact_empty`` rather than reaching
        ``resolve_build_settlement``'s ``artifact_missing`` fallback. Both are terminal,
        retryable and not the user's fault; the new one is more precise, since it comes from
        the code that looked at the bytes."""
        site = await _insert_site()
        await _run_job(site, client=FaultyDaytonaClient(artifact=b""))
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        assert fresh.build_reason == "infra_lost:artifact_empty"

    async def test_an_unbuildable_engine_is_refused_before_any_sandbox_exists(
        self, beanie_test_db
    ) -> None:
        """A routing bug, and spending a sandbox to discover it bills for a mistake that
        was knowable from the payload."""
        site = await _insert_site()
        client = FaultyDaytonaClient()
        runner = FakeRunner()
        await _run_job(site, engine="html", runner=runner, client=client)
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        assert fresh.build_reason == "engine_not_buildable:html"
        assert client.calls == []
        assert runner.inputs == []

    async def test_a_failed_scaffold_costs_no_sandbox(self, beanie_test_db) -> None:
        site = await _insert_site()
        client = FaultyDaytonaClient()
        runner = FakeRunner(raises=RuntimeError(f"generator failed: {STDERR_SECRET}"))
        await _run_job(site, runner=runner, client=client)
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        assert fresh.build_reason == "scaffold_failed:generator_raised"
        assert client.calls == []
        # The generator's own stderr names paths and carries the user's content.
        assert STDERR_SECRET not in (fresh.build_reason or "")

    async def test_an_empty_scaffold_costs_no_sandbox(self, beanie_test_db) -> None:
        """The empty-deploy failure caught one step earlier than ``artifact_empty`` catches
        it — before there is a sandbox to pay for."""
        site = await _insert_site()
        client = FaultyDaytonaClient()
        await _run_job(site, runner=FakeRunner(tree={}), client=client)
        fresh = await _reread(site)
        assert fresh.build_reason == "scaffold_empty:no_files_generated"
        assert client.calls == []

    async def test_an_unreachable_daytona_is_recorded_and_re_raised(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1's publish half, now provable. Daytona unconfigured must not read as the
        user's site being broken, and the row must not be left mid-flight."""
        site = await _insert_site()
        daytona_unconfigured(monkeypatch)
        with pytest.raises(RuntimeError, match="not configured"):
            await bj.run_site_build(
                {}, "ws1", str(site.id), _input(), "react", 600, _runner=FakeRunner()
            )
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        assert fresh.build_reason == "sandbox_unavailable:run_build_raised"
        assert bs.should_enqueue(fresh, 600) is True

    async def test_a_sandbox_that_cannot_be_created_leaves_a_republishable_row(
        self, beanie_test_db
    ) -> None:
        site = await _insert_site()
        with pytest.raises(DaytonaUnavailable):
            await _run_job(site, client=FaultyDaytonaClient(fail_at="create"))
        fresh = await _reread(site)
        assert fresh.build_reason == "sandbox_unavailable:run_build_raised"
        assert bs.should_enqueue(fresh, 600) is True

    async def test_a_missing_site_is_a_no_op(self, beanie_test_db) -> None:
        """The site was deleted, or the id is bogus. There is nothing to record on, and
        raising would only turn a deleted site into a worker error."""
        from bson import ObjectId

        client = FaultyDaytonaClient()
        await bj.run_site_build(
            {}, "ws1", str(ObjectId()), _input(), "react", 600, _runner=FakeRunner(), _client=client
        )
        assert client.calls == []

    async def test_another_workspaces_id_is_not_touched(self, beanie_test_db) -> None:
        """The job is handed an id. A read that ignored the workspace would let a bad
        payload move another tenant's row."""
        site = await _insert_site()
        client = FaultyDaytonaClient()
        await bj.run_site_build(
            {},
            "other-ws",
            str(site.id),
            _input(),
            "react",
            600,
            _runner=FakeRunner(),
            _client=client,
        )
        fresh = await _reread(site)
        assert fresh.build_status == "none"
        assert client.calls == []

    async def test_staying_in_flight_writes_no_status(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``settle`` returns None to mean "keep this attempt in flight", and writing a
        terminal status there is the bug that optional exists to prevent: ANY terminal
        status reads to ``should_enqueue`` as free to re-publish, so a transient ``failed``
        between attempts invites a second sandbox on top of the retry.

        Forced through the seam because no attempt loop exists yet to reach it naturally —
        the branch would otherwise be untested code waiting for the day it matters.
        """
        site = await _insert_site()
        monkeypatch.setattr(
            bj,
            "resolve_build_settlement",
            lambda result, attempts_left=0: bj.BuildSettlement(None, "infra_lost:pretend"),
        )
        await _run_job(site, client=sandbox_dies_mid_build(), timeout_seconds=100_000)
        fresh = await _reread(site)
        assert fresh.build_status == "building"
        assert fresh.build_reason is None
        assert bs.is_in_flight(fresh) is True

    async def test_the_scaffolded_tree_is_what_gets_uploaded(self, beanie_test_db) -> None:
        """The seam between the two halves, asserted on EXACT remote paths.

        A job that uploaded the wrong directory — the ``out`` dir rather than the
        ``projectDir`` the generator returns — would still build something, just one level
        up, with every source file a directory deeper than the build config expects. A
        suffix match would not notice: ``.../paw-build/project/src/App.tsx`` also ends with
        ``/src/App.tsx``.
        """
        from pocketpaw_ee.sites.daytona_runner import SANDBOX_PROJECT_DIR

        site = await _insert_site()
        client = FaultyDaytonaClient(artifact=clean_artifact())
        await _run_job(site, client=client)
        remote_paths = [remote for _, remote in client.uploaded]
        assert f"{SANDBOX_PROJECT_DIR}/src/App.tsx" in remote_paths, remote_paths
        assert f"{SANDBOX_PROJECT_DIR}/package.json" in remote_paths, remote_paths

    async def test_the_sandbox_is_held_to_the_budget_it_was_enqueued_with(
        self, beanie_test_db
    ) -> None:
        """The timeout travels in the payload rather than being re-derived, so the window
        the guard measures and the budget the sandbox gets are one number decided once."""
        from pocketpaw_ee.sites.daytona_runner import EXEC_TIMEOUT_SLACK_SECONDS

        site = await _insert_site()
        client = FaultyDaytonaClient(artifact=clean_artifact())
        await _run_job(site, client=client, timeout_seconds=900)
        assert client.exec_timeout == 900 + EXEC_TIMEOUT_SLACK_SECONDS


# ---------------------------------------------------------------------------
# The enqueue
# ---------------------------------------------------------------------------


class TestEnqueueingABuild:
    async def test_it_stamps_the_row_and_enqueues(self, beanie_test_db) -> None:
        site = await _insert_site()
        pool = FakePool()
        job_id = await bj.enqueue_site_build(
            site, engine="react", generator_input=_input(), _pool_override=pool
        )
        fresh = await _reread(site)
        assert fresh.build_status == "queued"
        assert fresh.build_started_at is not None
        assert fresh.build_job_id == job_id
        call = pool.calls[0]
        assert call["function"] == bj.ARQ_FUNCTION_NAME
        assert call["job_id"] == job_id
        assert call["args"][0] == "ws1"
        assert call["args"][1] == str(site.id)

    async def test_the_polling_handle_is_persisted_not_transient(self, beanie_test_db) -> None:
        """A queued build is exactly when a user reloads. DP0-4's job id is a transient
        PrivateAttr, which is gone on reload — the client loses its handle at the moment
        the wait is longest."""
        site = await _insert_site()
        job_id = await bj.enqueue_site_build(
            site, engine="react", generator_input=_input(), _pool_override=FakePool()
        )
        assert (await _reread(site)).build_job_id == job_id

    async def test_the_row_is_stamped_before_the_enqueue(self, beanie_test_db) -> None:
        """Order matters in one direction only. A worker that claimed the job first would
        write a terminal status, and a stamp landing afterwards would pin a FINISHED build
        in ``queued`` forever."""
        site = await _insert_site()
        observed: list[str] = []

        class _WatchingPool(FakePool):
            async def enqueue_job(self, function, *args, _job_id=None, **kw):
                observed.append((await _reread(site)).build_status)
                return await super().enqueue_job(function, *args, _job_id=_job_id, **kw)

        await bj.enqueue_site_build(
            site, engine="react", generator_input=_input(), _pool_override=_WatchingPool()
        )
        assert observed == ["queued"]

    async def test_a_live_build_is_not_enqueued_twice(self, beanie_test_db) -> None:
        """Single flight: two publishes of one site must not open two sandboxes and race
        on which artifact deploys."""
        site = await _insert_site(build_status="building", build_started_at=datetime.now(UTC))
        pool = FakePool()
        assert (
            await bj.enqueue_site_build(
                site, engine="react", generator_input=_input(), _pool_override=pool
            )
            is None
        )
        assert pool.calls == []

    async def test_a_stale_in_flight_row_is_re_enqueued(self, beanie_test_db) -> None:
        """The recovery half. A job no worker ever consumed leaves exactly this row, and
        the derived window is the only thing that unsticks it."""
        stale = datetime.now(UTC) - bs.stale_after(600) - timedelta(seconds=1)
        site = await _insert_site(build_status="queued", build_started_at=stale)
        pool = FakePool()
        assert (
            await bj.enqueue_site_build(
                site, engine="react", generator_input=_input(), _pool_override=pool
            )
            is not None
        )
        assert len(pool.calls) == 1

    async def test_a_failed_enqueue_does_not_pin_the_row_in_queued(self, beanie_test_db) -> None:
        """F3's enqueue injection, now real. Redis is down: the row was already stamped
        ``queued``, and if it stays there ``should_enqueue`` no-ops EVERY later publish of
        this site until the staleness window lapses — a site nobody can publish, with the
        error already swallowed by the caller's 5xx."""
        site = await _insert_site()
        pool = FakePool(error=RuntimeError("redis is down"))
        with pytest.raises(RuntimeError, match="redis is down"):
            await bj.enqueue_site_build(
                site, engine="react", generator_input=_input(), _pool_override=pool
            )
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        assert fresh.build_reason == "enqueue_failed:pool_or_enqueue_raised"
        assert bs.should_enqueue(fresh, 600) is True

    async def test_an_arq_refusal_is_treated_as_a_failed_enqueue(self, beanie_test_db) -> None:
        """arq returns None rather than raising when the job id already exists. Reading
        that as success leaves the row queued for a job nobody will run."""
        site = await _insert_site()
        with pytest.raises(RuntimeError, match="refused"):
            await bj.enqueue_site_build(
                site,
                engine="react",
                generator_input=_input(),
                _pool_override=FakePool(refuse=True),
            )
        fresh = await _reread(site)
        assert fresh.build_status == "failed"
        assert bs.should_enqueue(fresh, 600) is True

    async def test_a_rollback_failure_does_not_mask_the_enqueue_failure(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller acts on the enqueue error. A second error from the best-effort
        rollback would replace it with something less informative."""
        site = await _insert_site()
        from pocketpaw_ee.sites import service as sites_service

        async def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("mongo is down too")

        monkeypatch.setattr(sites_service, "record_build_outcome", _boom)
        with pytest.raises(RuntimeError, match="redis is down"):
            await bj.enqueue_site_build(
                site,
                engine="react",
                generator_input=_input(),
                _pool_override=FakePool(error=RuntimeError("redis is down")),
            )

    async def test_each_enqueue_mints_a_fresh_job_id(self, beanie_test_db) -> None:
        """Deliberately NOT deterministic per site. arq refuses an id that still has a
        RESULT in Redis (an hour by default), so a stable id would silently refuse every
        rebuild for an hour — a single-flight guard nobody asked for, in the wrong layer,
        and invisible because the refusal is a ``None`` return."""
        site = await _insert_site()
        pool = FakePool()
        first = await bj.enqueue_site_build(
            site, engine="react", generator_input=_input(), _pool_override=pool
        )
        await site.set({"build_status": "none", "build_started_at": None})
        second = await bj.enqueue_site_build(
            await _reread(site), engine="react", generator_input=_input(), _pool_override=pool
        )
        assert first != second

    async def test_the_budget_defaults_to_the_engines_resolved_timeout(
        self, beanie_test_db
    ) -> None:
        from pocketpaw_ee.sites.daytona_build import resolve_build_timeout_seconds

        site = await _insert_site()
        pool = FakePool()
        await bj.enqueue_site_build(
            site, engine="svelte", generator_input=_input(), _pool_override=pool
        )
        assert pool.calls[0]["args"][4] == resolve_build_timeout_seconds("svelte")

    async def test_a_new_attempt_clears_the_previous_rung(self, beanie_test_db) -> None:
        """A queued build showing the last attempt's failure reason is a row that lies to
        whoever is watching it."""
        site = await _insert_site(build_status="failed", build_reason="build_failed:build_failed")
        await bj.enqueue_site_build(
            site, engine="react", generator_input=_input(), _pool_override=FakePool()
        )
        assert (await _reread(site)).build_reason is None


class TestTheEnqueueNeedsRedisConfigured:
    """An unset ``POCKETPAW_REDIS_URL`` must refuse, not improvise.

    The worker's own ``_redis_settings`` learned this the hard way (review finding #4): a
    silent fallback to localhost split-brained a typoed production deploy, because the web
    process cheerfully enqueued into a Redis no worker was reading. The same fallback here
    would queue every build into nowhere and leave every row waiting on a job that does not
    exist.
    """

    async def test_an_unset_redis_url_refuses_and_frees_the_row(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
        bj._reset_for_tests()
        try:
            site = await _insert_site()
            with pytest.raises(RuntimeError, match="POCKETPAW_REDIS_URL"):
                await bj.enqueue_site_build(site, engine="react", generator_input=_input())
            # The row was stamped before the pool was reached, so the rollback has to cover
            # a failure that happened before any job could possibly exist.
            fresh = await _reread(site)
            assert bs.should_enqueue(fresh, 600) is True
            assert fresh.build_reason == "enqueue_failed:pool_or_enqueue_raised"
        finally:
            # A cached pool would outlive this test and be handed to every later enqueue.
            bj._reset_for_tests()


class TestTheBuildLaneCannotRollBackAConcurrentPublish:
    """Why the four seams use a targeted ``set`` and not ``save()``.

    A build runs for minutes. A publish of the same site can land in the middle of one and
    write ``url`` / ``deployed`` / ``name``. The build job holds a doc it loaded BEFORE that
    publish, so a full ``save()`` would write its whole stale snapshot back and silently
    revert the publish — a live site pointed at an old URL, with nothing in any log saying
    so, because from Mongo's side it is just a write.
    """

    async def test_recording_an_outcome_does_not_revert_a_concurrent_write(
        self, beanie_test_db
    ) -> None:
        from pocketpaw_ee.sites import service as sites_service

        site = await _insert_site()
        stale = await Site.get(site.id)  # what a running build is holding
        assert stale is not None

        # A publish lands while the build is in the sandbox.
        await site.set({"url": "https://live.example", "deployed": True})

        await sites_service.record_build_outcome(stale, status="built", reason="completed_ok:ok")

        fresh = await _reread(site)
        assert fresh.build_status == "built"
        assert fresh.url == "https://live.example"
        assert fresh.deployed is True

    async def test_marking_a_build_running_does_not_revert_one_either(self, beanie_test_db) -> None:
        """The same hazard on the other write — and the likelier one, since the queued
        window is exactly when a user hits publish again."""
        from pocketpaw_ee.sites import service as sites_service

        site = await _insert_site()
        stale = await Site.get(site.id)
        assert stale is not None
        await site.set({"name": "Renamed by the owner"})

        await sites_service.mark_build_running(stale)

        fresh = await _reread(site)
        assert fresh.build_status == "building"
        assert fresh.name == "Renamed by the owner"
