---
{
  "title": "Paw CLI Tests: init, doctor, channels, and serve Command Coverage",
  "summary": "This test file validates PocketPaw's `paw` CLI entry point using Click's `CliRunner`, covering the main command group, `init`, `doctor`, `channels`, and `serve` subcommands. It focuses on exit codes, output presence, option parsing, and graceful degradation when optional dependencies like `soul-protocol` are absent.",
  "concepts": [
    "paw CLI",
    "Click",
    "CliRunner",
    "init command",
    "doctor command",
    "channels command",
    "serve command",
    "soul-protocol optional dependency",
    "async CLI bridge",
    "exit codes"
  ],
  "categories": [
    "testing",
    "CLI",
    "developer tooling",
    "test"
  ],
  "source_docs": [
    "58c7a5085fe94b73"
  ],
  "backlinks": null,
  "word_count": 550,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `paw` CLI is the primary developer interface for configuring and running PocketPaw agents. These tests use Click's `CliRunner` to invoke commands in-process, capturing output and exit codes without spawning subprocesses. This approach makes tests fast and portable across environments.

## Main Group and Help

`TestMainGroup` verifies the CLI entry point itself:

- **No subcommand**: invoking `paw` with no arguments shows help text and exits zero — standard UX for CLI tools.
- **`--help` flag**: exits zero with help output.
- **`--version` flag**: outputs the version string, confirming the package version is correctly wired into the CLI.

These three tests catch the most common packaging regressions: broken imports, missing entry points, and version string misconfigurations.

## init Command: Graceful Dependency Handling

`TestInitCommand` is the most defensively designed group in this suite:

```python
def test_init_fails_gracefully_when_soul_protocol_missing(tmp_path):
    ...
```

This test patches `soul-protocol` as unavailable and verifies `paw init` exits with a non-zero code and a helpful error message rather than an unhandled `ImportError` traceback. This matters because `soul-protocol` is an optional dependency — users can run PocketPaw without it.

`test_init_with_mocked_async_impl` patches the async init implementation and verifies the CLI correctly bridges the sync Click context to the async implementation. This is a common pain point in FastAPI/asyncio projects where `asyncio.run()` must be called from a sync Click handler.

The `--name` and `--provider` option tests confirm that CLI options are parsed and forwarded to the underlying implementation, not silently dropped.

## doctor Command: Environment Health Checks

`TestDoctorCommand` validates the `paw doctor` diagnostic tool:

- **Basic run**: exits without error even in a minimal environment.
- **soul-protocol present**: reports "ok" status.
- **soul-protocol missing**: reports a "fail" or "missing" status with actionable output.
- **`.paw` directory check**: reports whether the project's `.paw/` directory exists.
- **`.paw` directory present**: correctly identifies and reports a found directory.

The doctor command exists because PocketPaw has multiple optional dependencies and config directories that must align. Rather than force users to read documentation when something breaks, `paw doctor` surfaces missing prerequisites directly.

## channels Command: Guard Clause on Flags

`TestChannelsCommand` verifies a UX guard:

- **No flags exits non-zero**: `paw channels` with no arguments should not silently succeed — it has no default behavior and requires specifying a channel.
- **Usage hint shown**: the output must include a hint directing the user to pass a flag like `--telegram`.
- **Telegram flag parsing**: `--telegram` is accepted by the parser and passes the guard check.

This pattern prevents the common mistake of running `paw channels` and getting confusing empty output.

## serve Command: Placeholder and Port Option

`TestServeCommand` covers the `paw serve` command:

- **Placeholder output**: confirms the command runs and outputs at least a status message. This test exists to catch import errors in the serve module more than to test real server behavior.
- **Custom port**: `--port 9000` is accepted, confirming the option is wired through to the underlying server config.

## Known Gaps

- `test_init_with_mocked_async_impl` patches the entire async impl, meaning the actual `paw init` logic path is not exercised in tests.
- No test covers what happens when `paw init` is run in an already-initialized project (idempotency).
- `paw serve` tests are shallow — no test verifies the server actually listens or handles requests.
- Channel-specific flags beyond `--telegram` (e.g., `--discord`, `--whatsapp`) are not tested.