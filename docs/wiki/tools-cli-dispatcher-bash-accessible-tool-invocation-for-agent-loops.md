---
{
  "title": "Tools CLI Dispatcher: Bash-Accessible Tool Invocation for Agent Loops",
  "summary": "The `pocketpaw.tools.cli` module is a CLI dispatcher that lets any builtin tool be invoked from a Bash shell via `python -m pocketpaw.tools.cli \u003ctool_name\u003e '\u003cjson_args\u003e'`. It serves as the bridge between shell-based agent loops (like Claude Code) and PocketPaw's async Python tool ecosystem.",
  "concepts": [
    "tools_cli",
    "asyncio_run",
    "tool_registry",
    "json_args",
    "dispatcher_pattern",
    "agent_loop_integration",
    "builtin_tools",
    "shell_invocation",
    "main_function",
    "module_dispatch"
  ],
  "categories": [
    "tools",
    "cli",
    "agent-integration",
    "infrastructure"
  ],
  "source_docs": [
    "32f27494be6164f7"
  ],
  "backlinks": null,
  "word_count": 506,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`cli.py` exposes PocketPaw's entire tool registry as a command-line interface. Its primary consumer is agent systems that invoke tools through shell commands rather than direct Python API calls — most notably Claude Code and similar CLI-driven agent runtimes. By wrapping the async `execute()` methods in a synchronous `asyncio.run()` dispatcher, the CLI makes any tool available to any agent that can run shell commands.

## Usage Pattern

```
python -m pocketpaw.tools.cli <tool_name> '<json_args>'
python -m pocketpaw.tools.cli --list
```

Examples from the module docstring:
```
python -m pocketpaw.tools.cli gmail_search '{"query": "is:unread"}'
python -m pocketpaw.tools.cli text_to_speech '{"text": "Hello world"}'
python -m pocketpaw.tools.cli health_check '{"include_connectivity": true}'
```

Arguments are passed as a JSON string, which sidesteps shell quoting issues for complex nested parameters. The `--list` flag prints all registered tools with their descriptions, making the CLI self-documenting.

## Tool Registry

```python
_TOOLS = {t.name: t for t in [RememberTool(), RecallTool(), ForgetTool(), GmailSearchTool(), ...]}
```

The registry is a flat dict mapping tool names to instantiated tool objects. The full list includes 60+ tools covering Gmail, Calendar, Google Drive, Reddit, Spotify, session management, memory operations, skill generation, image generation, OCR, Discord, web search, URL extraction, translation, TTS, STT, system info, health checks, and widget management (`AddWidgetTool`, `RemoveWidgetTool` added 2026-03-27).

Shell and filesystem tools are explicitly excluded ("those are SDK built-in") — they're handled by the agent SDK layer directly, not by this dispatcher.

## main() Dispatcher

```python
def main() -> None:
    if "--list" in sys.argv:
        _print_tool_list()
        return
    tool_name = sys.argv[1]
    raw_args = sys.argv[2] if len(sys.argv) > 2 else "{}"
    args = json.loads(raw_args)
    tool = _TOOLS.get(tool_name)
    if tool is None:
        print(f"Unknown tool: {tool_name}", file=sys.stderr)
        sys.exit(1)
    result = asyncio.run(tool.execute(**args))
    print(result)
```

The dispatcher calls `asyncio.run()` to bridge the synchronous CLI boundary to the async tool ecosystem. Output goes to stdout, making it capturable by any shell construct (`$(...)`, pipes, etc.). Errors go to stderr with a non-zero exit code.

JSON args are unpacked as `**args` directly into `execute()`, which means the JSON keys must match the method's parameter names exactly. There is no translation layer — the JSON schema defined in each tool's `parameters` property is the definitive interface contract.

## Change History

The file header documents two significant additions:
- **2026-02-17**: `health_check`, `error_log`, `config_doctor` — operational tooling for diagnosing deployment issues
- **2026-03-27**: `add_widget`, `remove_widget` — dashboard widget management, indicating the CLI grew to serve the web dashboard backend as well as agent loops

This evolution shows the CLI as a general-purpose tool execution surface, not just an agent-facing interface.

## Known Gaps

- No input validation: if the JSON arg keys don't match the tool's parameter names, Python raises a `TypeError` which propagates as an unhandled exception to stderr rather than a clean error message.
- Tools that raise exceptions (rather than returning `self._error(...)`) will surface raw tracebacks.
- No streaming output: tools that produce large results (e.g., `research` with `deep` depth) buffer the entire result before printing.
- The `--list` output format is not shown — whether it includes parameter schemas or just names and descriptions is unclear.
