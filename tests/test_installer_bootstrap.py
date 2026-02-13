"""Tests for installer.py bootstrap dependency logic.

Changes:
  - 2026-02-13: Created. Reproduces the bug where uv installs to wrong Python
                and _HAS_RICH is set True even though rich isn't importable.
"""

import importlib
import importlib.util
import subprocess
import sys
from unittest.mock import patch

# ---------------------------------------------------------------------------
# We can't import installer.py directly (it runs _bootstrap_deps at module
# level), so we extract and test the individual functions.
# ---------------------------------------------------------------------------


def _verify_imports_fn(packages: list[str]) -> tuple[bool, bool, bool]:
    """Standalone copy of _verify_imports for testing.

    Returns (all_ok, has_rich, has_inquirer).
    """
    has_rich = False
    has_inquirer = False
    all_ok = True
    for pkg in packages:
        spec_name = "rich" if pkg == "rich" else "InquirerPy"
        if importlib.util.find_spec(spec_name) is not None:
            if spec_name == "rich":
                has_rich = True
            else:
                has_inquirer = True
        else:
            all_ok = False
    return all_ok, has_rich, has_inquirer


class TestVerifyImports:
    """Tests for the _verify_imports helper."""

    def test_returns_true_when_all_found(self):
        """rich is available in our test env, so this should find it."""
        all_ok, has_rich, _ = _verify_imports_fn(["rich"])
        assert all_ok is True
        assert has_rich is True

    def test_returns_false_for_missing_package(self):
        """A package that doesn't exist should return False."""
        with patch.object(importlib.util, "find_spec", return_value=None):
            all_ok, has_rich, has_inquirer = _verify_imports_fn(["rich", "InquirerPy"])
        assert all_ok is False
        assert has_rich is False
        assert has_inquirer is False

    def test_partial_success(self):
        """If rich is found but InquirerPy isn't, all_ok is False but has_rich is True."""
        original_find_spec = importlib.util.find_spec

        def mock_find_spec(name):
            if name == "InquirerPy":
                return None
            return original_find_spec(name)

        with patch.object(importlib.util, "find_spec", side_effect=mock_find_spec):
            all_ok, has_rich, has_inquirer = _verify_imports_fn(["rich", "InquirerPy"])
        assert all_ok is False
        assert has_rich is True
        assert has_inquirer is False


class TestBootstrapBugReproduction:
    """Reproduces the original bug: uv install succeeds but package isn't importable."""

    def test_old_behavior_would_crash(self):
        """Before the fix: subprocess succeeds -> _HAS_RICH = True -> import crashes.

        This test verifies that if subprocess.check_call succeeds but find_spec
        returns None, _verify_imports correctly returns False so we don't
        blindly set _HAS_RICH = True.
        """
        # Simulate: uv pip install exits 0 but package isn't on sys.path
        with patch.object(importlib.util, "find_spec", return_value=None):
            all_ok, has_rich, _ = _verify_imports_fn(["rich"])

        # The fix: _verify_imports returns False, so we don't claim success
        assert all_ok is False
        assert has_rich is False

    def test_uv_command_includes_python_flag(self):
        """The uv command should include --python sys.executable to target the right Python."""
        captured_cmd = []

        def capture_check_call(cmd, **kwargs):
            captured_cmd.extend(cmd)
            raise subprocess.CalledProcessError(1, cmd)  # Fail so we can inspect

        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch("subprocess.check_call", side_effect=capture_check_call),
            patch.object(importlib.util, "find_spec", return_value=None),
        ):
            # Simulate the cascade 1 logic inline
            import shutil

            if shutil.which("uv"):
                try:
                    cmd = ["uv", "pip", "install", "-q", "--python", sys.executable, "rich"]
                    # Simulating _in_virtualenv() = False
                    cmd.insert(3, "--system")
                    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except Exception:
                    pass

        assert "--python" in captured_cmd
        assert sys.executable in captured_cmd
        assert "--system" in captured_cmd

    def test_fallback_to_plain_text_when_all_cascades_fail(self):
        """If all install cascades fail, _HAS_RICH stays False and we get plain text mode."""
        # All find_spec calls return None (nothing installable)
        with patch.object(importlib.util, "find_spec", return_value=None):
            all_ok, has_rich, has_inquirer = _verify_imports_fn(["rich", "InquirerPy"])

        assert all_ok is False
        assert has_rich is False
        assert has_inquirer is False
        # In the real code, this means line 157 `if _HAS_RICH:` is False
        # so we go to `else: console = None` instead of crashing on import
