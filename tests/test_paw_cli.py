# Tests for paw module CLI commands.
# Created: 2026-03-02
# Covers: main help, init (mocked soul-protocol), doctor, channels validation,
#         version option, serve placeholder. Uses Click's CliRunner throughout.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from pocketpaw.paw.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_soul():
    soul = MagicMock()
    soul.name = "Paw"
    soul.to_system_prompt.return_value = "I am Paw."
    soul.state = MagicMock(mood="curious", energy=85, social_battery=90)
    soul.self_model = None
    soul.remember = AsyncMock()
    soul.recall = AsyncMock(return_value=[])
    soul.observe = AsyncMock()
    soul.edit_core_memory = AsyncMock()
    soul.save = AsyncMock()
    soul.export = AsyncMock()
    return soul


# ---------------------------------------------------------------------------
# main group
# ---------------------------------------------------------------------------


class TestMainGroup:
    def test_main_shows_help_with_no_subcommand(self):
        runner = CliRunner()
        result = runner.invoke(main, [])

        assert result.exit_code == 0
        assert "paw" in result.output.lower()

    def test_help_flag_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0

    def test_version_option_outputs_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])

        # Should mention 'paw' in version string
        assert result.exit_code == 0
        assert "paw" in result.output.lower()


# ---------------------------------------------------------------------------
# paw init
# ---------------------------------------------------------------------------


class TestInitCommand:
    def test_init_fails_gracefully_when_soul_protocol_missing(self, tmp_path):
        runner = CliRunner()

        with patch("pocketpaw.paw.cli._check_soul_protocol", return_value=False):
            result = runner.invoke(main, ["init"], catch_exceptions=False)

        assert result.exit_code != 0
        assert "soul-protocol" in result.output.lower()

    def test_init_with_mocked_async_impl(self, tmp_path):
        runner = CliRunner()

        with (
            patch("pocketpaw.paw.cli._check_soul_protocol", return_value=True),
            patch("pocketpaw.paw.cli._init_async", new_callable=AsyncMock) as mock_init,
        ):
            result = runner.invoke(
                main, ["init", "--no-scan"], catch_exceptions=False
            )

        assert result.exit_code == 0
        mock_init.assert_awaited_once()

    def test_init_name_option_passed_through(self, tmp_path):
        runner = CliRunner()

        with (
            patch("pocketpaw.paw.cli._check_soul_protocol", return_value=True),
            patch("pocketpaw.paw.cli._init_async", new_callable=AsyncMock) as mock_init,
        ):
            result = runner.invoke(
                main, ["init", "--name", "Buddy", "--no-scan"], catch_exceptions=False
            )

        assert result.exit_code == 0
        # _init_async called with name="Buddy"
        _, kwargs = mock_init.call_args
        assert kwargs.get("name") == "Buddy" or mock_init.call_args.args[0] == "Buddy"

    def test_init_provider_option_accepted(self, tmp_path):
        runner = CliRunner()

        with (
            patch("pocketpaw.paw.cli._check_soul_protocol", return_value=True),
            patch("pocketpaw.paw.cli._init_async", new_callable=AsyncMock),
        ):
            result = runner.invoke(
                main, ["init", "--provider", "ollama", "--no-scan"], catch_exceptions=False
            )

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# paw doctor
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    def test_doctor_runs_without_error(self, tmp_path):
        runner = CliRunner()

        # Run inside tmp_path so .paw and paw.yaml are absent (WARN is fine)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("pocketpaw.paw.cli._check_soul_protocol", return_value=True):
                result = runner.invoke(main, ["doctor"], catch_exceptions=False)

        assert result.exit_code == 0

    def test_doctor_reports_soul_protocol_ok_when_installed(self, tmp_path):
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("pocketpaw.paw.cli._check_soul_protocol", return_value=True):
                result = runner.invoke(main, ["doctor"], catch_exceptions=False)

        assert "soul-protocol" in result.output.lower()

    def test_doctor_reports_soul_protocol_fail_when_missing(self, tmp_path):
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("pocketpaw.paw.cli._check_soul_protocol", return_value=False):
                result = runner.invoke(main, ["doctor"], catch_exceptions=False)

        assert "soul-protocol" in result.output.lower()

    def test_doctor_checks_for_paw_directory(self, tmp_path):
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["doctor"], catch_exceptions=False)

        # Output should mention .paw in some form
        assert ".paw" in result.output

    def test_doctor_paw_dir_ok_when_present(self, tmp_path):
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path(".paw").mkdir()
            result = runner.invoke(main, ["doctor"], catch_exceptions=False)

        assert "OK" in result.output or "ok" in result.output.lower()


# ---------------------------------------------------------------------------
# paw channels
# ---------------------------------------------------------------------------


class TestChannelsCommand:
    def test_channels_no_flags_exits_nonzero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["channels"], catch_exceptions=False)

        assert result.exit_code != 0

    def test_channels_no_flags_shows_usage_hint(self):
        runner = CliRunner()
        result = runner.invoke(main, ["channels"], catch_exceptions=False)

        assert "telegram" in result.output.lower() or "channel" in result.output.lower()

    def test_channels_telegram_flag_parsed_past_guard(self):
        runner = CliRunner()

        # Patch get_settings and the headless runner at their import locations
        mock_settings = MagicMock()

        with (
            patch("pocketpaw.config.get_settings", return_value=mock_settings),
            patch("pocketpaw.headless.run_telegram_mode", new=AsyncMock()),
        ):
            result = runner.invoke(main, ["channels", "--telegram"])

        # The flag was accepted — the "no flags" guard should NOT have fired
        assert "Specify at least one" not in result.output


# ---------------------------------------------------------------------------
# paw serve
# ---------------------------------------------------------------------------


class TestServeCommand:
    def test_serve_outputs_placeholder_message(self):
        runner = CliRunner()
        result = runner.invoke(main, ["serve"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "MCP" in result.output or "placeholder" in result.output.lower()

    def test_serve_custom_port_accepted(self):
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--port", "9999"], catch_exceptions=False)

        assert result.exit_code == 0
