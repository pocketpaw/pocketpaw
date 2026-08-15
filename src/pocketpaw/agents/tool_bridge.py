"""Tool bridge -- adapts PocketPaw tools for use by different agent backends.

Provides:
- _instantiate_all_tools(backend): discover and instantiate builtin tools, filtered by backend
- build_openai_function_tools(): wrap tools as OpenAI Agents SDK FunctionTool objects
- build_adk_function_tools(): wrap tools as Google ADK FunctionTool objects
- build_deep_agents_tools(): wrap tools as LangChain StructuredTool objects for Deep Agents
- build_pydantic_ai_tools(): wrap tools as pydantic-ai Tool objects
- get_tool_instructions_compact(): compact markdown for system-prompt injection

Backend-aware exclusion:
- claude_agent_sdk: shell/fs/edit tools excluded (provided natively by CLI)
- All other backends: shell/fs/edit tools included via the bridge
- BrowserTool/DesktopTool: always excluded (need special session state)

Changes:
- 2026-08-15 (HTN-2, feat/narration-registry-lookup): the ToolRegistry each
  build_*_tools() constructs is no longer dropped at function exit. It is kept
  on the ToolPolicy it was built under and reachable through
  narration_registry_for(backend), so a caller holding an agent can resolve
  that agent's OWN live tool instances — which is what humanized tool narration
  reads a tool's declared phrase off. Anchored on the policy because its
  lifetime is already the agent's; a module-level map would leak, since the
  registry references its policy and would keep its own weak key alive. The
  four builders now share _build_tool_registry() instead of repeating the same
  three lines.
- 2026-07-29 (feat/pydantic-ai-backend): added build_pydantic_ai_tools() plus the
  two signature builders it shares. pydantic-ai derives a tool's JSON schema from
  the wrapper's *signature*, so the wrappers synthesize one instead of emitting
  schema by hand. Two differences from the LangChain bridge, both deliberate:
  the JSON-schema path honours ``required`` (optional params become
  ``str | None = None`` rather than all-required), and every wrapper is asserted
  async — a sync tool would run on anyio's bounded thread pool and throttle every
  concurrent run in the in-process backend.
- 2026-03-12: Added EditFileTool to _CLAUDE_SDK_EXCLUDED (has native Edit)
- 2026-05-21 (#1160): _scan_tool_output now also caps oversized results via
  cap_tool_output(), so tool blobs returned through the OpenAI / ADK /
  LangChain wrappers can't flood agent context.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.tools.policy import ToolPolicy
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _build_tool_registry(backend: str, policy: ToolPolicy) -> ToolRegistry:
    """Build the ToolRegistry for one backend's bridged surface, and retain it.

    The four ``build_*_tools`` functions below all did these three lines
    themselves and dropped the registry on the floor at function exit. It is
    now kept on the POLICY, so a caller that has the agent can reach that
    agent's live tool instances. Humanized tool narration (HTN-2) is the
    consumer: it reads the ``Narration`` a tool declares off the instance the
    registry already holds, because CONSTRUCTING a tool to read a property
    would run ``ShellTool.__init__`` -> ``get_settings()`` on the event loop
    just to phrase a status line.

    The policy is the anchor for two reasons. Its LIFETIME is already the
    agent's — the pool builds one policy per agent, the backend holds it, and
    an evicted ``AgentInstance`` takes both with it — and ``set_tool_policy``
    swaps the policy while clearing ``_custom_tools``, so a rebuilt surface
    lands on the new policy instead of serving a stale registry.

    A module-level map would be worse in two distinct ways. It would LEAK: the
    registry references its policy, so even a weak-keyed entry would keep its
    own key alive forever, holding every agent's tools for the life of the
    process. And keyed by tool NAME it would also be ambiguous — EE registers
    ``DaytonaShellTool`` under the same ``shell`` name as the OSS builtin, so
    two live agents would disagree and the lookup would have to pick one
    nondeterministically or refuse to narrate ``shell`` at all.
    """
    registry = ToolRegistry(policy=policy)
    for tool in _instantiate_all_tools(backend=backend):
        registry.register(tool)
    policy._bridged_registry = registry
    return registry


def narration_registry_for(backend: Any) -> ToolRegistry | None:
    """Return the ToolRegistry backing *backend*'s bridged tool surface.

    The seam that lets a caller holding an agent (the cloud agent bridge)
    resolve that agent's OWN live tools. Returns ``None`` for a backend that
    never bridged tools through here — the Claude SDK backend surfaces its
    tools over MCP, so there is no registry to find and narration derives from
    the tool name instead.

    Duck-typed on the public ``get_tool_policy`` every bridged backend exposes,
    so no caller has to reach for a private attribute of a backend.
    """
    getter = getattr(backend, "get_tool_policy", None)
    if not callable(getter):
        return None
    try:
        policy = getter()
        if policy is None:
            return None
        registry = getattr(policy, "_bridged_registry", None)
    except Exception:  # noqa: BLE001 — a status line must never break a run
        logger.debug("Could not resolve a narration registry for %r", type(backend), exc_info=True)
        return None
    return registry if isinstance(registry, ToolRegistry) else None


# Tools excluded from ALL backends -- need special session state or desktop access.
_ALWAYS_EXCLUDED = frozenset({"BrowserTool", "DesktopTool"})

# Tools excluded only for claude_agent_sdk -- these are provided natively by the CLI.
_CLAUDE_SDK_EXCLUDED = frozenset(
    {
        "ShellTool",
        "ReadFileTool",
        "WriteFileTool",
        "ListDirTool",
        "EditFileTool",
    }
)

# Tool names (NOT class names) that overlap with Composio's hosted
# integrations. When Composio is enabled (cloud, with ``composio_api_key``
# set), these YAML-/native-connector-backed tools are dropped from the
# agent's surface so the LLM has exactly one path per integration. Without
# this, the agent gets confused between Composio's ``GMAIL_SEND_EMAIL`` and
# the legacy ``gmail_send``, and tends to fall back on the legacy tool's
# "Settings → Google OAuth" auth flow (a paw-enterprise UI affordance, not
# a chat one).
_COMPOSIO_OVERLAPPING_TOOL_NAMES = frozenset(
    {
        # Gmail
        "gmail_search",
        "gmail_read",
        "gmail_send",
        "gmail_list_labels",
        "gmail_create_label",
        "gmail_modify",
        "gmail_trash",
        "gmail_batch_modify",
        # Google Calendar
        "calendar_list",
        "calendar_create",
        "calendar_prep",
        # Google Docs
        "docs_read",
        "docs_create",
        "docs_search",
        # Google Drive
        "drive_list",
        "drive_download",
        "drive_upload",
        "drive_share",
        # Reddit
        "reddit_search",
        "reddit_read",
        "reddit_trending",
        # Spotify
        "spotify_search",
        "spotify_now_playing",
        "spotify_playback",
        "spotify_playlist",
    }
)


def _is_composio_enabled() -> bool:
    """True when Composio is configured. Read lazily so OSS-local runs
    don't pay the ``Settings.load`` cost up front."""
    try:
        from pocketpaw.config import Settings

        s = Settings.load()
        return bool(s.composio_api_key and s.composio_enterprise_id)
    except Exception:  # noqa: BLE001
        return False


