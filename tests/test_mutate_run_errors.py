"""Mutation verdicts require working tests; exercise the CLI in an isolated tree."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mutate.py"
ORIGINAL = b"original\r\n"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(SCRIPT, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "target.txt").write_bytes(ORIGINAL)
    return tmp_path


def _run(root: Path, tests: object, test_source: str = "", *args: str):
    (root / "test_target.py").write_text(test_source, encoding="utf-8")
    plan = [
        {
            "label": "change the sentinel",
            "file": "target.txt",
            "find": "original",
            "replace": "mutated",
            "tests": tests,
        }
    ]
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "mutate.py"), "--plan", "plan.json", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_restored(root: Path) -> None:
    assert (root / "target.txt").read_bytes() == ORIGINAL
    assert not (root / ".mutation-sweep-active").exists()


@pytest.mark.parametrize(
    "targets",
    [["missing_test.py"], ["test_target.py::test_missing"], ["test_target.py"]],
)
def test_missing_or_uncollected_tests_cannot_catch_a_mutation(sandbox, targets):
    result = _run(sandbox, targets)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "pytest exited" in result.stderr
    assert "change the sentinel" in result.stderr
    assert "caught" not in result.stdout
    _assert_restored(sandbox)


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5])
def test_test_run_errors_abort_and_restore_the_applied_mutation(sandbox, exit_code):
    result = _run(
        sandbox,
        ["test_target.py"],
        "from pathlib import Path\n"
        "import pytest\n"
        "def test_target():\n"
        "    if 'mutated' in Path('target.txt').read_text():\n"
        "        Path('mutation_seen').touch()\n"
        f"        pytest.exit('test runner unavailable', returncode={exit_code})\n",
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"pytest exited with code {exit_code}" in result.stderr
    assert "change the sentinel" in result.stderr
    assert "test runner unavailable" in result.stderr
    assert "caught" not in result.stdout
    assert (sandbox / "mutation_seen").exists()
    _assert_restored(sandbox)


def test_preexisting_failures_stop_the_sweep_before_mutating(sandbox):
    result = _run(
        sandbox,
        ["test_target.py"],
        "from pathlib import Path\n"
        "def test_target():\n"
        "    assert Path('target.txt').read_bytes() == b'original\\r\\n'\n"
        "    assert False, 'preexisting failure'\n",
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "baseline" in result.stderr.lower()
    assert "preexisting failure" in result.stderr
    assert "caught" not in result.stdout
    _assert_restored(sandbox)


@pytest.mark.parametrize("tests", ["test_target.py", [], None, {}, [None], [""]])
@pytest.mark.parametrize("args", [(), ("--validate",)])
def test_malformed_test_targets_are_rejected_before_mutating(sandbox, tests, args):
    result = _run(sandbox, tests, "", *args)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "tests must be a non-empty list" in result.stderr
    assert "change the sentinel" in result.stderr
    _assert_restored(sandbox)


@pytest.mark.parametrize("caught", [True, False])
def test_real_test_results_still_decide_caught_or_escaped(sandbox, caught):
    assertion = "assert text == 'original'" if caught else "assert text"
    result = _run(
        sandbox,
        ["test_target.py"],
        "from pathlib import Path\n"
        "def test_target():\n"
        "    text = Path('target.txt').read_text().strip()\n"
        f"    {assertion}\n",
    )

    assert result.returncode == (0 if caught else 1), result.stdout + result.stderr
    assert ("all 1 mutations caught" if caught else "1 of 1 mutations ESCAPED") in result.stdout
    _assert_restored(sandbox)
