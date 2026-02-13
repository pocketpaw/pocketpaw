"""Tests for Skills Library — SkillLoader.search() + REST endpoints.

Covers:
  - SkillLoader.search() method
  - GET /api/skills (list installed)
  - GET /api/skills/search (proxy to skills.sh)
  - POST /api/skills/install (npx subprocess)
  - POST /api/skills/remove (npx subprocess)
  - POST /api/skills/reload (force reload)

Created: 2026-02-12
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketclaw.skills.loader import Skill, SkillLoader

# ======================================================================
# SkillLoader.search() tests
# ======================================================================


class TestSkillLoaderSearch:
    def _make_loader(self):
        """Create a loader with some test skills pre-loaded."""
        loader = SkillLoader(extra_paths=[])
        loader._loaded = True
        loader._skills = {
            "commit": Skill(
                name="commit",
                description="AI-powered commit messages",
                content="do a commit",
                path=Path("/fake/commit/SKILL.md"),
                user_invocable=True,
            ),
            "code-review": Skill(
                name="code-review",
                description="Review code changes for issues",
                content="review the code",
                path=Path("/fake/code-review/SKILL.md"),
                user_invocable=True,
            ),
            "internal-tool": Skill(
                name="internal-tool",
                description="Internal only tool",
                content="internal stuff",
                path=Path("/fake/internal/SKILL.md"),
                user_invocable=False,
            ),
            "web-design": Skill(
                name="web-design",
                description="Create beautiful web designs",
                content="design a page",
                path=Path("/fake/web-design/SKILL.md"),
                user_invocable=True,
            ),
        }
        return loader

    def test_search_empty_query_returns_all_invocable(self):
        loader = self._make_loader()
        results = loader.search("")
        assert len(results) == 3
        names = {s.name for s in results}
        assert "commit" in names
        assert "code-review" in names
        assert "web-design" in names
        assert "internal-tool" not in names

    def test_search_by_name(self):
        loader = self._make_loader()
        results = loader.search("commit")
        assert len(results) == 1
        assert results[0].name == "commit"

    def test_search_by_description(self):
        loader = self._make_loader()
        results = loader.search("beautiful")
        assert len(results) == 1
        assert results[0].name == "web-design"

    def test_search_case_insensitive(self):
        loader = self._make_loader()
        results = loader.search("CODE")
        assert len(results) == 1
        assert results[0].name == "code-review"

    def test_search_partial_match(self):
        loader = self._make_loader()
        results = loader.search("review")
        assert len(results) == 1
        assert results[0].name == "code-review"

    def test_search_no_match(self):
        loader = self._make_loader()
        results = loader.search("nonexistent")
        assert len(results) == 0

    def test_search_excludes_non_invocable(self):
        loader = self._make_loader()
        results = loader.search("internal")
        assert len(results) == 0

    def test_search_multiple_matches(self):
        loader = self._make_loader()
        # "code" matches code-review (name) — only one match
        results = loader.search("code")
        assert len(results) == 1

    def test_search_matches_name_and_description(self):
        loader = self._make_loader()
        # "commit" matches by name, "messages" matches description
        results = loader.search("messages")
        assert len(results) == 1
        assert results[0].name == "commit"


# ======================================================================
# REST Endpoint tests (mocked)
# ======================================================================


class TestSkillsRESTEndpoints:
    """Test the REST endpoints by importing from dashboard and calling directly."""

    @pytest.fixture
    def mock_loader(self):
        loader = MagicMock()
        loader.reload.return_value = {}
        loader.get_invocable.return_value = [
            Skill(
                name="test-skill",
                description="A test skill",
                content="test",
                path=Path("/fake/SKILL.md"),
                user_invocable=True,
                argument_hint="[query]",
            ),
        ]
        return loader

    async def test_list_installed_skills(self, mock_loader):
        """GET /api/skills returns installed invocable skills."""
        with patch("pocketclaw.dashboard.get_skill_loader", return_value=mock_loader):
            from pocketclaw.dashboard import list_installed_skills

            result = await list_installed_skills()
            assert len(result) == 1
            assert result[0]["name"] == "test-skill"
            assert result[0]["description"] == "A test skill"
            assert result[0]["argument_hint"] == "[query]"
            mock_loader.reload.assert_called_once()

    async def test_reload_skills(self, mock_loader):
        """POST /api/skills/reload reloads and returns count."""
        mock_loader.reload.return_value = {
            "a": Skill(
                name="a",
                description="",
                content="",
                path=Path("/f"),
                user_invocable=True,
            ),
            "b": Skill(
                name="b",
                description="",
                content="",
                path=Path("/f"),
                user_invocable=False,
            ),
        }
        with patch("pocketclaw.dashboard.get_skill_loader", return_value=mock_loader):
            from pocketclaw.dashboard import reload_skills

            result = await reload_skills()
            assert result["status"] == "ok"
            assert result["count"] == 1  # only 1 invocable

    async def test_search_skills_library_empty_query(self):
        """GET /api/skills/search with empty q returns empty list."""
        from pocketclaw.dashboard import search_skills_library

        result = await search_skills_library(q="", limit=30)
        assert result == {"skills": [], "count": 0}

    async def test_search_skills_library_proxies(self):
        """GET /api/skills/search proxies to skills.sh API."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "skills": [{"name": "react-skill", "installs": 1000}],
            "count": 1,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from pocketclaw.dashboard import search_skills_library

            result = await search_skills_library(q="react", limit=10)
            assert result == {
                "skills": [{"name": "react-skill", "installs": 1000}],
                "count": 1,
            }
            mock_client.get.assert_called_once_with(
                "https://skills.sh/api/search",
                params={"q": "react", "limit": 10},
            )

    async def test_install_skill_missing_source(self):
        """POST /api/skills/install with no source returns 400."""
        from pocketclaw.dashboard import install_skill

        request = MagicMock()
        request.json = AsyncMock(return_value={})

        result = await install_skill(request)
        # FastAPI JSONResponse
        assert result.status_code == 400

    async def test_install_skill_invalid_source(self):
        """POST /api/skills/install with dangerous chars returns 400."""
        from pocketclaw.dashboard import install_skill

        request = MagicMock()
        request.json = AsyncMock(return_value={"source": "foo; rm -rf /"})

        result = await install_skill(request)
        assert result.status_code == 400

    async def test_install_skill_success(self, mock_loader):
        """POST /api/skills/install clones repo, copies skill dir, reloads."""
        import tempfile

        request = MagicMock()
        request.json = AsyncMock(return_value={"source": "owner/repo/my-skill"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch("pocketclaw.dashboard.asyncio") as mock_asyncio,
            patch("pocketclaw.dashboard.get_skill_loader", return_value=mock_loader),
            tempfile.TemporaryDirectory() as fake_home,
        ):
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.TimeoutError = asyncio.TimeoutError

            # Make wait_for actually call the coroutine
            async def passthrough(coro, timeout):
                return await coro
            mock_asyncio.wait_for = passthrough

            # Prepare a fake cloned repo with a skill inside skills/ subdir
            from pathlib import Path

            async def fake_clone(*args, **kwargs):
                # args: "git", "clone", "--depth=1", url, tmpdir
                tmpdir = Path(args[4])
                skill_dir = tmpdir / "skills" / "my-skill"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: my-skill\ndescription: test\n---\nContent"
                )
                return mock_proc

            mock_asyncio.create_subprocess_exec = AsyncMock(side_effect=fake_clone)

            # Patch install dir to use temp dir
            install_path = Path(fake_home) / ".agents" / "skills"
            with patch("pathlib.Path.home", return_value=Path(fake_home)):
                from pocketclaw.dashboard import install_skill

                result = await install_skill(request)
                assert result["status"] == "ok"
                assert "my-skill" in result["installed"]
                assert (install_path / "my-skill" / "SKILL.md").exists()

    async def test_install_skill_clone_failure(self, mock_loader):
        """POST /api/skills/install returns error when git clone fails."""
        request = MagicMock()
        request.json = AsyncMock(return_value={"source": "owner/bad-repo/skill"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"fatal: repository not found\n")
        )
        mock_proc.returncode = 128

        with (
            patch("pocketclaw.dashboard.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.TimeoutError = asyncio.TimeoutError

            async def passthrough(coro, timeout):
                return await coro
            mock_asyncio.wait_for = passthrough

            from pocketclaw.dashboard import install_skill

            result = await install_skill(request)
            assert result.status_code == 500

    async def test_remove_skill_missing_name(self):
        """POST /api/skills/remove with no name returns 400."""
        from pocketclaw.dashboard import remove_skill

        request = MagicMock()
        request.json = AsyncMock(return_value={})

        result = await remove_skill(request)
        assert result.status_code == 400

    async def test_remove_skill_invalid_name(self):
        """POST /api/skills/remove with dangerous chars returns 400."""
        from pocketclaw.dashboard import remove_skill

        request = MagicMock()
        request.json = AsyncMock(return_value={"name": "foo|bar"})

        result = await remove_skill(request)
        assert result.status_code == 400

    async def test_remove_skill_success(self, mock_loader):
        """POST /api/skills/remove deletes skill dir and reloads."""
        import tempfile
        from pathlib import Path

        request = MagicMock()
        request.json = AsyncMock(return_value={"name": "old-skill"})

        with (
            tempfile.TemporaryDirectory() as fake_home,
            patch("pocketclaw.dashboard.get_skill_loader", return_value=mock_loader),
            patch("pathlib.Path.home", return_value=Path(fake_home)),
        ):
            # Create a fake installed skill
            skill_dir = Path(fake_home) / ".agents" / "skills" / "old-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: old-skill\n---\nContent")

            from pocketclaw.dashboard import remove_skill

            result = await remove_skill(request)
            assert result["status"] == "ok"
            assert not skill_dir.exists()

    async def test_remove_skill_not_found(self):
        """POST /api/skills/remove returns 404 for non-existent skill."""
        import tempfile
        from pathlib import Path

        request = MagicMock()
        request.json = AsyncMock(return_value={"name": "nonexistent"})

        with (
            tempfile.TemporaryDirectory() as fake_home,
            patch("pathlib.Path.home", return_value=Path(fake_home)),
        ):
            from pocketclaw.dashboard import remove_skill

            result = await remove_skill(request)
            assert result.status_code == 404


# ======================================================================
# MCPPreset.needs_args tests
# ======================================================================


class TestMCPPresetNeedsArgs:
    def test_filesystem_needs_args(self):
        from pocketclaw.mcp.presets import get_preset

        p = get_preset("filesystem")
        assert p is not None
        assert p.needs_args is True

    def test_postgres_needs_args(self):
        from pocketclaw.mcp.presets import get_preset

        p = get_preset("postgres")
        assert p is not None
        assert p.needs_args is True

    def test_sqlite_needs_args(self):
        from pocketclaw.mcp.presets import get_preset

        p = get_preset("sqlite")
        assert p is not None
        assert p.needs_args is True

    def test_github_does_not_need_args(self):
        from pocketclaw.mcp.presets import get_preset

        p = get_preset("github")
        assert p is not None
        assert p.needs_args is False

    def test_needs_args_in_preset_response(self):
        """list_mcp_presets includes needs_args in response."""
        from pocketclaw.mcp.presets import get_all_presets

        for p in get_all_presets():
            # Every preset should have a bool needs_args
            assert isinstance(p.needs_args, bool), f"Preset {p.id} needs_args is not bool"


# ======================================================================
# MCP Registry endpoint tests
# ======================================================================


class TestMCPRegistryEndpoints:
    async def test_search_registry_empty_query(self):
        """GET /api/mcp/registry/search with no query returns featured servers."""
        mock_response = MagicMock()
        # Registry API wraps each entry as {server: {...}, _meta: {...}}
        mock_response.json.return_value = {
            "servers": [
                {
                    "server": {"name": "org/server", "description": "A server"},
                    "_meta": {"score": 0.9},
                }
            ],
            "metadata": {"count": 1},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from pocketclaw.dashboard import search_mcp_registry

            result = await search_mcp_registry(q="", limit=30, cursor="")
            assert "servers" in result
            # Backend should unwrap nested structure
            assert result["servers"][0]["name"] == "org/server"
            assert result["servers"][0]["_meta"]["score"] == 0.9
            # Should call registry with just limit (no search param)
            mock_client.get.assert_called_once()
            call_kwargs = mock_client.get.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert "search" not in params

    async def test_search_registry_with_query(self):
        """GET /api/mcp/registry/search proxies search param to registry."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "servers": [{"server": {"name": "org/sql-server", "description": "SQL"}, "_meta": {}}],
            "metadata": {"count": 1, "nextCursor": "abc123"},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from pocketclaw.dashboard import search_mcp_registry

            result = await search_mcp_registry(q="sql", limit=10, cursor="")
            # Unwrapped: flat server object
            assert result["servers"][0]["name"] == "org/sql-server"
            call_kwargs = mock_client.get.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert params["search"] == "sql"
            assert params["limit"] == 10

    async def test_search_registry_with_cursor(self):
        """GET /api/mcp/registry/search passes cursor for pagination."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "servers": [],
            "metadata": {"count": 0},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from pocketclaw.dashboard import search_mcp_registry

            await search_mcp_registry(q="test", limit=30, cursor="page2")
            call_kwargs = mock_client.get.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert params["cursor"] == "page2"

    async def test_search_registry_limit_capped(self):
        """Limit is capped at 100."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"servers": [], "metadata": {"count": 0}}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from pocketclaw.dashboard import search_mcp_registry

            await search_mcp_registry(q="x", limit=999, cursor="")
            call_kwargs = mock_client.get.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert params["limit"] == 100

    async def test_install_from_registry_missing_name(self):
        """POST /api/mcp/registry/install with empty server name returns 400."""
        from pocketclaw.dashboard import install_from_registry

        request = MagicMock()
        request.json = AsyncMock(return_value={"server": {}, "env": {}})

        result = await install_from_registry(request)
        assert result.status_code == 400

    async def test_install_from_registry_http_remote(self):
        """Install from registry with HTTP remote transport (legacy transportType key)."""
        mock_mgr = MagicMock()
        mock_mgr.add_server_config = MagicMock()
        mock_mgr.start_server = AsyncMock(return_value=True)
        mock_mgr.discover_tools = MagicMock(return_value=[])

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "server": {
                    "name": "org/my-server",
                    "remotes": [{"url": "https://api.example.com/mcp", "transportType": "http"}],
                },
                "env": {},
            }
        )

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mock_mgr):
            from pocketclaw.dashboard import install_from_registry

            result = await install_from_registry(request)
            assert result["status"] == "ok"
            assert result["name"] == "my-server"
            assert result["connected"] is True
            # Verify the config was created with HTTP transport
            config = mock_mgr.add_server_config.call_args[0][0]
            assert config.transport == "http"
            assert config.url == "https://api.example.com/mcp"

    async def test_install_from_registry_streamable_http_remote(self):
        """Install from registry with streamable-http transport (actual registry API format)."""
        mock_mgr = MagicMock()
        mock_mgr.add_server_config = MagicMock()
        mock_mgr.start_server = AsyncMock(return_value=True)
        mock_mgr.discover_tools = MagicMock(return_value=[])

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "server": {
                    "name": "org/stream-server",
                    "remotes": [{"url": "https://api.example.com/mcp", "type": "streamable-http"}],
                },
                "env": {},
            }
        )

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mock_mgr):
            from pocketclaw.dashboard import install_from_registry

            result = await install_from_registry(request)
            assert result["status"] == "ok"
            assert result["name"] == "stream-server"
            # streamable-http is preserved (needs different MCP SDK client)
            config = mock_mgr.add_server_config.call_args[0][0]
            assert config.transport == "streamable-http"
            assert config.url == "https://api.example.com/mcp"

    async def test_install_from_registry_npm_package(self):
        """Install from registry with npm package (stdio)."""
        mock_mgr = MagicMock()
        mock_mgr.add_server_config = MagicMock()
        mock_mgr.start_server = AsyncMock(return_value=False)
        mock_mgr.discover_tools = MagicMock(return_value=[])

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "server": {
                    "name": "org/cool-mcp",
                    "packages": [
                        {
                            "registryType": "npm",
                            "name": "@cool/mcp-server",
                            "runtime": "node",
                            "packageArguments": [],
                        }
                    ],
                },
                "env": {"API_KEY": "test123"},
            }
        )

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mock_mgr):
            from pocketclaw.dashboard import install_from_registry

            result = await install_from_registry(request)
            assert result["status"] == "ok"
            assert result["name"] == "cool-mcp"
            config = mock_mgr.add_server_config.call_args[0][0]
            assert config.transport == "stdio"
            assert config.command == "npx"
            assert "-y" in config.args
            assert "@cool/mcp-server" in config.args
            assert config.env == {"API_KEY": "test123"}

    async def test_install_from_registry_pypi_package(self):
        """Install from registry with pypi package (uvx)."""
        mock_mgr = MagicMock()
        mock_mgr.add_server_config = MagicMock()
        mock_mgr.start_server = AsyncMock(return_value=True)
        mock_mgr.discover_tools = MagicMock(return_value=[])

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "server": {
                    "name": "org/py-server",
                    "packages": [
                        {"registryType": "pypi", "name": "mcp-py-server", "runtime": "python"}
                    ],
                },
                "env": {},
            }
        )

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mock_mgr):
            from pocketclaw.dashboard import install_from_registry

            result = await install_from_registry(request)
            assert result["status"] == "ok"
            config = mock_mgr.add_server_config.call_args[0][0]
            assert config.command == "uvx"
            assert "mcp-py-server" in config.args

    async def test_install_from_registry_docker_package(self):
        """Install from registry with docker package."""
        mock_mgr = MagicMock()
        mock_mgr.add_server_config = MagicMock()
        mock_mgr.start_server = AsyncMock(return_value=True)
        mock_mgr.discover_tools = MagicMock(return_value=[])

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "server": {
                    "name": "org/docker-srv",
                    "packages": [
                        {
                            "registryType": "docker",
                            "name": "ghcr.io/org/mcp-docker",
                            "runtimeArguments": [
                                {"isFixed": True, "value": "-p"},
                                {"isFixed": True, "value": "3000:3000"},
                            ],
                        }
                    ],
                },
                "env": {},
            }
        )

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mock_mgr):
            from pocketclaw.dashboard import install_from_registry

            result = await install_from_registry(request)
            assert result["status"] == "ok"
            config = mock_mgr.add_server_config.call_args[0][0]
            assert config.command == "docker"
            assert "run" in config.args
            assert "-p" in config.args
            assert "ghcr.io/org/mcp-docker" in config.args

    async def test_install_from_registry_no_install_method(self):
        """POST /api/mcp/registry/install with no packages or remotes returns 400."""
        from pocketclaw.dashboard import install_from_registry

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "server": {"name": "org/empty-server"},
                "env": {},
            }
        )

        result = await install_from_registry(request)
        assert result.status_code == 400