def composio_tools_for(backend: str, settings: Any) -> list[Any]:
    """Composio integration tools for *backend*, via the
    ``pocketpaw.composio_tools`` entry point.

    Returns ``[]`` on an OSS install (no provider registered), when
    Composio is not configured, or when the per-stream fetch fails —
    Composio is always additive to the agent's tool surface, never
    load-bearing, so a failure degrades silently rather than aborting
    the run. Keeps the OSS core free of any ``pocketpaw_ee`` import: the
    cloud provider is discovered through the entry-point registry.
    """
    from pocketpaw._registry import first as _ext_first

    provider = _ext_first("pocketpaw.composio_tools")
    if provider is None:
        return []
    try:
        return list(provider.build_tools(backend, settings))
    except ImportError:
        return []  # composio SDK / provider package not installed
    except Exception as exc:  # noqa: BLE001
        logger.debug("composio_tools_for(%s) failed: %s", backend, exc)
        return []


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

    # Inject soul tools if soul is active — and exclude regular memory tools
    # to avoid overlap (soul_remember/soul_recall supersede remember/recall/forget).
    try:
        from pocketpaw.soul import get_soul_manager

        soul_mgr = get_soul_manager()
        if soul_mgr is not None:
            tools = [t for t in tools if t.name not in ("remember", "recall", "forget")]
            tools.extend(soul_mgr.get_tools())
    except Exception:
        pass  # Soul not available

    # EE agent extensions contribute backend-specific function tools — the
    # cloud pocket specialist for MCP-capable function-tool backends. The
    # extension owns the backend gating (which backends get the tool); an
    # OSS install registers no extension and this loop is a no-op.
    from pocketpaw._registry import providers as _ext_providers

    for ext in _ext_providers("pocketpaw.agent_extensions"):
        try:
            tools.extend(ext.agent_tools(backend))
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent extension %r agent_tools failed: %s", ext, exc)

    # When Composio is configured, drop the YAML-/native-connector tools
    # whose integrations are now served by Composio's hosted ``*_*`` tools
    # (GMAIL_SEND_EMAIL, etc.). Prevents the LLM from mixing two
    # integration paths and surfacing paw-enterprise's
    # "Settings → Google OAuth" affordance in chat.
    if _is_composio_enabled():
        before = len(tools)
        tools = [t for t in tools if t.name not in _COMPOSIO_OVERLAPPING_TOOL_NAMES]
        dropped = before - len(tools)
        if dropped:
            logger.info(
                "tool_bridge: dropped %d YAML-connector tools (Composio is enabled)",
                dropped,
            )

    return tools


