# tests/ee/sites/test_daytona_build.py — unit tests for the ephemeral Daytona build
# lane's decision core (ee/pocketpaw_ee/sites/daytona_build.py).
#
# Created 2026-08-09 (SG-9i). The module under test is the replacement for a failure
# discriminator the captain's ruling made impossible: a self-deleting sandbox cannot be
# re-queried after the fact, so the build must PROVE it completed and absence of proof
# is read as infrastructure loss.
#
# WHAT THESE TESTS ARE PROTECTING, stated plainly because it is the point of the whole
# module: the lane must never tell a user "your site failed to build" when what
# actually happened is that we lost the container. Every test below that asserts
# ``blames_user is False`` is guarding that specific mis-report, and the
# signalled-death cases (137/143) are the subtle ones — a signalled process still runs
# the shell trap, so it arrives WITH a sentinel and a non-zero exit, which is exactly
# what a genuine build failure looks like.
#
# Mutations that break these tests live in tests/mutations/daytona_build.json and have
# been run (per the workspace rule that a gate is not a gate until a mutation has been
# observed to break it).

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.sites import daytona_build as db


def _sentinel(**overrides: object) -> dict[str, object]:
    """A well-formed, successful sentinel. Tests override single fields so each case
    differs from the happy path by exactly one thing."""
    payload: dict[str, object] = {
        "schema": db.SENTINEL_SCHEMA,
        "engine": "react",
        "install_exit": 0,
        "build_exit": 0,
        "started_at": "2026-08-09T00:00:00Z",
        "finished_at": "2026-08-09T00:02:00Z",
        "artifact_rel": "dist",
        "artifact_bytes": 4096,
        "stderr_tail": "",
    }
    payload.update(overrides)
    return payload


class TestTheFourRowMatrix:
    """The core contract. One row each, plus the property that only row 2 blames the
    user."""

    def test_row1_sentinel_exit0_under_timeout_is_completed_ok(self) -> None:
        got = db.classify_build(_sentinel(), elapsed_seconds=30.0, timeout_seconds=600)
        assert got.outcome == "completed_ok"
        assert got.deployable is True
        assert got.blames_user is False

    def test_row2_sentinel_nonzero_exit_is_build_failed_and_blames_user(self) -> None:
        got = db.classify_build(
            _sentinel(build_exit=1, stderr_tail="TS2304: cannot find name 'foo'"),
            elapsed_seconds=30.0,
            timeout_seconds=600,
        )
        assert got.outcome == "build_failed"
        assert got.blames_user is True
        assert got.retryable is False
        assert "TS2304" in got.stderr_tail

    def test_row3_no_sentinel_at_or_over_timeout_is_timed_out(self) -> None:
        got = db.classify_build(None, elapsed_seconds=600.0, timeout_seconds=600)
        assert got.outcome == "timed_out"
        assert got.retryable is True
        assert got.blames_user is False

    def test_row4_no_sentinel_under_timeout_is_infra_lost(self) -> None:
        got = db.classify_build(None, elapsed_seconds=12.0, timeout_seconds=600)
        assert got.outcome == "infra_lost"
        assert got.retryable is True
        assert got.blames_user is False

    def test_only_build_failed_ever_blames_the_user(self) -> None:
        """The invariant the whole module exists to hold. If any non-``build_failed``
        outcome can blame the user, capacity loss becomes the user's problem."""
        cases = [
            db.classify_build(None, elapsed_seconds=1.0, timeout_seconds=600),
            db.classify_build(None, elapsed_seconds=999.0, timeout_seconds=600),
            db.classify_build(
                _sentinel(build_exit=db._EXIT_SIGKILL), elapsed_seconds=5.0, timeout_seconds=600
            ),
            db.classify_build(
                _sentinel(build_exit=db._EXIT_TIMEOUT), elapsed_seconds=5.0, timeout_seconds=600
            ),
            db.classify_build(_sentinel(), elapsed_seconds=5.0, timeout_seconds=600),
        ]
        for got in cases:
            if got.outcome != "build_failed":
                assert got.blames_user is False, got


