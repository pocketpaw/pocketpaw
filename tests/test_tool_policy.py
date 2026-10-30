"""Tests for the tool policy system."""

import pytest

from pocketpaw.tools.policy import TOOL_GROUPS, ToolPolicy


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
            "open_in_explorer",
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
        assert result == {"read_file", "write_file", "edit_file", "list_dir", "directory_tree"}

    def test_expand_multiple_groups(self):
        result = ToolPolicy._expand_names(["group:shell", "group:memory"])
        assert result == {"shell", "run_python", "remember", "recall", "forget"}

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
    """Unknown profile names fail closed (#889) — previously they silently
    fell back to 'full', which lifted every tool restriction on a typo."""

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError, match="Unknown tool profile"):
            ToolPolicy(profile="nonexistent_profile")


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
        from pocketpaw.tools.protocol import BaseTool
        from pocketpaw.tools.registry import ToolRegistry

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
        from pocketpaw.tools.protocol import BaseTool
        from pocketpaw.tools.registry import ToolRegistry

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
        from pocketpaw.tools.protocol import BaseTool
        from pocketpaw.tools.registry import ToolRegistry

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


class TestMCPExplicitAllow:
    """Test ``is_mcp_server_explicitly_allowed`` — opt-in gating for
    in-process built-in servers that must not be ambient on every agent run.

    The opt-in is driven by the dedicated ``mcp_servers_allow`` constructor
    argument, kept orthogonal to ``tools_allow``. Putting an ``mcp:*`` entry
    in ``tools_allow`` would flip the policy into allow-list mode and
    silently disable every other tool — ``mcp_servers_allow`` avoids that.
    """

    def test_empty_mcp_servers_allow_is_not_opt_in(self):
        """No ``mcp_servers_allow`` entry means the server stays off."""
        policy = ToolPolicy(profile="full")
        assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is False

    def test_full_profile_allow_list_does_not_opt_in(self):
        """A populated ``tools_allow`` does not leak into the MCP opt-in."""
        policy = ToolPolicy(profile="full", allow=["mcp:pocketpaw_planner:*"])
        assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is False

    def test_server_in_mcp_servers_allow_is_opt_in(self):
        policy = ToolPolicy(profile="full", mcp_servers_allow=frozenset({"pocketpaw_planner"}))
        assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is True

    def test_deny_overrides_mcp_servers_allow(self):
        """Deny always wins, even over an explicit ``mcp_servers_allow`` entry."""
        policy = ToolPolicy(
            profile="full",
            deny=["mcp:pocketpaw_planner:*"],
            mcp_servers_allow=frozenset({"pocketpaw_planner"}),
        )
        assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is False

    def test_group_mcp_deny_overrides_mcp_servers_allow(self):
        policy = ToolPolicy(
            profile="full",
            deny=["group:mcp"],
            mcp_servers_allow=frozenset({"pocketpaw_planner"}),
        )
        assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is False

    def test_unrelated_mcp_servers_allow_entry_does_not_leak(self):
        policy = ToolPolicy(profile="full", mcp_servers_allow=frozenset({"some_other_server"}))
        assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is False

    def test_mcp_servers_allow_defaults_to_empty(self):
        """Omitting the argument is equivalent to an empty frozenset."""
        policy = ToolPolicy(profile="full")
        assert policy.is_mcp_server_explicitly_allowed("anything") is False