def build_openai_function_tools(
    settings: Any, backend: str = "openai_agents", policy: ToolPolicy | None = None
) -> list:
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

    if policy is None:
        policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )

    registry = _build_tool_registry(backend, policy)

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
        if "properties" not in params_schema:
            params_schema["properties"] = {}

        ft = FunctionTool(
            name=defn.name,
            description=defn.description,
            params_json_schema=params_schema,
            on_invoke_tool=_make_invoke_callback(tool),
        )
        function_tools.append(ft)

    logger.info("Built %d OpenAI FunctionTools from PocketPaw tools", len(function_tools))
    return function_tools


def _scan_tool_output(result: str, tool_name: str) -> str:
    """Post-process a tool result before it reaches agent context.

    Two steps, both best-effort (an error in either leaves the result
    unchanged rather than breaking tool execution):

    1. Injection scan — sanitise content that trips the prompt-injection
       scanner (e.g. hostile web pages).
    2. Output budget — cap an oversized blob (a long test run, a build log,
       a big HTTP body) via ``cap_tool_output`` so it can't flood the
       context window. Normal-sized output passes through untouched.

    This runs inside the OpenAI / ADK / LangChain tool wrappers, which call
    ``tool.execute`` directly rather than through ``ToolRegistry.execute``.
    """
    try:
        from pocketpaw.config import get_settings
        from pocketpaw.security.injection_scanner import get_injection_scanner

        settings = get_settings()
        if settings.injection_scan_enabled and result:
            scanner = get_injection_scanner()
            scan = scanner.scan(result, source=f"tool:{tool_name}")
            if scan.threat_level.value != "none":
                result = scan.sanitized_content
    except Exception:
        pass  # Don't let scanner errors break tool execution

    # Output budget — cap a noisy blob. Idempotent, so a result already
    # capped inside BaseTool._success passes through unchanged.
    if result:
        try:
            from pocketpaw.config import get_settings
            from pocketpaw.tools.output_budget import cap_tool_output

            cap = getattr(get_settings(), "tool_output_char_cap", None)
            result = cap_tool_output(result, cap=cap, tool_name=tool_name)
        except Exception:
            logger.debug("Tool output cap failed for %s", tool_name, exc_info=True)

    return result


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
            result = await tool.execute(**params)
            return _scan_tool_output(result, tool.name)
        except Exception as exc:
            logger.error("Tool %s execution error: %s", tool.name, exc)
            return f"Error executing {tool.name}: {exc}"

    return callback


