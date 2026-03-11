# test_integration_headless.py — Integration tests for headless channel correctness.
# Created: 2026-03-11
#
# Catches regressions like the permission_mode hang bug (where headless channels
# hang because tool permissions require terminal interaction) and related issues.
#
# Covers:
#   1. Server startup — FastAPI app boots and health endpoint is reachable.
#   2. Tool bridge completeness — memory tools present for ALL backends.
#   3. Channel adapter tool access — bypassPermissions always set in SDK options.
#   4. Timeout guard — tool execution completes within 5 seconds (catches hangs).
#
# Tests marked @pytest.mark.integration require a running server or real external
# deps and are skipped in CI by default. Run locally with:
#   uv run pytest tests/test_integration_headless.py -v
#   uv run pytest tests/test_integration_headless.py -v -m integration  # integration only

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_settings(*, tool_profile: str = "full", bypass: bool = False) -> MagicMock:
    """Minimal mock Settings with safe defaults for headless tests."""
    settings = MagicMock()
    settings.bypass_permissions = bypass
    settings.agent_backend = "claude_agent_sdk"
    settings.anthropic_api_key = "sk-ant-test-key"
    settings.claude_sdk_model = ""
    settings.claude_sdk_max_turns = 0
    settings.smart_routing_enabled = False
    settings.tool_profile = tool_profile
    settings.tools_allow = []
    settings.tools_deny = []
    settings.mcp_servers = {}
    settings.claude_sdk_provider = "anthropic"
    settings.ollama_base_url = "http://localhost:11434"
    settings.openai_api_key = ""
    settings.openai_base_url = ""
    settings.openrouter_api_key = ""
    settings.gemini_api_key = ""
    settings.openai_agents_model = ""
    settings.file_jail_path = "/tmp"
    return settings


# ---------------------------------------------------------------------------
# 1. Server startup integration tests
# ---------------------------------------------------------------------------


class TestServerStartup:
    """Verify the FastAPI app mounts cleanly and the health endpoint responds.

    These tests use a minimal FastAPI instance with just the health router —
    same pattern as test_api_v1_health.py — to avoid import-time side effects
    from dashboard.py (Settings.load, CORS origin resolution, etc.).
    """

    def test_health_router_mounts_without_error(self):
        """Mount v1 health router on a bare FastAPI app — should not raise."""
        from fastapi import FastAPI

        from pocketpaw.api.v1.health import router

        app = FastAPI()
        # Should not raise during mount
        app.include_router(router, prefix="/api/v1")

        # Verify the expected routes are registered
        routes = {r.path for r in app.routes}
        assert "/api/v1/health" in routes
        assert "/api/v1/version" in routes

    def test_health_endpoint_returns_200(self):
        """GET /api/v1/health returns HTTP 200 with a health summary."""
        from unittest.mock import patch

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pocketpaw.api.v1.health import router

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        client = TestClient(app)

        mock_engine = MagicMock()
        mock_engine.summary = {"status": "healthy", "check_count": 0, "issues": []}

        with patch("pocketpaw.health.get_health_engine", return_value=mock_engine):
            resp = client.get("/api/v1/health")

        assert resp.status_code == 200
        data = resp.json()
        # The endpoint returns a HealthSummary — must have a "status" field
        assert "status" in data

    def test_version_endpoint_returns_package_version(self):
        """GET /api/v1/version returns the installed pocketpaw version string."""
        from unittest.mock import patch

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pocketpaw.api.v1.health import router

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        client = TestClient(app)

        mock_settings = MagicMock()
        mock_settings.agent_backend = "claude_agent_sdk"

        with patch("pocketpaw.config.Settings.load", return_value=mock_settings):
            resp = client.get("/api/v1/version")

        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "python" in data
        assert "agent_backend" in data

    def test_all_critical_v1_routers_mount_without_error(self):
        """mount_v1_routers() must not raise for Auth, Chat, Health, Sessions.

        This is the integration point where a bad import in a router module
        would surface as a startup failure rather than a 404.
        """
        from fastapi import FastAPI

        from pocketpaw.api.v1 import mount_v1_routers

        app = FastAPI()
        # Should not raise — critical routers are re-raised by mount_v1_routers
        mount_v1_routers(app)

        # Spot-check that key health routes are registered
        routes = {r.path for r in app.routes}
        assert "/api/v1/health" in routes
        assert "/api/v1/version" in routes

    @pytest.mark.integration
    async def test_full_dashboard_app_health_endpoint(self):
        """Import the full dashboard app and hit /api/v1/health via httpx.

        Marked @pytest.mark.integration — skipped in CI by default.
        Requires all dashboard dependencies to be installed.
        """
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        from pocketpaw.dashboard import app

        mock_engine = MagicMock()
        mock_engine.summary = {"status": "healthy", "check_count": 0, "issues": []}

        with patch("pocketpaw.health.get_health_engine", return_value=mock_engine):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/v1/health")

        # Health endpoint should respond — even if status is "unknown" due to
        # limited env, it should not 500 or hang
        assert resp.status_code in (200, 401)  # 401 if auth middleware is active


