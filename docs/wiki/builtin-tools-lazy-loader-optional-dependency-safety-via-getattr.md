---
{
  "title": "Builtin Tools Lazy Loader: Optional Dependency Safety via __getattr__",
  "summary": "The `pocketpaw/tools/builtin/__init__.py` implements a lazy import scheme using Python's module-level `__getattr__` hook, ensuring that importing one built-in tool never fails due to a missing optional dependency of a different tool. This lets PocketPaw operate with a minimal install footprint while still exposing a rich tool catalogue.",
  "concepts": [
    "lazy imports",
    "__getattr__",
    "optional dependencies",
    "built-in tools",
    "importlib",
    "enterprise tools",
    "tool catalogue",
    "import isolation",
    "module-level hook"
  ],
  "categories": [
    "tool-system",
    "package-structure",
    "python-patterns"
  ],
  "source_docs": [
    "d3b62d7010bf6531"
  ],
  "backlinks": null,
  "word_count": 357,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw ships dozens of built-in tools — browser automation (Playwright), speech-to-text (Whisper), image generation (DALL-E), Google Drive, Spotify, and more. Each of these tools has optional system dependencies. If `from pocketpaw.tools.builtin import BrowserTool` triggered all tool imports eagerly, a missing Playwright install would break every tool in the package, even ones with no browser dependency.

The `__getattr__` hook solves this: tool classes are imported only when first accessed by name.

## How __getattr__ Works

Python calls a module's `__getattr__` function when an attribute lookup fails the normal `dir()` check. The `_LAZY_IMPORTS` dictionary maps exported names to `(submodule, class_name)` tuples:

```python
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "ShellTool": (".shell", "ShellTool"),
    "BrowserTool": (".browser", "BrowserTool"),
    "RememberTool": (".memory", "RememberTool"),
    # ... 40+ more entries
}

def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, class_name = _LAZY_IMPORTS[name]
        module = _importlib.import_module(module_path, package=__name__)
        return getattr(module, class_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

The first access imports the submodule and caches the result in the module's namespace, so subsequent accesses skip `__getattr__` entirely.

## Growth History

The comment block at the top of the file is a chronological changelog of tool additions, revealing the pace of the built-in tool library:

- **2026-02-05**: `RememberTool`, `RecallTool` (memory)
- **2026-02-06**: `WebSearchTool`, `ImageGenerateTool`, `CreateSkillTool`
- **2026-02-07**: Gmail, Calendar, Voice, Research, Delegate tools
- **2026-02-09**: STT, Drive, Docs, Spotify, OCR, Reddit tools — plus the switch to lazy loading
- **2026-02-17**: `HealthCheckTool`, `ErrorLogTool`, `ConfigDoctorTool`
- **2026-03-12**: `EditFileTool`, `RunPythonTool`, `InstallPackageTool`
- **2026-03-27**: `AddWidgetTool`, `RemoveWidgetTool`
- **2026-03-28**: Fabric + Instinct enterprise tools (guarded by `ee/` availability)

The conversion to lazy loading on 2026-02-09 was a direct response to import failures when tools were added that required optional dependencies.

## Enterprise Tool Guarding

Fabric and Instinct tools (added 2026-03-28) are conditionally included: the lazy import will raise `ImportError` if the `ee/` (enterprise edition) package is absent. The `__getattr__` approach gracefully surfaces this as an `AttributeError` rather than crashing at package import time.

## Known Gaps

- The `_LAZY_IMPORTS` map must be manually updated every time a new tool is added. There is no automatic discovery mechanism. Forgetting to add an entry means the tool is silently inaccessible from `pocketpaw.tools.builtin`.