def build_adk_function_tools(
    settings: Any, backend: str = "google_adk", policy: ToolPolicy | None = None
) -> list:
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

    if policy is None:
        policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )

    registry = _build_tool_registry(backend, policy)

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
            result = await tool.execute(**kwargs)
            return _scan_tool_output(result, tool.name)
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


def build_deep_agents_tools(
    settings: Any, backend: str = "deep_agents", policy: ToolPolicy | None = None
) -> list:
    """Build a list of LangChain ``StructuredTool`` wrappers for PocketPaw tools.

    Deep Agents accepts LangChain tools, plain callables, or dicts. We use
    StructuredTool for the richest schema support.

    Only tools permitted by the active ToolPolicy are included.

    Args:
        settings: A ``Settings`` instance used to build the ToolPolicy.

    Returns:
        List of ``langchain_core.tools.StructuredTool`` objects (empty if not installed).
    """
    try:
        from langchain_core.tools import StructuredTool  # noqa: F401
    except ImportError:
        logger.debug("langchain-core not installed — returning empty tools list")
        return []

    if policy is None:
        policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )

    registry = _build_tool_registry(backend, policy)

    structured_tools: list = []
    for tool_name in registry.allowed_tool_names:
        tool = registry.get(tool_name)
        if tool is None:
            continue

        wrapper = _make_langchain_wrapper(tool)
        structured_tools.append(wrapper)

    logger.info("Built %d LangChain StructuredTools from PocketPaw tools", len(structured_tools))
    return structured_tools


def build_pydantic_ai_tools(
    settings: Any, backend: str = "pydantic_ai", policy: ToolPolicy | None = None
) -> list:
    """Build a list of pydantic-ai ``Tool`` objects for PocketPaw tools.

    The pydantic-ai analogue of :func:`build_deep_agents_tools`. Only tools
    permitted by the active ``ToolPolicy`` are included, so the RFC-14 surface
    policy — ``(agent ∪ surface ∪ entity).allow − deny`` — is enforced by
    ABSENCE from the model's tool list rather than by a refusal at call time.

    Every returned tool is a coroutine, and that is asserted rather than
    assumed. The Pydantic AI backend runs in-process and shares one event loop
    with the API tier: a single blocking tool function would execute on anyio's
    bounded worker-thread pool and throttle every concurrent run in the process,
    which is precisely the ceiling this backend exists to raise.

    Args:
        settings: A ``Settings`` instance used to build the ToolPolicy.
        backend: Backend name passed to the tool instantiator.
        policy: Explicit policy; built from *settings* when omitted.

    Returns:
        List of ``pydantic_ai.tools.Tool`` (empty if pydantic-ai isn't installed).
    """
    try:
        from pydantic_ai.tools import Tool
    except ImportError:
        logger.debug("pydantic-ai not installed — returning empty tools list")
        return []

    if policy is None:
        policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )

    registry = _build_tool_registry(backend, policy)

    tools: list = []
    for tool_name in registry.allowed_tool_names:
        tool = registry.get(tool_name)
        if tool is None:
            continue
        try:
            tools.append(_make_pydantic_ai_tool(Tool, tool, settings))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping tool %s — could not build wrapper: %s", tool_name, exc)

    logger.info("Built %d Pydantic AI Tools from PocketPaw tools", len(tools))
    return tools


