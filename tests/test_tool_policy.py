"""Tests for the tool policy system."""

import pytest

from pocketclaw.tools.policy import TOOL_GROUPS, ToolPolicy


class TestToolGroups:
    """Verify group definitions are consistent."""

    def test_groups_contain_only_strings(self):
        for group, tools in TOOL_GROUPS.items():
            assert isinstance(tools, list)
            for t in tools:
                assert isinstance(t, str), f"{group} contains non-string: {t}"

    def test_group_keys_prefixed(self):
        for key in TOOL_GROUPS:
            assert key.startswith("group:"), f"Group key missing prefix: {key}"


class TestProfileResolution:
    """Test ToolPolicy.resolve_profile()."""

    def test_minimal_profile_memory_and_sessions(self):
        result = ToolPolicy.resolve_profile("minimal")
        assert result == {
            "remember",
            "recall",
            "forget",
            "new_session",
            "list_sessions",
            "switch_session",
            "clear_session",
            "rename_session",
            "delete_session",
        }

    def test_coding_profile_includes_fs_shell_memory(self):
        result = ToolPolicy.resolve_profile("coding")
        assert "shell" in result
        assert "read_file" in result
        assert "write_file" in result
        assert "remember" in result
        # Should NOT include browser
        assert "browser" not in result

    def test_full_profile_returns_empty_set(self):
        """Full profile means no restrictions — returns empty set."""
        result = ToolPolicy.resolve_profile("full")
        assert result == set()

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError, match="Unknown tool profile"):
            ToolPolicy.resolve_profile("nonexistent")


class TestGroupExpansion:
    """Test that group references expand to tool names."""

    def test_expand_single_group(self):
        result = ToolPolicy._expand_names(["group:fs"])
        assert result == {"read_file", "write_file", "edit_file", "list_dir"}

    def test_expand_multiple_groups(self):
        result = ToolPolicy._expand_names(["group:shell", "group:memory"])
        assert result == {"shell", "remember", "recall", "forget"}

    def test_expand_mixed_groups_and_names(self):
        result = ToolPolicy._expand_names(["group:memory", "custom_tool"])
        assert "remember" in result
        assert "recall" in result
        assert "custom_tool" in result

    def test_expand_unknown_group_kept_as_literal(self):
        result = ToolPolicy._expand_names(["group:nonexistent"])
        assert "group:nonexistent" in result


class TestToolPolicyAllow:
    """Test is_tool_allowed() with various configurations."""

    def test_full_profile_allows_everything(self):
        policy = ToolPolicy(profile="full")
        assert policy.is_tool_allowed("shell") is True
        assert policy.is_tool_allowed("browser") is True
        assert policy.is_tool_allowed("anything") is True

    def test_minimal_profile_blocks_shell(self):
        policy = ToolPolicy(profile="minimal")
        assert policy.is_tool_allowed("remember") is True
        assert policy.is_tool_allowed("recall") is True
        assert policy.is_tool_allowed("shell") is False
        assert policy.is_tool_allowed("browser") is False

    def test_coding_profile_allows_shell_and_fs(self):
        policy = ToolPolicy(profile="coding")
        assert policy.is_tool_allowed("shell") is True
        assert policy.is_tool_allowed("read_file") is True
        assert policy.is_tool_allowed("browser") is False

    def test_explicit_allow_merges_with_profile(self):
        """Explicit allow list is merged with the profile."""
        policy = ToolPolicy(profile="minimal", allow=["browser"])
        assert policy.is_tool_allowed("remember") is True  # from profile
        assert policy.is_tool_allowed("browser") is True  # from explicit allow
        assert policy.is_tool_allowed("shell") is False  # not in either

    def test_explicit_allow_with_group(self):
        policy = ToolPolicy(profile="minimal", allow=["group:browser"])
        assert policy.is_tool_allowed("browser") is True
        assert policy.is_tool_allowed("remember") is True
        assert policy.is_tool_allowed("shell") is False


class TestToolPolicyDeny:
    """Test deny list precedence."""

    def test_deny_overrides_full_profile(self):
        policy = ToolPolicy(profile="full", deny=["shell"])
        assert policy.is_tool_allowed("shell") is False
        assert policy.is_tool_allowed("browser") is True

    def test_deny_overrides_explicit_allow(self):
        """Deny has highest priority — even if tool is in allow list."""
        policy = ToolPolicy(profile="minimal", allow=["shell"], deny=["shell"])
        assert policy.is_tool_allowed("shell") is False
        assert policy.is_tool_allowed("remember") is True

    def test_deny_with_group(self):
        policy = ToolPolicy(profile="full", deny=["group:shell"])
        assert policy.is_tool_allowed("shell") is False
        assert policy.is_tool_allowed("browser") is True

    def test_deny_overrides_profile(self):
        policy = ToolPolicy(profile="coding", deny=["shell"])
        assert policy.is_tool_allowed("shell") is False
        assert policy.is_tool_allowed("read_file") is True


