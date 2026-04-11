# Tests for API v1 skills router.
# Created: 2026-02-21

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.skills import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestListSkills:
    """Tests for GET /skills."""

    @patch("pocketpaw.skills.get_skill_loader")
    def test_list_skills(self, mock_loader_fn, client):
        skill = MagicMock()
        skill.name = "test-skill"
        skill.description = "A test skill"
        skill.argument_hint = "optional arg"
        loader = MagicMock()
        loader.get_invocable.return_value = [skill]
        mock_loader_fn.return_value = loader

        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test-skill"

    @patch("pocketpaw.skills.get_skill_loader")
    def test_list_empty_skills(self, mock_loader_fn, client):
        loader = MagicMock()
        loader.get_invocable.return_value = []
        mock_loader_fn.return_value = loader

        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200
        assert resp.json() == []


class TestSearchSkills:
    """Tests for GET /skills/search."""

    def test_search_empty_query(self, client):
        resp = client.get("/api/v1/skills/search?q=")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


class TestInstallSkill:
    """Tests for POST /skills/install."""

    def test_install_missing_source(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": ""})
        assert resp.status_code == 400

    def test_install_path_traversal_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "../evil/repo"})
        assert resp.status_code == 400

    def test_install_shell_injection_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner/repo;rm -rf"})
        assert resp.status_code == 400

    def test_install_invalid_format(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "single-part"})
        assert resp.status_code == 400

    # --- New whitelist validation tests (issue #748) ---

    def test_install_owner_with_spaces_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner with spaces/repo"})
        assert resp.status_code == 400

    def test_install_owner_with_dollar_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner$var/repo"})
        assert resp.status_code == 400

    def test_install_owner_with_at_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "@evil/repo"})
        assert resp.status_code == 400

    def test_install_owner_with_null_byte_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner\x00/repo"})
        assert resp.status_code == 400

    def test_install_repo_with_spaces_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner/repo name"})
        assert resp.status_code == 400

    def test_install_repo_with_backtick_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner/repo`cmd`"})
        assert resp.status_code == 400

    def test_install_skill_name_with_special_chars_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner/repo/skill name!"})
        assert resp.status_code == 400

    def test_install_skill_name_dotdot_blocked(self, client):
        """skill_name='..' must be rejected to prevent path traversal."""
        resp = client.post("/api/v1/skills/install", json={"source": "owner/repo/.."})
        assert resp.status_code == 400

    def test_install_source_too_many_parts_blocked(self, client):
        """source.split('/') with >3 parts must be rejected."""
        resp = client.post("/api/v1/skills/install", json={"source": "a/b/c/d"})
        assert resp.status_code == 400

    def test_install_owner_with_underscore_accepted(self, client):
        """GitHub usernames with underscores are valid and should pass validation."""
        resp = client.post("/api/v1/skills/install", json={"source": "user_name/repo"})
        # Should pass validation (non-400 means validation succeeded)
        assert resp.status_code != 400

    def test_install_valid_owner_repo_accepted(self, client):
        """Valid owner/repo should pass validation (git clone itself will fail in test env)."""
        # We just verify validation passes and the call reaches subprocess (which fails without git)
        resp = client.post("/api/v1/skills/install", json={"source": "valid-owner/valid.repo"})
        # Either 404/500/504 is acceptable — means validation passed
        assert resp.status_code != 400

    def test_install_git_stderr_not_leaked(self, client):
        """Git stderr must not appear verbatim in the response body."""
        secret_path = "/secret/internal/path/to/repos"

        async def _fake_communicate():
            return b"", f"fatal: repository not found at {secret_path}".encode()

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = _fake_communicate

        async def _fake_create_subprocess(*args, **kwargs):
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_create_subprocess):
            resp = client.post("/api/v1/skills/install", json={"source": "owner/repo"})

        assert resp.status_code == 500
        assert secret_path not in resp.text

    def test_install_audit_log_written_on_success(self, client):
        """A successful install must emit an audit log entry."""
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock

        async def _fake_communicate():
            return b"", b""

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = _fake_communicate

        async def _fake_create_subprocess(*args, **kwargs):
            return mock_proc

        mock_loader = MagicMock()
        mock_audit_logger = MagicMock()

        # Build the fake skill directory before applying patches.
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "my-skill"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("# My skill")

            mock_tmpdir_ctx = MagicMock()
            mock_tmpdir_ctx.__enter__ = MagicMock(return_value=tmpdir)
            mock_tmpdir_ctx.__exit__ = MagicMock(return_value=False)

            with (
                patch("asyncio.create_subprocess_exec", side_effect=_fake_create_subprocess),
                patch(
                    "pocketpaw.skills.installer.get_skill_loader",
                    return_value=mock_loader,
                ),
                patch(
                    "pocketpaw.skills.installer.get_audit_logger",
                    return_value=mock_audit_logger,
                ),
                patch("tempfile.TemporaryDirectory", return_value=mock_tmpdir_ctx),
                patch("pathlib.Path.mkdir"),
                patch("shutil.copytree"),
                patch("shutil.rmtree"),
            ):
                client.post("/api/v1/skills/install", json={"source": "owner/repo"})

        # The audit logger must have been called
        assert mock_audit_logger.log.called

    def test_install_symlinks_are_skipped(self, client):
        """Symlinks inside a cloned skill dir must not be dereferenced."""
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock

        async def _fake_communicate():
            return b"", b""

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = _fake_communicate

        async def _fake_create_subprocess(*args, **kwargs):
            return mock_proc

        mock_loader = MagicMock()
        mock_audit_logger = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "my-skill"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("# My skill")
            (skill_dir / "legit.txt").write_text("real content")
            # Create a symlink pointing outside the skill dir
            (skill_dir / "evil_link").symlink_to("/etc/passwd")

            mock_tmpdir_ctx = MagicMock()
            mock_tmpdir_ctx.__enter__ = MagicMock(return_value=tmpdir)
            mock_tmpdir_ctx.__exit__ = MagicMock(return_value=False)

            copytree_calls = []

            def _tracking_copytree(*args, **kwargs):
                copytree_calls.append(kwargs)
                # Don't actually copy, just record the call
                return None

            with (
                patch("asyncio.create_subprocess_exec", side_effect=_fake_create_subprocess),
                patch(
                    "pocketpaw.skills.installer.get_skill_loader",
                    return_value=mock_loader,
                ),
                patch(
                    "pocketpaw.skills.installer.get_audit_logger",
                    return_value=mock_audit_logger,
                ),
                patch("tempfile.TemporaryDirectory", return_value=mock_tmpdir_ctx),
                patch("pathlib.Path.mkdir"),
                patch("shutil.rmtree"),
                patch(
                    "pocketpaw.skills.installer.shutil.copytree",
                    side_effect=_tracking_copytree,
                ),
            ):
                resp = client.post("/api/v1/skills/install", json={"source": "owner/repo"})

            assert resp.status_code == 200
            # copytree must have been called with the ignore callback
            assert len(copytree_calls) >= 1
            assert "ignore" in copytree_calls[0]
            assert copytree_calls[0]["ignore"] is not None


class TestRemoveSkill:
    """Tests for POST /skills/remove."""

    def test_remove_missing_name(self, client):
        resp = client.post("/api/v1/skills/remove", json={"name": ""})
        assert resp.status_code == 400

    def test_remove_path_traversal_blocked(self, client):
        resp = client.post("/api/v1/skills/remove", json={"name": "../evil"})
        assert resp.status_code == 400

    def test_remove_slash_blocked(self, client):
        resp = client.post("/api/v1/skills/remove", json={"name": "evil/path"})
        assert resp.status_code == 400


class TestReloadSkills:
    """Tests for POST /skills/reload."""

    @patch("pocketpaw.skills.get_skill_loader")
    def test_reload(self, mock_loader_fn, client):
        skill = MagicMock()
        skill.user_invocable = True
        loader = MagicMock()
        loader.reload.return_value = {"test": skill}
        mock_loader_fn.return_value = loader

        resp = client.post("/api/v1/skills/reload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["count"] == 1