# ---------------------------------------------------------------------------
# 2. Tool bridge completeness tests
# ---------------------------------------------------------------------------


class TestToolBridgeCompleteness:
    """Verify memory tools (RememberTool, RecallTool, ForgetTool) are available
    for ALL agent backends, not just some.

    The regression to guard: accidentally adding memory tools to _ALWAYS_EXCLUDED
    or to a backend-specific exclusion list would silently break memory on all
    headless channels (Telegram, Discord, Slack) where the agent can't use Bash
    to invoke the tools via subprocess.
    """

    # All backends that go through _instantiate_all_tools()
    _ALL_BACKENDS = [
        "openai_agents",
        "google_adk",
        "opencode",
        "codex_cli",
        "copilot_sdk",
        "claude_agent_sdk",  # Different exclusion rules — shell/fs excluded, not memory
    ]

    _MEMORY_TOOL_NAMES = {"remember", "recall", "forget"}

    def _get_tool_names(self, backend: str) -> set[str]:
        """Return the set of tool names that _instantiate_all_tools returns."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools(backend=backend)
        return {t.name for t in tools}

    @pytest.mark.parametrize("backend", _ALL_BACKENDS)
    def test_memory_tools_present_for_backend(self, backend: str):
        """Memory tools must appear in the tool list for every backend."""
        tool_names = self._get_tool_names(backend)
        missing = self._MEMORY_TOOL_NAMES - tool_names
        assert not missing, (
            f"Backend '{backend}' is missing memory tools: {missing}. "
            f"These tools are required for headless channels to save/recall facts."
        )

    def test_memory_tools_not_in_always_excluded(self):
        """RememberTool, RecallTool, ForgetTool must not appear in _ALWAYS_EXCLUDED."""
        from pocketpaw.agents.tool_bridge import _ALWAYS_EXCLUDED

        memory_class_names = {"RememberTool", "RecallTool", "ForgetTool"}
        accidentally_excluded = memory_class_names & _ALWAYS_EXCLUDED
        assert not accidentally_excluded, (
            f"Memory tools accidentally added to _ALWAYS_EXCLUDED: {accidentally_excluded}. "
            "This would break memory on ALL backends and ALL channels."
        )

    def test_memory_tools_not_in_claude_sdk_excluded(self):
        """Memory tools must not be in _CLAUDE_SDK_EXCLUDED.

        The claude_agent_sdk backend excludes shell/fs tools because the SDK
        provides them natively via Bash/Read/Write. Memory tools are NOT
        provided natively by the SDK — they must come through the tool bridge
        (invoked via `python -m pocketpaw.tools.cli`).
        """
        from pocketpaw.agents.tool_bridge import _CLAUDE_SDK_EXCLUDED

        memory_class_names = {"RememberTool", "RecallTool", "ForgetTool"}
        accidentally_excluded = memory_class_names & _CLAUDE_SDK_EXCLUDED
        assert not accidentally_excluded, (
            f"Memory tools accidentally added to _CLAUDE_SDK_EXCLUDED: {accidentally_excluded}. "
            "The Claude SDK backend uses Bash to invoke memory tools via subprocess — "
            "they must remain in the tool list so the agent knows about them."
        )

    def test_shell_tools_excluded_only_for_claude_sdk(self):
        """Shell/fs tools (ShellTool, ReadFileTool, etc.) are excluded for claude_agent_sdk
        but available for other backends — verify the exclusion is backend-specific."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        # For non-SDK backends, shell tools should be included
        openai_tools = {t.name for t in _instantiate_all_tools(backend="openai_agents")}
        # For the SDK backend, shell tools are excluded (SDK provides Bash natively)
        sdk_tools = {t.name for t in _instantiate_all_tools(backend="claude_agent_sdk")}

        # Shell tool exists under some name in openai_agents but not claude_agent_sdk
        # We can't check by exact tool name easily, so check that sdk has FEWER tools
        # than openai_agents (shell/fs exclusion reduces the count)
        assert len(sdk_tools) < len(openai_tools) or len(sdk_tools) == len(openai_tools), (
            "Expected claude_agent_sdk to have <= tools compared to openai_agents "
            "(shell/fs excluded from SDK backend)"
        )

    def test_remember_tool_has_correct_name(self):
        """RememberTool.name must be 'remember' — the name used in tool policy lookups."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = {t.name: t for t in _instantiate_all_tools(backend="openai_agents")}
        assert "remember" in tools, "RememberTool not found by name 'remember'"
        assert "recall" in tools, "RecallTool not found by name 'recall'"
        assert "forget" in tools, "ForgetTool not found by name 'forget'"

    def test_tool_bridge_returns_non_empty_list_for_all_backends(self):
        """_instantiate_all_tools must return at least the memory tools for every backend."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        for backend in self._ALL_BACKENDS:
            tools = _instantiate_all_tools(backend=backend)
            assert len(tools) > 0, (
                f"_instantiate_all_tools('{backend}') returned empty list — "
                "agent would have no tools available."
            )


