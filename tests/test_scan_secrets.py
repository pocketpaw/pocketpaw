"""Tests for scripts/scan_secrets.py, the PR quality gate's secret scanner.

Added 2026-08-10 alongside the scanner. These exist because the check they
guard was dead in CI for months without anyone noticing: the PEM private-key
pattern began with a hyphen, grep parsed it as an option, and the resulting
exit status 2 was read by the calling `if` as "nothing found". A scan that
cannot fail is indistinguishable from a scan that passes, so the regression
has to be caught mechanically.

Every secret-shaped string used here is assembled from fragments at call
time. Nothing in this file, at rest, matches a pattern the scanner looks for
- that is deliberate. The scanner has twice been tripped by test fixtures and
by comments that quoted a realistic credential while explaining it. Describe
the shape; never write one down.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_secrets.py"

# A lone hyphen, so no five-hyphen run (and therefore no PEM header) is ever
# a literal in this file.
H = "-"


def _load() -> ModuleType:
    """Import the script by path. `scripts/` is not a package, so there is no
    import path to it; registering in sys.modules first is required because
    the module's frozen dataclasses resolve their annotations through it."""
    spec = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets"] = module
    spec.loader.exec_module(module)
    return module


scan_secrets = _load()


def _pem_header(kind: str = "RSA") -> str:
    """A PEM header built at runtime. Header only, no key material."""
    return f"{H * 5}BEGIN {kind} PRIVATE KEY{H * 5}"


def _diff(path: str, *added: str) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


# ---------------------------------------------------------------------------
# The regression this PR fixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["RSA", "EC", "DSA", "OPENSSH", "ENCRYPTED", ""])
def test_pem_private_key_is_detected(kind: str) -> None:
    """The check that never ran. Every PEM flavour must be caught."""
    findings = scan_secrets.scan(_diff("deploy/id_key", _pem_header(kind).replace("  ", " ")))
    assert [f.label for f in findings] == ["PEM private key"]


def test_pem_pattern_does_not_match_its_own_definition() -> None:
    """Guards the trap that kept the old pattern unfixable.

    The previous pattern (`-----BEGIN .*PRIVATE KEY-----`) matched its own
    text, so a correctly-quoted version of the old step would have failed the
    gate on the very workflow file that declared it. Any future pattern must
    stay inert against the scanner's own source.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    matching = [p.label for p in scan_secrets.PATTERNS if p.regex.search(source)]
    assert matching == []


def test_scanner_source_and_this_test_file_are_clean_at_rest() -> None:
    """Neither file may trip the scan when it lands in a PR diff."""
    for path in (SCRIPT, Path(__file__).resolve()):
        as_diff = _diff(path.name, *path.read_text(encoding="utf-8").splitlines())
        assert scan_secrets.scan(as_diff) == []


# ---------------------------------------------------------------------------
# Every pattern is alive
# ---------------------------------------------------------------------------


def test_self_test_reports_no_failures() -> None:
    assert scan_secrets.self_test() == []


@pytest.mark.parametrize("pattern", scan_secrets.PATTERNS, ids=lambda p: p.label)
def test_each_pattern_detects_its_own_sample(pattern: object) -> None:
    sample = pattern.sample()  # type: ignore[attr-defined]
    label = pattern.label  # type: ignore[attr-defined]
    findings = scan_secrets.scan(_diff("planted.txt", sample))
    assert label in [f.label for f in findings]


def test_anthropic_key_shape_is_detected() -> None:
    """The shape this repo is most likely to leak, and the one the old
    character class could not match because it stopped at the first hyphen."""
    key = "sk" + H + "ant" + H + "api03" + H + ("A" * 95)
    findings = scan_secrets.scan(_diff("src/config.py", f'ANTHROPIC_API_KEY = "{key}"'))
    assert "Anthropic API key" in [f.label for f in findings]


def test_github_actions_token_shape_is_detected() -> None:
    """ghs_ was absent from the old list; only ghp_ and gho_ were covered."""
    token = "gh" + "s_" + ("A" * 36)
    findings = scan_secrets.scan(_diff("ci/env.sh", f"export TOKEN={token}"))
    assert "GitHub token" in [f.label for f in findings]


# ---------------------------------------------------------------------------
# False positives the repo has already been burned by
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Rotate the disk" + H + "usagemonitoringcredentials every quarter.",
        "The sk_live prefix marks a Stripe key; the test prefix is different.",
        "See docs/security.md for how we handle PEM material.",
        "REDACT_PATTERNS covers AWS, Slack and GitHub token prefixes.",
    ],
)
def test_prose_about_secrets_does_not_trip_the_scan(line: str) -> None:
    assert scan_secrets.scan(_diff("docs/security.md", line)) == []


def test_removed_lines_are_not_scanned() -> None:
    """A PR whose purpose is to delete a leaked key must not be blocked."""
    removal = f"+++ b/leak.py\n-{_pem_header()}\n"
    assert scan_secrets.scan(removal) == []


def test_context_lines_are_not_scanned() -> None:
    """Pre-existing matches near an unrelated edit must not fail every PR."""
    diff = f"+++ b/leak.py\n@@ -1,3 +1,3 @@\n {_pem_header()}\n+x = 1\n"
    assert scan_secrets.scan(diff) == []


# ---------------------------------------------------------------------------
# Finding location
# ---------------------------------------------------------------------------


def test_finding_reports_the_post_change_file_line() -> None:
    """The line number comes from the hunk header, so it points at the real
    location in the file rather than at an offset within the diff."""
    diff = (
        "diff --git a/deploy/keys.py b/deploy/keys.py\n"
        "--- a/deploy/keys.py\n"
        "+++ b/deploy/keys.py\n"
        "@@ -40,3 +40,4 @@ def load():\n"
        "     pass\n"
        "-old = 1\n"
        f"+KEY = '{_pem_header()}'\n"
        "     return None\n"
    )
    findings = scan_secrets.scan(diff)
    assert [f.render() for f in findings] == ["deploy/keys.py:41: PEM private key"]


# ---------------------------------------------------------------------------
# Exit status contract - the part CI depends on
# ---------------------------------------------------------------------------


def _run(stdin: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_exit_status_1_on_findings() -> None:
    result = _run(_diff("deploy/id_key", _pem_header()))
    assert result.returncode == scan_secrets.EXIT_FINDINGS
    assert "PEM private key" in result.stdout


def test_exit_status_0_on_clean_diff() -> None:
    result = _run(_diff("src/foo.py", "def hello():", "    return 1"))
    assert result.returncode == scan_secrets.EXIT_CLEAN


def test_self_test_flag_exits_zero() -> None:
    result = _run("", "--self-test")
    assert result.returncode == scan_secrets.EXIT_CLEAN
    assert "patterns alive" in result.stdout


def test_findings_never_echo_the_matched_text() -> None:
    """CI logs are widely readable; printing the match publishes the secret."""
    key = "AKIA" + ("Q" * 16)
    result = _run(_diff("src/config.py", f"AWS_KEY = {key}"))
    assert result.returncode == scan_secrets.EXIT_FINDINGS
    assert key not in result.stdout
    assert "AWS access key ID" in result.stdout


def test_all_lines_mode_scans_plain_text() -> None:
    result = _run(_pem_header() + "\n", "--all-lines")
    assert result.returncode == scan_secrets.EXIT_FINDINGS
