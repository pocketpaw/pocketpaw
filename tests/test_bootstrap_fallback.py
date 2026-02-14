# Tests for bootstrap extras fallback chain (fix for #54 — Windows install failure).
# Verifies that when heavy extras fail, the installer retries with lighter sets.

from __future__ import annotations

from unittest.mock import MagicMock, patch

from installer.launcher.bootstrap import Bootstrap


class TestBuildFallbackChain:
    """Unit tests for Bootstrap._build_fallback_chain."""

    def setup_method(self):
        self.bs = Bootstrap()

    def test_recommended_falls_back_to_dashboard_then_bare(self):
        chain = self.bs._build_fallback_chain(["recommended"])
        assert chain == [["dashboard"], []]

    def test_all_falls_back_to_dashboard_then_bare(self):
        chain = self.bs._build_fallback_chain(["all"])
        assert chain == [["dashboard"], []]

    def test_dashboard_only_falls_back_to_bare(self):
        chain = self.bs._build_fallback_chain(["dashboard"])
        # dashboard alone — no intermediate step, just bare
        assert chain == [[]]

    def test_dashboard_plus_browser_falls_back_to_dashboard_then_bare(self):
        chain = self.bs._build_fallback_chain(["dashboard", "browser"])
        assert chain == [["dashboard"], []]

    def test_bare_extras_returns_just_bare(self):
        chain = self.bs._build_fallback_chain(["telegram"])
        # Non-heavy single extra — just try bare
        assert chain == [[]]

    def test_empty_extras_returns_bare(self):
        chain = self.bs._build_fallback_chain([])
        assert chain == [[]]


class TestInstallFallbackIntegration:
    """Integration test: verify the fallback chain is exercised when install fails."""

    def test_fallback_called_on_extras_failure(self):
        bs = Bootstrap()
        install_results = iter(
            [
                "failed: mem0ai",  # recommended fails
                None,  # dashboard succeeds
            ]
        )

        with patch.object(
            bs, "_install_pocketpaw", side_effect=lambda *a, **kw: next(install_results)
        ):
            with patch.object(bs, "_find_python", return_value="/usr/bin/python3"):
                with patch.object(bs, "_get_python_version", return_value="3.12.0"):
                    with patch.object(bs, "_ensure_uv", return_value=None):
                        with patch.object(bs, "_venv_python") as mock_vp:
                            mock_path = MagicMock()
                            mock_path.exists.return_value = True
                            mock_path.__str__ = lambda s: "/fake/venv/python"
                            mock_vp.return_value = mock_path

                            with patch(
                                "installer.launcher.bootstrap.get_installed_version",
                                return_value="0.3.0",
                            ):
                                status = bs.run(extras=["recommended"])

        assert status.pocketpaw_installed is True
        assert status.error is None

    def test_all_fallbacks_fail_returns_error(self):
        bs = Bootstrap()

        with patch.object(bs, "_install_pocketpaw", return_value="install failed"):
            with patch.object(bs, "_find_python", return_value="/usr/bin/python3"):
                with patch.object(bs, "_get_python_version", return_value="3.12.0"):
                    with patch.object(bs, "_ensure_uv", return_value=None):
                        with patch.object(bs, "_venv_python") as mock_vp:
                            mock_path = MagicMock()
                            mock_path.exists.return_value = True
                            mock_path.__str__ = lambda s: "/fake/venv/python"
                            mock_vp.return_value = mock_path

                            status = bs.run(extras=["recommended"])

        assert status.error is not None
        assert "install failed" in status.error