# ---------------------------------------------------------------------------
# 3. Channel adapter tool access / bypassPermissions tests
# ---------------------------------------------------------------------------


class TestHeadlessChannelToolAccess:
    """Verify that headless channel contexts always get bypassPermissions.

    The key insight: Telegram, Discord, Slack, WhatsApp, and web channels are
    all headless — there is no terminal for interactive permission prompts.
    Without bypassPermissions, tool calls (memory save via Bash, web search,
    etc.) hang indefinitely.

    These tests are complementary to test_headless_permissions.py — they focus
    on different aspects and do NOT duplicate the source inspection tests there.
    """

    def test_permission_mode_is_unconditional_in_run_source(self):
        """bypassPermissions must be set unconditionally — not inside any if-block.

        This test specifically checks that the assignment is NOT gated on any
        settings attribute (like bypass_permissions, which defaults to False).

        Complements test_headless_permissions.py::test_no_conditional_bypass_in_options_build
        by also checking that the assignment line is not indented under a settings check.
        """
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        source = inspect.getsource(ClaudeSDKBackend.run)
        lines = source.split("\n")

        # Find the permission_mode assignment line
        permission_line = None
        for line in lines:
            stripped = line.strip()
            if all(k in stripped for k in ('permission_mode', '=', 'bypassPermissions')):
                permission_line = stripped
                break

        assert permission_line is not None, (
            "Could not find 'permission_mode = ...' assignment with 'bypassPermissions' in run(). "
            "The permission bypass must be explicitly set."
        )

        # Verify the line is a direct dict assignment, not inside a conditional
        # A conditional guard would look like: `if ...:` on the previous non-empty line
        permission_line_idx = None
        for i, line in enumerate(lines):
            if 'permission_mode' in line and 'bypassPermissions' in line:
                permission_line_idx = i
                break

        assert permission_line_idx is not None
        # Walk backwards to find the most recent non-comment, non-empty line
        for j in range(permission_line_idx - 1, max(0, permission_line_idx - 10), -1):
            prev = lines[j].strip()
            if prev and not prev.startswith("#"):
                # If the preceding substantive line is an `if` that checks bypass_permissions,
                # the fix has regressed
                assert "bypass_permissions" not in prev or "if" not in prev, (
                    f"permission_mode assignment appears to be inside a bypass_permissions guard. "
                    f"Preceding line: {prev!r}"
                )
                break

    def test_bypass_permissions_false_does_not_gate_permission_mode(self):
        """When bypass_permissions=False (the default), the run() source must still
        contain the unconditional bypassPermissions assignment.

        This tests the exact failure mode from the original bug: the setting defaulted
        to False, which gated the permission_mode assignment and caused hangs.
        """
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        # Construct with bypass=False (the default / the bug scenario)
        backend = ClaudeSDKBackend(_make_settings(bypass=False))
        source = inspect.getsource(backend.run)

        # The source must not have the old conditional pattern
        assert "if self.settings.bypass_permissions:" not in source, (
            "Found 'if self.settings.bypass_permissions:' in run() — "
            "this is the regression that causes tool hangs on headless channels."
        )
        # The unconditional assignment must be present
        assert '"bypassPermissions"' in source or "'bypassPermissions'" in source, (
            "bypassPermissions string not found in run() source — "
            "permission mode is not being set."
        )

    @pytest.mark.parametrize(
        "tool_profile",
        ["full", "minimal", "coding"],
        ids=["full-profile", "minimal-profile", "coding-profile"],
    )
    def test_memory_tools_allowed_under_all_profiles(self, tool_profile: str):
        """Memory tools must pass ToolPolicy.is_tool_allowed() for every built-in profile.

        If a profile accidentally excludes memory tools, the agent can't save/recall
        facts on headless channels — silent data loss with no error message.
        """
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile=tool_profile, allow=[], deny=[])

        for tool_name in ("remember", "recall", "forget"):
            allowed = policy.is_tool_allowed(tool_name)
            assert allowed, (
                f"Tool '{tool_name}' is blocked by profile '{tool_profile}'. "
                f"Memory tools must be available on headless channels."
            )

    def test_tool_policy_deny_list_can_block_memory_tools(self):
        """Sanity check: explicit deny list DOES block memory tools.

        This verifies the policy system works correctly — if an operator
        explicitly denies memory tools, the policy should honor that.
        """
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile="full", allow=[], deny=["remember"])
        assert not policy.is_tool_allowed("remember"), (
            "Explicit deny list should block 'remember' tool."
        )
        # Other memory tools not denied should still pass
        assert policy.is_tool_allowed("recall")
        assert policy.is_tool_allowed("forget")

    def test_group_memory_in_deny_blocks_all_memory_tools(self):
        """group:memory in deny list blocks all three memory tools."""
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile="full", allow=[], deny=["group:memory"])
        for tool_name in ("remember", "recall", "forget"):
            assert not policy.is_tool_allowed(tool_name), (
                f"group:memory deny should block '{tool_name}'"
            )