class TestToolPolicyFallback:
    """Test unknown profile fallback."""

    def test_unknown_profile_falls_back_to_full(self):
        """Unknown profile logs a warning and acts like 'full'."""
        policy = ToolPolicy(profile="nonexistent_profile")
        # Should allow everything (full fallback)
        assert policy.is_tool_allowed("shell") is True
        assert policy.is_tool_allowed("browser") is True


class TestFilterToolNames:
    """Test the filter_tool_names convenience method."""

    def test_filter_with_minimal(self):
        policy = ToolPolicy(profile="minimal")
        names = ["shell", "read_file", "remember", "recall", "browser"]
        result = policy.filter_tool_names(names)
        assert result == ["remember", "recall"]

    def test_filter_with_full(self):
        policy = ToolPolicy(profile="full")
        names = ["shell", "read_file", "remember"]
        result = policy.filter_tool_names(names)
        assert result == ["shell", "read_file", "remember"]

    def test_filter_with_deny(self):
        policy = ToolPolicy(profile="full", deny=["shell", "browser"])
        names = ["shell", "read_file", "remember", "browser"]
        result = policy.filter_tool_names(names)
        assert result == ["read_file", "remember"]


class TestRegistryPolicyIntegration:
    """Test that ToolRegistry respects the policy."""

    def test_registry_filters_definitions(self):
        from pocketclaw.tools.protocol import BaseTool
        from pocketclaw.tools.registry import ToolRegistry

        class FakeTool(BaseTool):
            @property
            def name(self):
                return self._name

            @property
            def description(self):
                return "test"

            def __init__(self, name):
                self._name = name

            async def execute(self, **params):
                return "ok"

        policy = ToolPolicy(profile="minimal")
        registry = ToolRegistry(policy=policy)
        registry.register(FakeTool("remember"))
        registry.register(FakeTool("shell"))
        registry.register(FakeTool("browser"))

        defs = registry.get_definitions(format="openai")
        # Only "remember" should pass the minimal policy
        names_in_defs = [d["function"]["name"] for d in defs]
        assert "remember" in names_in_defs
        assert "shell" not in names_in_defs
        assert "browser" not in names_in_defs

    @pytest.mark.asyncio
    async def test_registry_blocks_execution(self):
        from pocketclaw.tools.protocol import BaseTool
        from pocketclaw.tools.registry import ToolRegistry

        class FakeTool(BaseTool):
            @property
            def name(self):
                return "shell"

            @property
            def description(self):
                return "test"

            async def execute(self, **params):
                return "executed"

        policy = ToolPolicy(profile="minimal")
        registry = ToolRegistry(policy=policy)
        registry.register(FakeTool())

        result = await registry.execute("shell", command="ls")
        assert "not allowed" in result

    def test_registry_allowed_tool_names(self):
        from pocketclaw.tools.protocol import BaseTool
        from pocketclaw.tools.registry import ToolRegistry

        class FakeTool(BaseTool):
            @property
            def name(self):
                return self._name

            @property
            def description(self):
                return "test"

            def __init__(self, name):
                self._name = name

            async def execute(self, **params):
                return "ok"

        policy = ToolPolicy(profile="coding")
        registry = ToolRegistry(policy=policy)
        registry.register(FakeTool("shell"))
        registry.register(FakeTool("browser"))
        registry.register(FakeTool("remember"))

        assert "shell" in registry.allowed_tool_names
        assert "remember" in registry.allowed_tool_names
        assert "browser" not in registry.allowed_tool_names
        # tool_names should still show all registered
        assert "browser" in registry.tool_names


class TestMCPPolicy:
    """Test MCP-specific policy methods."""

    def test_full_profile_allows_all_mcp(self):
        policy = ToolPolicy(profile="full")
        assert policy.is_mcp_server_allowed("filesystem") is True
        assert policy.is_mcp_tool_allowed("filesystem", "read_file") is True

    def test_deny_specific_server(self):
        policy = ToolPolicy(profile="full", deny=["mcp:dangerous:*"])
        assert policy.is_mcp_server_allowed("dangerous") is False
        assert policy.is_mcp_server_allowed("safe") is True

    def test_deny_group_mcp(self):
        policy = ToolPolicy(profile="full", deny=["group:mcp"])
        assert policy.is_mcp_server_allowed("anything") is False
        assert policy.is_mcp_tool_allowed("anything", "tool") is False

    def test_deny_specific_tool(self):
        policy = ToolPolicy(profile="full", deny=["mcp:fs:delete_file"])
        assert policy.is_mcp_tool_allowed("fs", "delete_file") is False
        assert policy.is_mcp_tool_allowed("fs", "read_file") is True
        assert policy.is_mcp_server_allowed("fs") is True  # server itself ok

    def test_minimal_profile_blocks_mcp_unless_allowed(self):
        policy = ToolPolicy(profile="minimal")
        # minimal has only memory tools — MCP not in allowed set
        assert policy.is_mcp_server_allowed("fs") is False

    def test_allow_specific_mcp_server(self):
        policy = ToolPolicy(profile="minimal", allow=["mcp:fs:*"])
        assert policy.is_mcp_server_allowed("fs") is True
        assert policy.is_mcp_server_allowed("other") is False
