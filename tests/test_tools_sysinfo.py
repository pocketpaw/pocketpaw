"""Tests for SystemInfoTool."""

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.tools.builtin.sysinfo import SystemInfoTool


@pytest.fixture
def sysinfo_tool():
    return SystemInfoTool()


class TestBasicInfo:
    @pytest.mark.asyncio
    async def test_returns_string(self, sysinfo_tool):
        result = await sysinfo_tool.execute()
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_contains_system_info(self, sysinfo_tool):
        result = await sysinfo_tool.execute()
        # Should contain at least platform info regardless of psutil
        assert "System Status" in result


class TestWithPsutil:
    @pytest.mark.asyncio
    async def test_includes_network(self, sysinfo_tool):
        try:
            import psutil  # noqa: F401
        except ImportError:
            pytest.skip("psutil not installed")

        result = await sysinfo_tool.execute()
        assert "Network" in result

    @pytest.mark.asyncio
    async def test_include_processes(self, sysinfo_tool):
        try:
            import psutil  # noqa: F401
        except ImportError:
            pytest.skip("psutil not installed")

        result = await sysinfo_tool.execute(include_processes=True)
        # May or may not have processes with >0% CPU, but shouldn't error
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_processes_not_shown_by_default(self, sysinfo_tool):
        result = await sysinfo_tool.execute()
        assert "Top processes" not in result


class TestWithoutPsutil:
    @pytest.mark.asyncio
    async def test_fallback_without_psutil(self, sysinfo_tool):
        with patch(
            "pocketpaw.tools.builtin.sysinfo.get_system_status",
            return_value="🟡 **System Status (limited)**\n\n💻 **Darwin (arm64)**\n\n"
            "Install psutil for full stats: pip install 'pocketpaw[desktop]'",
        ):
            # Also mock the psutil import inside execute to raise ImportError
            import builtins

            real_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == "psutil":
                    raise ImportError("mocked")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                result = await sysinfo_tool.execute()

        assert "limited" in result
        assert "Network" not in result


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_handles_status_error(self, sysinfo_tool):
        with patch(
            "pocketpaw.tools.builtin.sysinfo.get_system_status",
            side_effect=RuntimeError("boom"),
        ):
            result = await sysinfo_tool.execute()

        assert "Error" in result
        assert "boom" in result


class TestToolDefinition:
    def test_name(self, sysinfo_tool):
        assert sysinfo_tool.name == "system_info"

    def test_definition_schema(self, sysinfo_tool):
        defn = sysinfo_tool.definition
        assert defn.name == "system_info"
        assert "include_processes" in defn.parameters["properties"]
        assert defn.trust_level == "standard"