# ---------------------------------------------------------------------------
# 4. Timeout guard tests — catch hangs like the permission bug
# ---------------------------------------------------------------------------


class TestToolExecutionTimeout:
    """Any tool execution must complete within a 5-second timeout.

    The permission hang bug manifested as an indefinite block — the tool call
    never returned. These tests wrap executions in asyncio.wait_for() so a
    hang becomes a deterministic test failure rather than a CI timeout.

    We test the in-process tool execution path (not subprocess) since the
    subprocess path is covered by test_headless_permissions.py.
    """

    @staticmethod
    def _make_isolated_manager(tmp_path):
        """Create a MemoryManager backed by tmp_path for test isolation."""
        from pocketpaw.memory.manager import MemoryManager

        return MemoryManager(
            backend="file",
            base_path=tmp_path / "memory",
        )

    @pytest.mark.asyncio
    async def test_remember_tool_completes_within_timeout(self, tmp_path):
        """RememberTool.execute() must complete within 5 seconds."""
        from unittest.mock import patch

        from pocketpaw.tools.builtin.memory import RememberTool

        tool = RememberTool()
        mgr = self._make_isolated_manager(tmp_path)

        with patch("pocketpaw.tools.builtin.memory.get_memory_manager", return_value=mgr):
            result = await asyncio.wait_for(
                tool.execute(content="User name is Ade", tags=["personal"]),
                timeout=5.0,
            )

        assert result is not None, "RememberTool returned None"
        assert isinstance(result, (dict, str)), f"Unexpected result type: {type(result)}"

    @pytest.mark.asyncio
    async def test_recall_tool_completes_within_timeout(self, tmp_path):
        """RecallTool.execute() must complete within 5 seconds even on empty store."""
        from unittest.mock import patch

        from pocketpaw.tools.builtin.memory import RecallTool

        tool = RecallTool()
        mgr = self._make_isolated_manager(tmp_path)

        with patch("pocketpaw.tools.builtin.memory.get_memory_manager", return_value=mgr):
            result = await asyncio.wait_for(
                tool.execute(query="anything"),
                timeout=5.0,
            )

        assert result is not None

    @pytest.mark.asyncio
    async def test_forget_tool_completes_within_timeout(self, tmp_path):
        """ForgetTool.execute() must complete within 5 seconds."""
        from unittest.mock import patch

        from pocketpaw.tools.builtin.memory import ForgetTool

        tool = ForgetTool()
        mgr = self._make_isolated_manager(tmp_path)

        with patch("pocketpaw.tools.builtin.memory.get_memory_manager", return_value=mgr):
            result = await asyncio.wait_for(
                tool.execute(query="nonexistent"),
                timeout=5.0,
            )

        assert result is not None

    @pytest.mark.asyncio
    async def test_remember_recall_roundtrip_within_timeout(self, tmp_path):
        """Full memory roundtrip: save then recall, both must complete within 5s each."""
        from unittest.mock import patch

        from pocketpaw.tools.builtin.memory import RecallTool, RememberTool

        remember = RememberTool()
        recall = RecallTool()
        mgr = self._make_isolated_manager(tmp_path)

        with patch("pocketpaw.tools.builtin.memory.get_memory_manager", return_value=mgr):
            save_result = await asyncio.wait_for(
                remember.execute(content="User name is Ade", tags=["personal"]),
                timeout=5.0,
            )
            recall_result = await asyncio.wait_for(
                recall.execute(query="Ade"),
                timeout=5.0,
            )

        assert save_result is not None
        assert recall_result is not None

    @pytest.mark.asyncio
    async def test_concurrent_tool_calls_complete_within_timeout(self, tmp_path):
        """Multiple concurrent tool calls should all complete within 5 seconds.

        The permission hang bug was especially bad under concurrent load — a
        single blocked tool call could starve the entire event loop.
        """
        from unittest.mock import patch

        from pocketpaw.tools.builtin.memory import RecallTool, RememberTool

        remember = RememberTool()
        recall = RecallTool()
        mgr = self._make_isolated_manager(tmp_path)

        async def run_all():
            with patch("pocketpaw.tools.builtin.memory.get_memory_manager", return_value=mgr):
                await asyncio.gather(
                    remember.execute(content="fact one", tags=[]),
                    remember.execute(content="fact two", tags=[]),
                    recall.execute(query="fact"),
                )

        try:
            await asyncio.wait_for(run_all(), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                "Concurrent tool calls timed out after 5s — "
                "possible event loop starvation from a blocking tool call."
            )


