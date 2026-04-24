---
{
  "title": "SystemInfoTool: System Status with Optional psutil Dependency",
  "summary": "The `SystemInfoTool` reports CPU, RAM, disk, network, and process information to agents, with graceful degradation to platform-only info when `psutil` is not installed. Tests cover the basic output contract, psutil-dependent sections (network, processes), fallback behavior without psutil, error handling when `get_system_status` raises, and the tool's name and schema definition.",
  "concepts": [
    "SystemInfoTool",
    "psutil",
    "get_system_status",
    "graceful_degradation",
    "optional_dependency",
    "process_listing",
    "network_stats",
    "system_info"
  ],
  "categories": [
    "tool-system",
    "testing",
    "system-monitoring",
    "test"
  ],
  "source_docs": [
    "f0c85a53238f9be3"
  ],
  "backlinks": null,
  "word_count": 345,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`SystemInfoTool` (tool name: `system_info`) gives agents visibility into the host machine's resource state. It wraps `get_system_status()` and adds optional process listing. The key design challenge is that `psutil` — the library providing detailed system metrics — is an optional dependency only available in the `pocketpaw[desktop]` install variant.

## Basic Output Contract

All installations, with or without `psutil`, must return a non-empty string containing `"System Status"`. This is the minimum viable output that an agent can parse to know the tool executed successfully.

## psutil-Dependent Sections

When `psutil` is available:
- `"Network"` section appears in the output (interface stats, bytes sent/received)
- `include_processes=True` adds a top-process list without raising
- By default (without `include_processes`), `"Top processes"` does not appear — preventing expensive process enumeration in routine status checks

These tests are skipped with `pytest.skip` when `psutil` is absent, rather than failing, because psutil availability is an environment fact, not a code defect.

## Fallback Without psutil

The fallback test patches both `get_system_status` (to return a limited-mode string) and `builtins.__import__` (to raise `ImportError` for `psutil`) to simulate a psutil-free environment:

```python
def mock_import(name, *args, **kwargs):
    if name == "psutil":
        raise ImportError("mocked")
    return real_import(name, *args, **kwargs)
```

The expected output contains `"limited"` and does not contain `"Network"`, confirming that the tool degrades gracefully rather than crashing. The install hint (`pip install 'pocketpaw[desktop]'`) should be surfaced to guide users toward the full installation.

## Error Handling

If `get_system_status` raises a `RuntimeError`, `execute` catches it and returns an error string containing both `"Error"` and the original exception message. This prevents agent loops from hanging on a broken status call.

```python
async def test_handles_status_error(self, sysinfo_tool):
    with patch("...get_system_status", side_effect=RuntimeError("boom")):
        result = await sysinfo_tool.execute()
    assert "Error" in result and "boom" in result
```

## Tool Definition

The tool's `name` property returns `"system_info"` (not `"sysinfo"` or `"status"`). The schema definition test (`test_definition_schema`) verifies the parameters structure is valid for injection into LLM tool call schemas.

## Known Gaps

No TODOs. The `TestWithoutPsutil` test uses a complex `builtins.__import__` patch that could be simplified with a more targeted mock on the module's internal import.