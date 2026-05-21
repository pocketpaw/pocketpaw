"""Tests for the backend protocol, BackendInfo, and Capability flags."""

import inspect

import pytest

from pocketpaw.agents.backend import _DEFAULT_IDENTITY, AgentBackend, BackendInfo, Capability


class TestDefaultIdentity:
    def test_default_identity_is_nonempty(self):
        """_DEFAULT_IDENTITY must be a non-empty string used as the system-prompt fallback."""
        assert isinstance(_DEFAULT_IDENTITY, str)
        assert len(_DEFAULT_IDENTITY.strip()) > 0

    def test_default_identity_mentions_pocketpaw(self):
        """The fallback identity should at minimum identify the agent as PocketPaw."""
        assert "PocketPaw" in _DEFAULT_IDENTITY


class TestCapability:
    def test_individual_flags(self):
        assert Capability.STREAMING.value != 0
        assert Capability.TOOLS.value != 0
        assert Capability.MCP.value != 0

    def test_flag_combination(self):
        combo = Capability.STREAMING | Capability.TOOLS | Capability.MCP
        assert Capability.STREAMING in combo
        assert Capability.TOOLS in combo
        assert Capability.MCP in combo
        assert Capability.MULTI_TURN not in combo

    def test_all_capabilities(self):
        all_caps = (
            Capability.STREAMING
            | Capability.TOOLS
            | Capability.MCP
            | Capability.MULTI_TURN
            | Capability.CUSTOM_SYSTEM_PROMPT
        )
        assert Capability.STREAMING in all_caps
        assert Capability.CUSTOM_SYSTEM_PROMPT in all_caps


class TestBackendInfo:
    def test_creation(self):
        info = BackendInfo(
            name="test",
            display_name="Test Backend",
            capabilities=Capability.STREAMING | Capability.TOOLS,
        )
        assert info.name == "test"
        assert info.display_name == "Test Backend"
        assert Capability.STREAMING in info.capabilities

    def test_frozen(self):
        info = BackendInfo(name="test", display_name="Test", capabilities=Capability.STREAMING)
        import pytest

        with pytest.raises(AttributeError):
            info.name = "changed"

    def test_defaults(self):
        info = BackendInfo(name="test", display_name="Test", capabilities=Capability.STREAMING)
        assert info.builtin_tools == []
        assert info.tool_policy_map == {}
        assert info.required_keys == []
        assert info.supported_providers == []

    def test_required_keys_and_supported_providers(self):
        info = BackendInfo(
            name="test",
            display_name="Test",
            capabilities=Capability.STREAMING,
            required_keys=["api_key_1"],
            supported_providers=["provider_a", "provider_b"],
        )
        assert info.required_keys == ["api_key_1"]
        assert info.supported_providers == ["provider_a", "provider_b"]

    def test_with_tools(self):
        info = BackendInfo(
            name="test",
            display_name="Test",
            capabilities=Capability.STREAMING,
            builtin_tools=["Bash", "Read"],
            tool_policy_map={"Bash": "shell", "Read": "read_file"},
        )
        assert "Bash" in info.builtin_tools
        assert info.tool_policy_map["Bash"] == "shell"


class TestAgentBackendProtocol:
    def test_run_has_session_key_param(self):
        """AgentBackend.run() protocol includes session_key parameter."""
        sig = inspect.signature(AgentBackend.run)
        assert "session_key" in sig.parameters
        param = sig.parameters["session_key"]
        assert param.default is None
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


class TestToolPolicyProtocol:
    """get_tool_policy / set_tool_policy are present on every backend class."""

    BACKEND_CLASSES = [
        "pocketpaw.agents.claude_sdk.ClaudeSDKBackend",
        "pocketpaw.agents.openai_agents.OpenAIAgentsBackend",
        "pocketpaw.agents.google_adk.GoogleADKBackend",
        "pocketpaw.agents.codex_cli.CodexCLIBackend",
        "pocketpaw.agents.opencode.OpenCodeBackend",
        "pocketpaw.agents.copilot_sdk.CopilotSDKBackend",
        "pocketpaw.agents.deep_agents.DeepAgentsBackend",
    ]

    @pytest.mark.parametrize("dotted_path", BACKEND_CLASSES)
    def test_has_get_tool_policy(self, dotted_path):
        import importlib

        module_path, cls_name = dotted_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)
        assert callable(getattr(cls, "get_tool_policy", None)), (
            f"{cls_name} missing get_tool_policy()"
        )

    @pytest.mark.parametrize("dotted_path", BACKEND_CLASSES)
    def test_has_set_tool_policy(self, dotted_path):
        import importlib

        module_path, cls_name = dotted_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)
        assert callable(getattr(cls, "set_tool_policy", None)), (
            f"{cls_name} missing set_tool_policy()"
        )

    @pytest.mark.parametrize("dotted_path", BACKEND_CLASSES)
    def test_round_trip(self, dotted_path):
        """set_tool_policy(p) then get_tool_policy() must return the exact same object."""
        import importlib

        from pocketpaw.tools.policy import ToolPolicy

        module_path, cls_name = dotted_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)

        instance = cls.__new__(cls)
        policy = ToolPolicy(profile="full", deny=["group:shell"])
        instance.set_tool_policy(policy)
        assert instance.get_tool_policy() is policy, (
            f"{cls_name}.get_tool_policy() did not return the policy passed to set_tool_policy()"
        )

    @pytest.mark.parametrize(
        "dotted_path,extra_cache_attrs",
        [
            ("pocketpaw.agents.openai_agents.OpenAIAgentsBackend", ["_custom_tools"]),
            ("pocketpaw.agents.google_adk.GoogleADKBackend", ["_custom_tools"]),
            ("pocketpaw.agents.deep_agents.DeepAgentsBackend", ["_custom_tools", "_mcp_tools"]),
        ],
    )
    def test_cache_invalidation_on_set(self, dotted_path, extra_cache_attrs):
        """set_tool_policy must clear tool caches so the next build picks up the new policy."""
        import importlib

        from pocketpaw.tools.policy import ToolPolicy

        module_path, cls_name = dotted_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)

        instance = cls.__new__(cls)
        for attr in extra_cache_attrs:
            setattr(instance, attr, ["cached_tool"])

        instance.set_tool_policy(ToolPolicy(profile="full", deny=["group:shell"]))

        for attr in extra_cache_attrs:
            assert getattr(instance, attr) is None, (
                f"{cls_name}.set_tool_policy() did not clear {attr}"
            )