class TestSignalledDeathsAreNotBuildFailures:
    """The residual gap in the sentinel design, and the subtlest part of the module.

    A signalled process STILL RUNS THE TRAP, so an OOM kill produces a sentinel with a
    non-zero ``build_exit`` — indistinguishable from a real build failure unless the
    exit code is inspected. Mutation: reorder the signal check below the
    ``build_exit != 0`` branch and every one of these becomes ``build_failed``.
    """

    def test_sigkill_137_is_infra_lost_not_build_failed(self) -> None:
        got = db.classify_build(
            _sentinel(build_exit=137), elapsed_seconds=45.0, timeout_seconds=600
        )
        assert got.outcome == "infra_lost"
        assert got.blames_user is False
        assert got.retryable is True

    def test_sigterm_143_is_infra_lost(self) -> None:
        got = db.classify_build(
            _sentinel(build_exit=143), elapsed_seconds=45.0, timeout_seconds=600
        )
        assert got.outcome == "infra_lost"
        assert got.blames_user is False

    def test_install_killed_by_signal_is_also_infra_lost(self) -> None:
        """The install step is the long one in a cold-per-build lane, so it is the more
        likely OOM victim — it must get the same treatment as the build step."""
        got = db.classify_build(
            _sentinel(install_exit=137, build_exit=-1), elapsed_seconds=45.0, timeout_seconds=600
        )
        assert got.outcome == "infra_lost"
        assert "install" in got.reason

    def test_exit_124_is_timed_out_with_evidence_retained(self) -> None:
        """``timeout(1)`` fired in-sandbox. Better than the clock-inferred timeout: we
        keep the stderr tail, so the user learns WHICH step overran."""
        got = db.classify_build(
            _sentinel(build_exit=124, stderr_tail="vite building..."),
            elapsed_seconds=610.0,
            timeout_seconds=600,
        )
        assert got.outcome == "timed_out"
        assert got.blames_user is False
        assert got.stderr_tail == "vite building..."

    def test_signal_check_wins_even_when_clock_says_under_timeout(self) -> None:
        """A 137 five seconds in is an OOM, not a slow build. The exit code is stronger
        evidence than the clock and must not be overridden by it."""
        got = db.classify_build(_sentinel(build_exit=137), elapsed_seconds=5.0, timeout_seconds=600)
        assert got.outcome == "infra_lost"


class TestUnusableSentinelsFailClosed:
    """A sentinel we cannot trust must classify exactly like no sentinel at all —
    never as a success, and never as the user's fault."""

    def test_truncated_json_is_treated_as_absent(self) -> None:
        got = db.classify_build(
            '{"schema": 1, "build_exit"', elapsed_seconds=5.0, timeout_seconds=600
        )
        assert got.outcome == "infra_lost"

    def test_non_dict_json_is_treated_as_absent(self) -> None:
        got = db.classify_build("[1, 2, 3]", elapsed_seconds=5.0, timeout_seconds=600)
        assert got.outcome == "infra_lost"

    def test_unknown_schema_is_treated_as_absent(self) -> None:
        """Refusing an unknown schema rather than guessing at field meanings: an
        unreadable sentinel carries no information, so it must not be read as one."""
        got = db.classify_build(_sentinel(schema=999), elapsed_seconds=5.0, timeout_seconds=600)
        assert got.outcome == "infra_lost"

    def test_missing_build_exit_does_not_deploy(self) -> None:
        payload = _sentinel()
        del payload["build_exit"]
        got = db.classify_build(payload, elapsed_seconds=5.0, timeout_seconds=600)
        assert got.outcome == "infra_lost"
        assert got.deployable is False

    def test_bool_exit_code_is_rejected_not_coerced(self) -> None:
        """``True`` is an ``int`` subclass that would read as exit 1 and ``False`` as
        exit 0 — the latter would deploy an unbuilt site. Mutation: drop the ``bool``
        guard in ``_coerce_exit`` and the ``False`` case starts reporting
        ``completed_ok``."""
        got = db.classify_build(
            _sentinel(build_exit=False), elapsed_seconds=5.0, timeout_seconds=600
        )
        assert got.outcome != "completed_ok"

    def test_accepts_bytes_as_well_as_str(self) -> None:
        got = db.classify_build(
            json.dumps(_sentinel()).encode(), elapsed_seconds=5.0, timeout_seconds=600
        )
        assert got.outcome == "completed_ok"

    def test_undecodable_bytes_are_treated_as_absent(self) -> None:
        got = db.classify_build(b"\xff\xfe not json", elapsed_seconds=5.0, timeout_seconds=600)
        assert got.outcome == "infra_lost"