def _make_pydantic_ai_tool(tool_cls: Any, tool: Any, settings: Any) -> Any:
    """Wrap one PocketPaw tool as a pydantic-ai ``Tool``.

    pydantic-ai derives a tool's JSON schema from the wrapper's *signature*, so
    both paths below build a synthetic signature rather than hand-writing schema:

    * ``args_schema`` present — mirror the Pydantic model's fields with their
      real annotations and defaults, so nested objects (the pocket specialist's
      ``hints``) round-trip as ``$defs`` instead of being flattened to strings.
    * otherwise — one ``str`` parameter per declared property, optional ones
      annotated ``str | None`` with a ``None`` default so the model isn't forced
      to invent a value for every field. (The LangChain bridge marks all
      parameters required; respecting ``required`` here is deliberate.)
    """
    import inspect

    defn = tool.definition
    limit = int(getattr(settings, "pydantic_ai_max_tool_output_chars", 0) or 0)

    async def _run(**kwargs: Any) -> str:
        try:
            result = await tool.execute(**kwargs)
        except Exception as exc:
            logger.error("Pydantic AI tool %s execution error: %s", tool.name, exc)
            return f"Error executing {tool.name}: {exc}"
        scanned = _scan_tool_output(result, tool.name)
        if limit and isinstance(scanned, str) and len(scanned) > limit:
            # Announce the truncation with the true length. A silent "...(truncated)"
            # on a tool whose contract asks for complete content is how the /code
            # fabrication bug happened (2026-07-28): past the cap the instruction
            # pair became jointly unobeyable and the model reconstructed the tail.
            return (
                f"{scanned[:limit]}\n\n[truncated: {tool.name} returned "
                f"{len(scanned)} chars, limit {limit}. This output is INCOMPLETE — "
                f"narrow the request rather than inferring the remainder.]"
            )
        return scanned

    _run.__name__ = defn.name
    _run.__qualname__ = defn.name
    _run.__doc__ = defn.description

    schema_cls = getattr(tool, "args_schema", None)
    if schema_cls is not None:
        params, annotations = _signature_from_model(inspect, schema_cls)
    else:
        params, annotations = _signature_from_json_schema(inspect, defn.parameters or {})

    annotations["return"] = str
    _run.__signature__ = inspect.Signature(parameters=params, return_annotation=str)
    _run.__annotations__ = annotations

    if not inspect.iscoroutinefunction(_run):  # pragma: no cover - defensive
        raise TypeError(
            f"Bridged tool {defn.name} is not async. Sync tools run on anyio's "
            "bounded thread pool and throttle every concurrent run in the process."
        )

    return tool_cls(_run, name=defn.name, description=defn.description)


def _signature_from_model(inspect: Any, model: Any) -> tuple[list, dict]:
    """Build signature params from a Pydantic model's fields, preserving types."""
    params: list = []
    annotations: dict = {}
    for name, field in model.model_fields.items():
        if field.is_required():
            default = inspect.Parameter.empty
        else:
            default = field.get_default(call_default_factory=True)
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=field.annotation,
                default=default,
            )
        )
        annotations[name] = field.annotation
    return params, annotations


def _signature_from_json_schema(inspect: Any, parameters: dict) -> tuple[list, dict]:
    """Build ``str``-typed signature params from a tool's JSON-schema properties.

    Honours the schema's ``required`` list: optional properties become
    ``str | None = None`` so the model may omit them.
    """
    props = parameters.get("properties", {}) or {}
    required = set(parameters.get("required", []) or [])
    params: list = []
    annotations: dict = {}
    for name in props:
        if name in required:
            params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str))
            annotations[name] = str
        else:
            params.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=str | None,
                    default=None,
                )
            )
            annotations[name] = str | None
    return params, annotations