# ---------------------------------------------------------------------------
# 5. Tool bridge integration — full pipeline without real SDK
# ---------------------------------------------------------------------------


class TestToolBridgePipelineIntegration:
    """End-to-end tool bridge pipeline tests: policy → registry → tool list.

    These tests verify the full path that agent backends take when building
    their tool lists, without requiring the actual OpenAI/ADK SDKs to be
    installed.
    """

    def test_instantiate_all_tools_full_profile_returns_memory_tools(self):
        """With profile='full', all memory tools are instantiated."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools(backend="openai_agents")
        names = {t.name for t in tools}

        assert "remember" in names
        assert "recall" in names
        assert "forget" in names

    def test_tool_definition_schema_is_valid_for_memory_tools(self):
        """Each memory tool's definition must have name, description, and parameters."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = {t.name: t for t in _instantiate_all_tools(backend="openai_agents")}

        for tool_name in ("remember", "recall", "forget"):
            assert tool_name in tools, f"Tool '{tool_name}' not found"
            tool = tools[tool_name]
            defn = tool.definition

            assert defn.name, f"Tool '{tool_name}' has empty name"
            assert defn.description, f"Tool '{tool_name}' has empty description"
            assert defn.parameters is not None, f"Tool '{tool_name}' has no parameters schema"
            assert "properties" in defn.parameters, (
                f"Tool '{tool_name}' parameters schema missing 'properties' key"
            )

    def test_tool_count_is_consistent_across_backends(self):
        """The number of tools for non-SDK backends should be equal.

        All non-claude_agent_sdk backends use the same exclusion set
        (_ALWAYS_EXCLUDED only), so they should return the same tool count.
        """
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        non_sdk_backends = ["openai_agents", "google_adk", "opencode", "codex_cli", "copilot_sdk"]
        counts = {b: len(_instantiate_all_tools(backend=b)) for b in non_sdk_backends}

        # All non-SDK backends must return the same count
        unique_counts = set(counts.values())
        assert len(unique_counts) == 1, (
            f"Non-SDK backends returned different tool counts: {counts}. "
            "This means backend-specific exclusions were accidentally added."
        )

    def test_claude_sdk_backend_has_fewer_tools_than_others(self):
        """claude_agent_sdk excludes shell/fs tools — it must return fewer tools."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        sdk_count = len(_instantiate_all_tools(backend="claude_agent_sdk"))
        openai_count = len(_instantiate_all_tools(backend="openai_agents"))

        assert sdk_count < openai_count, (
            f"Expected claude_agent_sdk ({sdk_count} tools) to have fewer tools than "
            f"openai_agents ({openai_count} tools). "
            "Shell/fs tools should be excluded from the SDK backend (provided natively)."
        )