class TestEmptyArtifactIsCaughtBeforeDeploy:
    """The empty-deploy failure: every step reports success and a blank site goes live.
    Exit 0 alone is not sufficient evidence."""

    def test_zero_bytes_is_build_failed_not_completed_ok(self) -> None:
        got = db.classify_build(
            _sentinel(artifact_bytes=0), elapsed_seconds=30.0, timeout_seconds=600
        )
        assert got.outcome == "build_failed"
        assert got.reason == "artifact_empty"
        assert got.deployable is False

    def test_missing_artifact_bytes_is_not_deployable(self) -> None:
        payload = _sentinel()
        del payload["artifact_bytes"]
        got = db.classify_build(payload, elapsed_seconds=30.0, timeout_seconds=600)
        assert got.deployable is False

    def test_install_failure_is_reported_but_retryable(self) -> None:
        """A bad manifest is the user's to fix, but a registry outage looks identical
        and nothing was built yet, so the retry is cheap. Both flags are set."""
        got = db.classify_build(
            _sentinel(install_exit=1, build_exit=-1), elapsed_seconds=30.0, timeout_seconds=600
        )
        assert got.outcome == "build_failed"
        assert got.reason == "install_failed"
        assert got.retryable is True
        assert got.blames_user is True


class TestTimeoutSizing:
    def test_covers_install_plus_build_not_build_alone(self) -> None:
        """The specific way a "strict timeout" instruction goes wrong: sizing on the
        build alone kills every build in a cold-per-build lane. Mutation: drop the
        install term and this drops to the floor, hiding the bug."""
        got = db.build_timeout_seconds(500.0, 400.0)
        assert got == pytest.approx(1350)  # (500 + 400) * 1.5

    def test_floor_applies_when_measurements_are_small(self) -> None:
        assert db.build_timeout_seconds(10.0, 10.0) == db.TIMEOUT_FLOOR_SECONDS

    def test_floor_is_at_least_the_existing_cold_install_budget(self) -> None:
        """scaffold.py already budgets 600s for a cold install of this toolchain; a
        floor below that guarantees timeouts on healthy builds."""
        assert db.TIMEOUT_FLOOR_SECONDS >= 600

    def test_negative_measurements_clamp_to_the_floor(self) -> None:
        assert db.build_timeout_seconds(-100.0, -100.0) == db.TIMEOUT_FLOOR_SECONDS

    def test_returns_whole_seconds(self) -> None:
        assert isinstance(db.build_timeout_seconds(101.3, 7.9), int)


class TestArtifactIncludeList:
    def test_react_tars_only_dist(self) -> None:
        cmd = db.artifact_tar_command("react", "/home/daytona/proj", "/tmp/a.tgz")
        assert "/home/daytona/proj/dist" in cmd
        # Updated 2026-08-10 (SG-7): this used to assert ``"node_modules" not in cmd``,
        # commented "excluded by construction, not by a filter". SG-7 measured that the
        # ``-C`` scope excludes a SIBLING node_modules but packs one NESTED inside the
        # output dir, so the command now carries an explicit ``--exclude`` and the old
        # string check fails on the very mention of the word. What the assertion was
        # protecting — that node_modules is not part of what gets packed — is checked
        # properly in test_fault_ladder_build.py by running the real tar over a real tree.
        assert "--exclude=./node_modules" in cmd
        # node_modules must appear ONLY as the exclusion, never as a packed path.
        assert cmd.count("node_modules") == 1

    def test_svelte_tars_only_the_adapter_output(self) -> None:
        cmd = db.artifact_tar_command("svelte", "/home/daytona/proj", "/tmp/a.tgz")
        assert "/home/daytona/proj/.svelte-kit/cloudflare" in cmd

    def test_html_is_refused_because_its_output_is_the_project_root(self) -> None:
        """Tarring ``.`` would sweep in node_modules and defeat the include-list. html
        needs no build so never reaches this lane; a caller that gets here is buggy and
        should hear about it. Mutation: remove the guard and html silently ships a
        node_modules-laden artifact."""
        with pytest.raises(ValueError, match="project root"):
            db.artifact_tar_command("html", "/home/daytona/proj", "/tmp/a.tgz")

    def test_paths_are_shell_quoted(self) -> None:
        cmd = db.artifact_tar_command("react", "/home/daytona/my proj", "/tmp/a b.tgz")
        assert "'/home/daytona/my proj/dist'" in cmd
        assert "'/tmp/a b.tgz'" in cmd