def _make_langchain_wrapper(tool: Any):
    """Create a LangChain StructuredTool wrapper for a PocketPaw tool.

    When the tool exposes ``args_schema`` (a Pydantic model), pass it through
    to ``StructuredTool.from_function`` so nested object params (e.g. the
    pocket specialist's ``hints``) round-trip with their real types instead
    of getting flattened to str-typed signature params.
    """
    import inspect

    from langchain_core.tools import StructuredTool

    defn = tool.definition

    async def _run(**kwargs: Any) -> str:
        try:
            result = await tool.execute(**kwargs)
            return _scan_tool_output(result, tool.name)
        except Exception as exc:
            logger.error("LangChain tool %s execution error: %s", tool.name, exc)
            return f"Error executing {tool.name}: {exc}"

    _run.__name__ = defn.name
    _run.__qualname__ = defn.name
    _run.__doc__ = defn.description

    schema_cls = getattr(tool, "args_schema", None)
    if schema_cls is not None:
        # Rich-schema path: let LangChain derive the signature from the
        # Pydantic model. Signature/annotations stay generic on _run.
        return StructuredTool.from_function(
            coroutine=_run,
            name=defn.name,
            description=defn.description,
            args_schema=schema_cls,
        )

    # Default str-only path (covers the 50+ existing builtin tools).
    props = (defn.parameters or {}).get("properties", {})
    param_names = list(props.keys())
    sig_params = [
        inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str)
        for name in param_names
    ]
    _run.__signature__ = inspect.Signature(
        parameters=sig_params,
        return_annotation=str,
    )
    _run.__annotations__ = {name: str for name in param_names}
    _run.__annotations__["return"] = str

    return StructuredTool.from_function(
        coroutine=_run,
        name=defn.name,
        description=defn.description,
    )


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
        "To use a tool, pipe JSON via stdin (avoids bash $-expansion issues):",
        "```",
        "echo '<json_args>' | python -m pocketpaw.tools.cli <tool_name>",
        "```",
        "IMPORTANT: Always use single quotes around JSON to prevent bash from",
        "expanding $ signs in values like prices ($74.30) or currency amounts.",
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


# ---------------------------------------------------------------------------
# in-process MCP servers -> pydantic-ai toolsets
# ---------------------------------------------------------------------------


