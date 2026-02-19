"""Tests for installer.py interactive prompt fallback — Issue #184.

Reproduces: InquirerPy raises OSError(22, "Invalid argument") when running
via `curl | sh` on macOS with stdin redirected from /dev/tty. The installer
should fall back to plain text prompts instead of crashing.

Changes:
  - 2026-02-17: Created. Reproduces issue #184 (macOS Errno 22 crash).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# We test the InstallerUI methods by importing them from installer.py.
# The module runs _bootstrap_deps() at import time, so we patch around it.
# ---------------------------------------------------------------------------


def _import_installer():
    """Import installer module, handling the module-level bootstrap."""
    # Add installer dir to path if needed
    import importlib
    from pathlib import Path

    installer_dir = Path(__file__).parent.parent / "installer"
    if str(installer_dir) not in sys.path:
        sys.path.insert(0, str(installer_dir))

    # If already imported, reload; otherwise import fresh
    if "installer" in sys.modules:
        return sys.modules["installer"]

    return importlib.import_module("installer")


class TestPromptFallbackOnOSError:
    """Issue #184: InquirerPy crashes with OSError on macOS curl|sh installs.

    The installer should gracefully fall back to plain text prompts when
    InquirerPy's prompt_toolkit raises OSError (Errno 22: Invalid argument).
    """

    def test_prompt_profile_falls_back_on_oserror(self):
        """prompt_profile() should return a valid profile even when InquirerPy raises OSError."""
        mod = _import_installer()

        ui = mod.InstallerUI()

        # Mock InquirerPy to raise OSError (simulating the macOS curl|sh bug)
        mock_select = MagicMock()
        mock_select.return_value.execute.side_effect = OSError(22, "Invalid argument")

        # Mock plain input to select "recommended" (choice 1)
        with (
            patch.object(mod, "_HAS_INQUIRER", True),
            patch.object(mod, "inquirer", MagicMock(select=mock_select)),
            patch("builtins.input", return_value="1"),
        ):
            result = ui.prompt_profile()

        assert result == "recommended"

    def test_prompt_backend_falls_back_on_oserror(self):
        """prompt_backend() should return a valid backend when InquirerPy fails."""
        mod = _import_installer()

        ui = mod.InstallerUI()

        mock_select = MagicMock()
        mock_select.return_value.execute.side_effect = OSError(22, "Invalid argument")

        # Mock plain input to select first choice (claude_agent_sdk)
        with (
            patch.object(mod, "_HAS_INQUIRER", True),
            patch.object(mod, "inquirer", MagicMock(select=mock_select)),
            patch("builtins.input", return_value="1"),
        ):
            result = ui.prompt_backend()

        assert result == "claude_agent_sdk"

    def test_prompt_confirmation_falls_back_on_oserror(self):
        """prompt_confirmation() should work when InquirerPy fails."""
        mod = _import_installer()

        ui = mod.InstallerUI()

        mock_confirm = MagicMock()
        mock_confirm.return_value.execute.side_effect = OSError(22, "Invalid argument")

        summary = {
            "profile": "recommended",
            "extras": ["recommended"],
            "backend": "claude_agent_sdk",
            "llm_provider": "auto",
            "web_port": 8888,
            "config": {},
            "pip_cmd": "uv pip",
        }

        # Mock plain input to confirm (Enter = default yes)
        with (
            patch.object(mod, "_HAS_INQUIRER", True),
            patch.object(mod, "inquirer", MagicMock(confirm=mock_confirm)),
            patch("builtins.input", return_value=""),
        ):
            result = ui.prompt_confirmation(summary)

        assert result is True

    def test_prompt_web_port_falls_back_on_oserror(self):
        """prompt_web_port() should return default port when InquirerPy fails."""
        mod = _import_installer()

        ui = mod.InstallerUI()

        mock_text = MagicMock()
        mock_text.return_value.execute.side_effect = OSError(22, "Invalid argument")

        # Mock plain input to accept default
        with (
            patch.object(mod, "_HAS_INQUIRER", True),
            patch.object(mod, "inquirer", MagicMock(text=mock_text)),
            patch("builtins.input", return_value="8888"),
        ):
            result = ui.prompt_web_port()

        assert result == 8888

    def test_prompt_llm_provider_falls_back_on_oserror(self):
        """prompt_llm_provider() should return a valid provider when InquirerPy fails."""
        mod = _import_installer()

        ui = mod.InstallerUI()

        mock_select = MagicMock()
        mock_select.return_value.execute.side_effect = OSError(22, "Invalid argument")

        # Mock plain input to select first choice (anthropic)
        with (
            patch.object(mod, "_HAS_INQUIRER", True),
            patch.object(mod, "inquirer", MagicMock(select=mock_select)),
            patch("builtins.input", return_value="1"),
        ):
            result = ui.prompt_llm_provider()

        assert result == "anthropic"

    def test_prompt_custom_features_falls_back_on_oserror(self):
        """prompt_custom_features() should work when InquirerPy fails."""
        mod = _import_installer()

        ui = mod.InstallerUI()

        mock_checkbox = MagicMock()
        mock_checkbox.return_value.execute.side_effect = OSError(22, "Invalid argument")

        # Mock plain input to select "all"
        with (
            patch.object(mod, "_HAS_INQUIRER", True),
            patch.object(mod, "inquirer", MagicMock(checkbox=mock_checkbox)),
            patch.object(mod, "Separator", MagicMock()),
            patch("builtins.input", return_value="all"),
        ):
            result = ui.prompt_custom_features()

        assert isinstance(result, list)
        assert len(result) > 0

    def test_disables_inquirer_after_first_failure(self):
        """After OSError, _HAS_INQUIRER should be set to False so subsequent prompts skip InquirerPy."""
        mod = _import_installer()

        ui = mod.InstallerUI()

        mock_select = MagicMock()
        mock_select.return_value.execute.side_effect = OSError(22, "Invalid argument")

        original_has_inquirer = mod._HAS_INQUIRER

        try:
            with (
                patch.object(mod, "inquirer", MagicMock(select=mock_select)),
                patch("builtins.input", return_value="1"),
            ):
                # Set it to True to simulate InquirerPy being available
                mod._HAS_INQUIRER = True
                ui.prompt_profile()

                # After the OSError, _HAS_INQUIRER should be False
                assert mod._HAS_INQUIRER is False
        finally:
            # Restore
            mod._HAS_INQUIRER = original_has_inquirer