class TestWrapperScript:
    def test_trap_is_installed_before_any_command_that_can_fail(self) -> None:
        """If the trap is registered after the install, a failing install produces NO
        sentinel and is misclassified as infrastructure loss. Order is the contract."""
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz"
        )
        assert script.index("trap write_result EXIT") < script.index("bun install")

    def test_does_not_use_set_e(self) -> None:
        """``set -e`` would abort before the sentinel is written with the real exit
        codes — precisely when the evidence matters most."""
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz"
        )
        assert "set -e" not in script
        assert "set -u" in script

    def test_both_steps_run_under_the_in_sandbox_timeout(self) -> None:
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=777, artifact_path="/tmp/a.tgz"
        )
        assert script.count("timeout 777s") == 2

    def test_exit_codes_start_at_minus_one_so_a_step_that_never_ran_fails_closed(
        self,
    ) -> None:
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz"
        )
        assert "INSTALL_EXIT=-1" in script
        assert "BUILD_EXIT=-1" in script

    def test_sentinel_is_serialized_by_python_not_shell_string_concat(self) -> None:
        """stderr routinely contains quotes and newlines; a shell-escaped JSON writer
        would emit an unparseable sentinel exactly when there is an error worth
        reading."""
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz"
        )
        assert "python3 -" in script
        assert "json.dump" in script

    def test_tail_bytes_placeholder_is_substituted(self) -> None:
        script = db.build_wrapper_script(
            "react", "/p", timeout_seconds=600, artifact_path="/tmp/a.tgz"
        )
        assert "TAIL_BYTES" not in script
        assert str(db.STDERR_TAIL_BYTES) in script

    def test_install_and_build_commands_are_overridable(self) -> None:
        script = db.build_wrapper_script(
            "react",
            "/p",
            timeout_seconds=600,
            artifact_path="/tmp/a.tgz",
            install_command="bun install --frozen-lockfile",
            build_command="bun run build:prod",
        )
        assert "bun install --frozen-lockfile" in script
        assert "bun run build:prod" in script


class TestResolveBuildTimeout:
    """The shipped default, with measurement descoped. Both engines land on the floor,
    and the floor is deliberately loose: a too-generous timeout costs a slow failure,
    a too-tight one reports a healthy build as broken."""

    def _clear(self, mp: pytest.MonkeyPatch) -> None:
        for name in (
            "PAW_SITES_BUILD_TIMEOUT_SEC",
            "PAW_SITES_BUILD_TIMEOUT_SEC_REACT",
            "PAW_SITES_BUILD_TIMEOUT_SEC_SVELTE",
        ):
            mp.delenv(name, raising=False)

    def test_defaults_to_the_floor_for_both_engines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear(monkeypatch)
        assert db.resolve_build_timeout_seconds("react") == db.TIMEOUT_FLOOR_SECONDS
        assert db.resolve_build_timeout_seconds("svelte") == db.TIMEOUT_FLOOR_SECONDS

    def test_shared_env_knob_is_the_same_name_sg_p2_uses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One constant serves the local builder and this lane, so an operator tuning a
        slow deploy does not have to find two names."""
        self._clear(monkeypatch)
        monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", "1200")
        assert db.resolve_build_timeout_seconds("react") == 1200

    def test_per_engine_override_beats_the_shared_knob(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Engines differ by more than a constant factor — react installs 4 direct deps,
        svelte pulls the whole SvelteKit toolchain — so one number is either wasteful for
        one or fatal for the other."""
        self._clear(monkeypatch)
        monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", "1200")
        monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC_SVELTE", "2400")
        assert db.resolve_build_timeout_seconds("svelte") == 2400
        assert db.resolve_build_timeout_seconds("react") == 1200

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "  "])
    def test_malformed_values_fall_back_rather_than_raise(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        """The timeout is a safety net and must never itself break a build."""
        self._clear(monkeypatch)
        monkeypatch.setenv("PAW_SITES_BUILD_TIMEOUT_SEC", bad)
        assert db.resolve_build_timeout_seconds("react") == db.TIMEOUT_FLOOR_SECONDS
