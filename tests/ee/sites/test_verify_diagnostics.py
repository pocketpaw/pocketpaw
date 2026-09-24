# tests/ee/sites/test_verify_diagnostics.py — the agent-only diagnostics channel
# (pocketpaw_ee.sites.verify_diagnostics). Created 2026-09-24 (PP-2).
#
# These pin the contract §5 rules for every message that reaches an agent: secrets
# redacted, sandbox paths made project-relative, ``site_key_*`` scrubbed, and the whole
# errors + warnings payload capped at 2 KB with a trailing ``{"code": "truncated"}``.
# The mutation plan tests/mutations/sites_verify_pipeline.json breaks each step.
from __future__ import annotations

import json

from pocketpaw_ee.sites import verify_diagnostics as vd

_KEY = "site_key_" + "A1b2C3d4E5f6G7h8I9j0K1l2"


class TestScrub:
    def test_site_keys_are_scrubbed(self) -> None:
        out = vd.scrub_text(f"posting with {_KEY} failed")
        assert _KEY not in out
        assert "site_key_[redacted]" in out

    def test_sandbox_paths_become_project_relative(self) -> None:
        out = vd.scrub_text("/home/daytona/paw-build/src/lib/components/Hero.svelte:12:5 boom")
        assert out.startswith("src/lib/components/Hero.svelte:12:5")
        assert "/home/daytona" not in out

    def test_other_absolute_paths_keep_only_their_basename(self) -> None:
        out = vd.scrub_text("failed reading /tmp/paw-browser-abc/secret-dir/file.js")
        assert "/tmp" not in out and "secret-dir" not in out
        assert "file.js" in out

    def test_redact_output_runs(self) -> None:
        token = "ghp_" + "a1B2" * 10
        out = vd.scrub_text(f"cloning with {token}")
        assert token not in out

    def test_local_server_origins_are_dropped(self) -> None:
        out = vd.scrub_text("GET http://127.0.0.1:53211/_app/x.js 404")
        assert "127.0.0.1" not in out

    def test_one_message_is_bounded(self) -> None:
        assert len(vd.scrub_text("x" * 5000)) <= vd.MESSAGE_MAX_CHARS


class TestCap:
    def _big(self, n: int, layer: str = "build") -> list[dict]:
        return [
            {"layer": layer, "code": "build_error", "message": f"error {i} " + "y" * 200}
            for i in range(n)
        ]

    def test_under_the_cap_is_untouched(self) -> None:
        errors, warnings = vd.finalize([{"layer": "build", "code": "c", "message": "m"}], [])
        assert errors == [{"layer": "build", "code": "c", "message": "m"}]
        assert warnings == []

    def test_over_the_cap_is_cut_with_a_trailing_marker(self) -> None:
        errors, warnings = vd.finalize(self._big(30), self._big(5))
        payload = json.dumps({"errors": errors, "warnings": warnings}, separators=(",", ":"))
        assert len(payload.encode()) <= vd.DIAGNOSTICS_CAP_BYTES
        assert errors[-1] == vd.TRUNCATED_ENTRY
        assert len(errors) > 1, "the cap must keep what fits, not drop everything"

    def test_errors_are_kept_before_warnings(self) -> None:
        errors, warnings = vd.finalize(self._big(3), self._big(30, "static"))
        assert len([e for e in errors if e.get("message")]) == 3
        assert warnings[-1] == vd.TRUNCATED_ENTRY

    def test_a_prior_marker_survives_a_merge(self) -> None:
        """A job's report was already cut; merging it with the static layer must not
        lose the fact that something is missing."""
        errors, _ = vd.finalize(
            [{"layer": "build", "code": "c", "message": "m"}, dict(vd.TRUNCATED_ENTRY)]
        )
        assert errors[-1] == vd.TRUNCATED_ENTRY
        assert errors[0]["message"] == "m"

    def test_the_cap_applies_after_scrubbing_every_entry(self) -> None:
        errors, _ = vd.finalize([{"layer": "build", "code": "c", "message": _KEY}])
        assert _KEY not in json.dumps(errors)


class TestParseBuildStderr:
    def test_a_svelte_compile_error_names_file_line_col(self) -> None:
        tail = (
            "vite v6.0.0 building SSR bundle for production...\n"
            "[vite-plugin-svelte] Error while compiling\n"
            "/home/daytona/paw-build/src/lib/components/Hero.svelte:12:5 "
            "Unexpected token (CompileError)\n"
        )
        entries = vd.parse_build_stderr(tail)
        hit = next(e for e in entries if e.get("line") == 12)
        assert hit["file"].endswith("src/lib/components/Hero.svelte")
        assert hit["col"] == 5

    def test_a_rollup_unresolved_import_names_the_package(self) -> None:
        tail = (
            '[vite]: Rollup failed to resolve import "three" from '
            '"/home/daytona/paw-build/src/lib/components/Scene.svelte".\n'
        )
        entries = vd.parse_build_stderr(tail)
        assert entries[0]["code"] == "unresolved_import"
        assert "three" in entries[0]["message"]

    def test_an_unparseable_failure_still_yields_one_entry(self) -> None:
        entries = vd.parse_build_stderr("something odd happened\nand then it stopped\n")
        assert len(entries) == 1
        assert entries[0]["code"] == "build_failed"

    def test_an_empty_tail_yields_nothing(self) -> None:
        assert vd.parse_build_stderr("") == []

    def test_parsed_entries_are_scrubbed_by_finalize(self) -> None:
        tail = f"Error: key {_KEY} at /home/daytona/paw-build/src/app.css:1:1\n"
        errors, _ = vd.finalize(vd.parse_build_stderr(tail))
        text = json.dumps(errors)
        assert _KEY not in text and "/home/daytona" not in text


class TestHarnessEntries:
    def test_pages_flatten_to_file_and_code(self) -> None:
        entries = vd.harness_entries(
            {
                "ok": False,
                "pages": [
                    {
                        "path": "index.html",
                        "errors": [{"kind": "pageerror", "message": "boom", "source": "x.js:1:2"}],
                    }
                ],
            }
        )
        assert entries == [
            {
                "layer": "browser",
                "code": "pageerror",
                "message": "boom (at x.js:1:2)",
                "file": "index.html",
            }
        ]