async def build_inprocess_mcp_toolsets(
    policy: ToolPolicy | None = None, settings: Any = None
) -> list:
    """Bridge PocketPaw's IN-PROCESS MCP servers into pydantic-ai toolsets.

    Added 2026-07-31. These are the sites / pocket / connectors / media / …
    servers registered through the ``pocketpaw.mcp_servers`` entry points, and
    until now they reached the Claude SDK backend and NOTHING else — every
    provider's ``build_server`` is written against ``claude_agent_sdk``'s
    ``create_sdk_mcp_server`` and returns ``None`` without it.

    The consequence was not a missing convenience. Asked to build a site on the
    ``pydantic_ai`` backend the agent had no ``create_svelte_site`` to call, so
    it did the next best thing it could see — and "the model wrote a file
    instead of calling the tool" is what a missing tool looks like from outside.

    What makes the bridge cheap is that ``create_sdk_mcp_server`` returns a
    plain low-level ``mcp.server.Server``. Its ``request_handlers`` can be
    driven directly, in this process, with no transport, no subprocess and no
    second copy of the handler logic — the tools that run here are the same
    objects the SDK backend calls.

    Names come out as ``<server>_<tool>``, matching what pydantic-ai's
    ``PrefixedToolset`` produces for external servers, so a surface's
    ``mcp__<server>__<tool>`` deny/allow ids translate onto them with the
    normalization that already exists.
    """
    try:
        import mcp.types as mcp_types
        from pydantic_ai.toolsets import FunctionToolset, PrefixedToolset
    except ImportError:
        logger.debug("mcp / pydantic-ai toolsets unavailable; in-process MCP bridge skipped")
        return []

    try:
        from pocketpaw._registry import providers as _ext_providers
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS
    except ImportError:
        return []

    toolsets: list = []
    for provider in _ext_providers("pocketpaw.mcp_servers"):
        provider_name = type(provider).__name__
        try:
            built = provider.build_server()
        except Exception as exc:  # noqa: BLE001
            # WARNING, not DEBUG: a stale editable install silently swallowing
            # this cost 30+ minutes to diagnose on the SDK path (claude_sdk:1143).
            logger.warning(
                "MCP provider %s failed to build: %s: %s", provider_name, type(exc).__name__, exc
            )
            continue
        if not built:
            continue

        name, server = built
        instance = server.get("instance") if isinstance(server, dict) else None
        if instance is None:
            logger.debug("MCP provider %s returned no server instance", provider_name)
            continue
        if policy is not None and not policy.is_mcp_server_allowed(name):
            logger.info("In-process MCP server '%s' blocked by tool policy", name)
            continue
        # Opt-in servers stay off unless the agent named them, mirroring the
        # SDK backend's allowlist rule rather than inventing a second one.
        if name in OPT_IN_MCP_SERVERS and not (
            policy is not None and policy.is_mcp_server_explicitly_allowed(name)
        ):
            continue

        try:
            tools = await _bridge_inprocess_server(name, instance, mcp_types, settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not bridge in-process MCP server '%s': %s", name, exc)
            continue
        if tools:
            toolsets.append(PrefixedToolset(FunctionToolset(tools), name))
            logger.info("Bridged in-process MCP server '%s' (%d tools)", name, len(tools))

    return toolsets


async def _bridge_inprocess_server(
    name: str, instance: Any, mcp_types: Any, settings: Any = None
) -> list:
    """Enumerate one in-process MCP server's tools as pydantic-ai ``Tool``s."""
    from pydantic_ai.tools import Tool

    list_handler = instance.request_handlers.get(mcp_types.ListToolsRequest)
    call_handler = instance.request_handlers.get(mcp_types.CallToolRequest)
    if list_handler is None or call_handler is None:
        return []

    listed = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    tools: list = []
    for spec in getattr(listed.root, "tools", None) or []:
        tools.append(
            Tool.from_schema(
                _make_inprocess_caller(spec.name, call_handler, mcp_types, settings),
                name=spec.name,
                description=spec.description or spec.name,
                # The server's OWN schema, passed through untouched. Synthesizing
                # a signature would flatten every object and array argument to a
                # string — ``edit_svelte_component`` takes a list of edits and
                # ``create_dynamic_site`` a whole spec object.
                json_schema=spec.inputSchema or {"type": "object", "properties": {}},
            )
        )
    return tools


def _make_inprocess_caller(tool_name: str, call_handler: Any, mcp_types: Any, settings: Any = None):
    """Build the coroutine that invokes one in-process MCP tool.

    A factory rather than a closure written inline in the loop: the latter
    captures the loop variable, so every tool ends up calling the last one.

    The result goes through the SAME post-processing as a bridged function tool
    (``_scan_tool_output`` plus the per-tool character cap). It did not until
    2026-08-01, which meant 97 of the backend's 134 tools skipped the
    prompt-injection scan and the output budget — including
    ``connector_execute`` and ``sense_execute``, which return data from
    EXTERNAL systems and are exactly the hostile-content case the scanner
    exists for.
    """
    limit = int(getattr(settings, "pydantic_ai_max_tool_output_chars", 0) or 0)

    async def _call(**kwargs: Any) -> str:
        # Drop unset optionals — MCP handlers check presence, and an explicit
        # ``None`` is not the same as an omitted argument to them.
        arguments = {k: v for k, v in kwargs.items() if v is not None}
        result = await call_handler(
            mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name=tool_name, arguments=arguments),
            )
        )
        root = result.root
        text = "\n".join(
            getattr(block, "text", "") or "" for block in (getattr(root, "content", None) or [])
        ).strip()
        if getattr(root, "isError", False):
            # Returned, not raised: the model can read the reason and correct
            # its arguments. Raising would burn a retry on an error it never saw.
            return text or f"Error: {tool_name} failed."

        scanned = _scan_tool_output(text, tool_name)
        if limit and len(scanned) > limit:
            # Announced, never silent — see the same cap in
            # ``_make_pydantic_ai_tool`` for why a quiet truncation on a tool
            # asked for complete content is how the /code fabrication bug began.
            return (
                f"{scanned[:limit]}\n\n[truncated: {tool_name} returned "
                f"{len(scanned)} chars, limit {limit}. This output is INCOMPLETE — "
                f"narrow the request rather than inferring the remainder.]"
            )
        return scanned

    _call.__name__ = tool_name
    return _call
