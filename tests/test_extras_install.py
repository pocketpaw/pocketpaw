"""Tests for GET /api/extras/check and POST /api/extras/install endpoints."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def test_client():
    """Create a FastAPI TestClient for the dashboard app with auth bypassed."""
    from starlette.testclient import TestClient

    from pocketpaw.dashboard import app

    return TestClient(app, raise_server_exceptions=False)


def _auth_bypass():
    """Context manager to bypass dashboard auth middleware."""
    return patch("pocketpaw.dashboard._is_genuine_localhost", return_value=True)


def _dep_installed():
    """Mock _is_module_importable to return True (dep present)."""
    return patch("pocketpaw.dashboard._is_module_importable", return_value=True)


def _dep_missing():
    """Mock _is_module_importable to return False (dep absent)."""
    return patch("pocketpaw.dashboard._is_module_importable", return_value=False)


# ---------------------------------------------------------------------------
# GET /api/extras/check
# ---------------------------------------------------------------------------


class TestExtrasCheck:
    def test_check_installed_dep(self, test_client):
        """When the module is importable, installed should be True."""
        with _auth_bypass(), _dep_installed():
            resp = test_client.get("/api/extras/check?channel=discord")
        assert resp.status_code == 200
        data = resp.json()
        assert data["installed"] is True
        assert data["extra"] == "discord"
        assert data["package"] == "discord.py"
        assert data["pip_spec"] == "pocketpaw[discord]"

    def test_check_missing_dep(self, test_client):
        """When the module is NOT importable, installed should be False."""
        with _auth_bypass(), _dep_missing():
            resp = test_client.get("/api/extras/check?channel=discord")
        assert resp.status_code == 200
        data = resp.json()
        assert data["installed"] is False
        assert data["package"] == "discord.py"

    def test_check_unknown_channel_returns_installed(self, test_client):
        """Unknown channels (e.g. signal) have no optional dep — always installed."""
        with _auth_bypass():
            resp = test_client.get("/api/extras/check?channel=signal")
        assert resp.status_code == 200
        data = resp.json()
        assert data["installed"] is True
        assert data["package"] == ""

    def test_check_all_known_channels(self, test_client):
        """Every channel in _CHANNEL_DEPS should return correct metadata."""
        from pocketpaw.dashboard import _CHANNEL_DEPS

        for ch, (_import_mod, package, pip_spec) in _CHANNEL_DEPS.items():
            with _auth_bypass(), _dep_missing():
                resp = test_client.get(f"/api/extras/check?channel={ch}")
            data = resp.json()
            assert data["extra"] == ch
            assert data["package"] == package
            assert data["pip_spec"] == pip_spec

    def test_check_whatsapp_returns_neonize(self, test_client):
        """WhatsApp check should refer to neonize (personal mode dep)."""
        with _auth_bypass(), _dep_missing():
            resp = test_client.get("/api/extras/check?channel=whatsapp")
        data = resp.json()
        assert data["package"] == "neonize"
        assert data["pip_spec"] == "pocketpaw[whatsapp-personal]"


# ---------------------------------------------------------------------------
# POST /api/extras/install
# ---------------------------------------------------------------------------


class TestExtrasInstall:
    def test_install_unknown_extra_returns_400(self, test_client):
        """Unknown extra names should be rejected."""
        with _auth_bypass():
            resp = test_client.post(
                "/api/extras/install",
                json={"extra": "nonexistent"},
            )
        assert resp.status_code == 400

    def test_install_already_installed(self, test_client):
        """If the dep is already importable, return ok immediately."""
        with _auth_bypass(), _dep_installed():
            resp = test_client.post(
                "/api/extras/install",
                json={"extra": "discord"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_install_success(self, test_client):
        """Successful install calls auto_install and returns ok."""
        with (
            _auth_bypass(),
            _dep_missing(),
            patch("pocketpaw.bus.adapters.auto_install") as mock_install,
        ):
            resp = test_client.post(
                "/api/extras/install",
                json={"extra": "discord"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_install.assert_called_once_with("discord", "discord.ext.commands")

    def test_install_whatsapp_uses_personal_extra(self, test_client):
        """WhatsApp should use 'whatsapp-personal' as the extra name."""
        with (
            _auth_bypass(),
            _dep_missing(),
            patch("pocketpaw.bus.adapters.auto_install") as mock_install,
        ):
            resp = test_client.post(
                "/api/extras/install",
                json={"extra": "whatsapp"},
            )
        assert resp.status_code == 200
        mock_install.assert_called_once_with("whatsapp-personal", "neonize")

    def test_install_failure_returns_error(self, test_client):
        """If auto_install raises RuntimeError, return the error message."""
        with (
            _auth_bypass(),
            _dep_missing(),
            patch(
                "pocketpaw.bus.adapters.auto_install",
                side_effect=RuntimeError("pip not found"),
            ),
        ):
            resp = test_client.post(
                "/api/extras/install",
                json={"extra": "discord"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert "pip not found" in data["error"]

    def test_install_prevents_arbitrary_packages(self, test_client):
        """Ensure only known extras can be installed (prevents arbitrary pkg install)."""
        with _auth_bypass():
            resp = test_client.post(
                "/api/extras/install",
                json={"extra": "malicious-package"},
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/channels/toggle — missing_dep fallback
# ---------------------------------------------------------------------------


class TestToggleMissingDep:
    def test_toggle_start_import_error_returns_missing_dep(self, test_client):
        """When _start_channel_adapter raises ImportError, return missing_dep."""
        with (
            _auth_bypass(),
            patch("pocketpaw.dashboard.Settings") as mock_settings_cls,
            patch("pocketpaw.dashboard._channel_is_running", return_value=False),
            patch("pocketpaw.dashboard._channel_is_configured", return_value=True),
            patch(
                "pocketpaw.dashboard._start_channel_adapter",
                side_effect=ImportError("module not found"),
            ),
        ):
            settings = MagicMock()
            mock_settings_cls.load.return_value = settings
            resp = test_client.post(
                "/api/channels/toggle",
                json={"channel": "telegram", "action": "start"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["missing_dep"] is True
        assert data["channel"] == "telegram"
        assert data["package"] == "python-telegram-bot"
        assert data["pip_spec"] == "pocketpaw[telegram]"

    def test_toggle_start_regular_error_returns_error(self, test_client):
        """Non-ImportError exceptions return a plain error string."""
        with (
            _auth_bypass(),
            patch("pocketpaw.dashboard.Settings") as mock_settings_cls,
            patch("pocketpaw.dashboard._channel_is_running", return_value=False),
            patch("pocketpaw.dashboard._channel_is_configured", return_value=True),
            patch(
                "pocketpaw.dashboard._start_channel_adapter",
                side_effect=RuntimeError("connection refused"),
            ),
        ):
            settings = MagicMock()
            mock_settings_cls.load.return_value = settings
            resp = test_client.post(
                "/api/channels/toggle",
                json={"channel": "telegram", "action": "start"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert "missing_dep" not in data
