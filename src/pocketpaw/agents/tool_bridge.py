"""Tool bridge -- adapts PocketPaw tools for use by different agent backends.

Provides:
- _instantiate_all_tools(backend): discover and instantiate builtin tools, filtered by backend
- build_openai_function_tools(): wrap tools as OpenAI Agents SDK FunctionTool objects
- build_adk_function_tools(): wrap tools as Google ADK FunctionTool objects
- get_tool_instructions_compact(): compact markdown for system-prompt injection

Backend-aware exclusion:
- claude_agent_sdk: shell/fs tools excluded (provided natively by CLI)
- All other backends: shell/fs tools included via the bridge
- BrowserTool/DesktopTool: always excluded (need special session state)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.tools.policy import ToolPolicy
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Tools excluded from ALL backends -- need special session state or desktop access.
_ALWAYS_EXCLUDED = frozenset({"BrowserTool", "DesktopTool"})

# Tools excluded only for claude_agent_sdk -- these are provided natively by the CLI.
_CLAUDE_SDK_EXCLUDED = frozenset({"ShellTool", "ReadFileTool", "WriteFileTool", "ListDirTool"})


def _instantiate_all_tools(backend: str = "claude_agent_sdk") -> list[BaseTool]:
    """Discover and instantiate all builtin tools, filtered by backend.

    Args:
        backend: The agent backend name. For ``claude_agent_sdk``, shell/fs
                 tools are excluded (they're SDK builtins). Other backends
                 get the full set minus browser/desktop.

    Returns a list of BaseTool instances.  Import errors per-tool are caught
    and logged so one broken tool doesn't block the rest.
    """
    from pocketpaw.tools.builtin import _LAZY_IMPORTS

    excluded = _ALWAYS_EXCLUDED
    if backend == "claude_agent_sdk":
        excluded = excluded | _CLAUDE_SDK_EXCLUDED

    tools: list[BaseTool] = []
    for class_name, (module_path, attr_name) in _LAZY_IMPORTS.items():
        if class_name in excluded:
            continue
        try:
            import importlib

            mod = importlib.import_module(module_path, "pocketpaw.tools.builtin")
            cls = getattr(mod, attr_name)
            tools.append(cls())
        except Exception as exc:
            logger.debug("Skipping tool %s: %s", class_name, exc)
    return tools


def build_openai_function_tools(settings: Any, backend: str = "openai_agents") -> list:
    """Build a list of OpenAI Agents SDK ``FunctionTool`` wrappers for PocketPaw tools.

    Each tool is wrapped in a FunctionTool whose ``on_invoke_tool`` callback
    parses the JSON args string and calls ``tool.execute(**params)``.

    Only tools permitted by the active ToolPolicy are included.

    Args:
        settings: A ``Settings`` instance used to build the ToolPolicy.

    Returns:
        List of ``agents.FunctionTool`` objects (empty if SDK not installed).
    """
    try:
        from agents import FunctionTool
    except ImportError:
        logger.debug("OpenAI Agents SDK not installed — returning empty tools list")
        return []

    policy = ToolPolicy(
        profile=settings.tool_profile,
        allow=settings.tools_allow,
        deny=settings.tools_deny,
    )

    registry = ToolRegistry(policy=policy)
    for tool in _instantiate_all_tools(backend=backend):
        registry.register(tool)

    function_tools: list[FunctionTool] = []
    for tool_name in registry.allowed_tool_names:
        tool = registry.get(tool_name)
        if tool is None:
            continue

        defn = tool.definition

        # Sanitize JSON schema: strict providers (e.g. Groq) reject schemas
        # where 'required' is present but 'properties' is empty or missing.
        params_schema = dict(defn.parameters) if defn.parameters else {"type": "object"}
        props = params_schema.get("properties")
        if not props and "required" in params_schema:
            params_schema.pop("required")
        if not props and "properties" in params_schema:
            params_schema.pop("properties")

        ft = FunctionTool(
            name=defn.name,
            description=defn.description,
            params_json_schema=params_schema,
            on_invoke_tool=_make_invoke_callback(tool),
        )
        function_tools.append(ft)

    logger.info("Built %d OpenAI FunctionTools from PocketPaw tools", len(function_tools))
    return function_tools


def _make_invoke_callback(tool: Any):
    """Create an async callback for a single tool (avoids closure-capture bugs)."""

    async def callback(ctx: Any, args: str) -> str:
        try:
            params = json.loads(args) if args else {}
        except (json.JSONDecodeError, TypeError):
            return f"Error: invalid JSON arguments for {tool.name}: {args!r}"

        if not isinstance(params, dict):
            return f"Error: arguments must be a JSON object, got {type(params).__name__}"

        try:
            return await tool.execute(**params)
        except Exception as exc:
            logger.error("Tool %s execution error: %s", tool.name, exc)
            return f"Error executing {tool.name}: {exc}"

    return callback


def build_adk_function_tools(settings: Any, backend: str = "google_adk") -> list:
    """Build a list of Google ADK ``FunctionTool`` wrappers for PocketPaw tools.

    ADK accepts plain Python callables as tools via ``FunctionTool(func=...)``.
    Each PocketPaw tool becomes an async function with a docstring derived from
    ``tool.definition.description``.

    Only tools permitted by the active ToolPolicy are included.

    Args:
        settings: A ``Settings`` instance used to build the ToolPolicy.

    Returns:
        List of ``google.adk.tools.FunctionTool`` objects (empty if SDK not installed).
    """
    try:
        from google.adk.tools import FunctionTool
    except ImportError:
        logger.debug("Google ADK not installed — returning empty tools list")
        return []

    policy = ToolPolicy(
        profile=settings.tool_profile,
        allow=settings.tools_allow,
        deny=settings.tools_deny,
    )

    registry = ToolRegistry(policy=policy)
    for tool in _instantiate_all_tools(backend=backend):
        registry.register(tool)

    function_tools: list = []
    for tool_name in registry.allowed_tool_names:
        tool = registry.get(tool_name)
        if tool is None:
            continue

        wrapper = _make_adk_wrapper(tool)
        ft = FunctionTool(func=wrapper)
        function_tools.append(ft)

    logger.info("Built %d ADK FunctionTools from PocketPaw tools", len(function_tools))
    return function_tools


def _make_adk_wrapper(tool: Any):
    """Create an async wrapper function for a PocketPaw tool for use by ADK.

    ADK introspects the function name, docstring, and type annotations to build
    the tool schema, so we dynamically construct a wrapper with the correct metadata.
    """
    import inspect

    defn = tool.definition
    params = defn.parameters or {}
    props = params.get("properties", {})

    # Build parameter list for the wrapper
    param_names = list(props.keys())

    async def _adk_tool_wrapper(**kwargs: str) -> str:
        try:
            return await tool.execute(**kwargs)
        except Exception as exc:
            logger.error("ADK tool %s execution error: %s", tool.name, exc)
            return f"Error executing {tool.name}: {exc}"

    # Set function metadata so ADK can introspect it
    _adk_tool_wrapper.__name__ = defn.name
    _adk_tool_wrapper.__qualname__ = defn.name
    _adk_tool_wrapper.__doc__ = defn.description

    # Build proper signature with string-typed parameters
    sig_params = [
        inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str)
        for name in param_names
    ]
    _adk_tool_wrapper.__signature__ = inspect.Signature(
        parameters=sig_params,
        return_annotation=str,
    )
    # Type annotations dict for ADK's schema builder
    _adk_tool_wrapper.__annotations__ = {name: str for name in param_names}
    _adk_tool_wrapper.__annotations__["return"] = str

    return _adk_tool_wrapper


def get_tool_instructions_compact(settings: Any, backend: str = "opencode") -> str:
    """Build a compact tool-instruction block for system prompt injection.

    Returns a markdown section listing available tool names that the agent
    can invoke via ``python -m pocketpaw.tools.cli <name> '<json>'``.

    Only tools permitted by the active ToolPolicy are listed.

    Args:
        settings: A ``Settings`` instance used to build the ToolPolicy.

    Returns:
        Markdown string, or empty string if no tools are available.
    """
    policy = ToolPolicy(
        profile=settings.tool_profile,
        allow=settings.tools_allow,
        deny=settings.tools_deny,
    )

    registry = ToolRegistry(policy=policy)
    for tool in _instantiate_all_tools(backend=backend):
        registry.register(tool)

    allowed = registry.allowed_tool_names
    if not allowed:
        return ""

    lines = [
        "# PocketPaw Tools",
        "",
        "You have access to the following PocketPaw tools.",
        "To use a tool, run: `python -m pocketpaw.tools.cli <tool_name> '<json_args>'`",
        "",
    ]
    for tool_name in sorted(allowed):
        tool = registry.get(tool_name)
        if tool:
            desc = tool.definition.description.split(".")[0]
            lines.append(f"- `{tool_name}` — {desc}")

    lines.append("")
    lines.append(f"Total: {len(allowed)} tools available.")
    return "\n".join(lines)
