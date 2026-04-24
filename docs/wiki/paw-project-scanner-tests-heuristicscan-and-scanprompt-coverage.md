---
{
  "title": "Paw Project Scanner Tests: heuristic_scan and scan_prompt Coverage",
  "summary": "The `paw scan` heuristic reads a project's on-disk artifacts — README, `pyproject.toml`, `package.json`, `.env.example`, and top-level directory structure — and stores extracted facts into the soul's memory. These tests verify that each artifact type is correctly parsed, that the soul's `remember()` method is called with appropriate metadata, and that failures in memory storage do not abort the scan.",
  "concepts": [
    "heuristic_scan",
    "project scanning",
    "scan_prompt",
    "README detection",
    "pyproject.toml",
    "package.json",
    ".env.example",
    "soul.remember",
    "importance weighting",
    "error resilience"
  ],
  "categories": [
    "testing",
    "project initialization",
    "soul integration",
    "test"
  ],
  "source_docs": [
    "c7a3ad0540c07a52"
  ],
  "backlinks": null,
  "word_count": 498,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a developer runs `paw init` or `paw scan` on a new project, PocketPaw scans the codebase to pre-populate the soul with project context. This removes the need to manually tell the AI companion what the project does. The `heuristic_scan` function and the `scan_prompt` template are the two components under test.

## Scan Prompt Validation

`TestScanPrompt` verifies that the LLM prompt template used during scanning contains required placeholders and keywords:

- `{project_path}` placeholder is present (it must be formatted before sending to the LLM).
- The prompt mentions `README` and `pyproject.toml` to guide the model on what to look for.
- The prompt references `soul_remember` to instruct the model to emit memory calls.

These tests catch template corruption — a missing placeholder that would cause a `KeyError` at runtime, or a removed instruction that would cause the model to forget to store results.

## README Detection

`TestHeuristicScanReadme` verifies README discovery and storage:

- **`README.md` content stored**: the file content is passed to `soul.remember()` with high importance.
- **`.rst` fallback**: if `README.md` is absent, `README.rst` is tried next.
- **`.md` takes precedence**: when both exist, `.md` is preferred.
- **No README**: the scan continues without error, no call is made.
- **High importance**: README facts are stored with importance >= 7, reflecting that project-level context is critical for soul recall.

```python
async def test_readme_stored_with_high_importance(tmp_path, mock_soul):
    (tmp_path / "README.md").write_text("# My Project")
    await heuristic_scan(tmp_path, mock_soul)
    call_args = mock_soul.remember.call_args
    assert call_args.kwargs.get("importance", 0) >= 7
```

## pyproject.toml and package.json

Both `TestHeuristicScanPyproject` and `TestHeuristicScanPackageJson` verify that the file content is stored and that the stored fact includes a label identifying the source file. This label prevents the soul from conflating Python and JavaScript project metadata during recall.

## .env.example: Variable Name Extraction

`TestHeuristicScanEnvExample` is particularly security-conscious:

- **Variable names extracted**: key names like `DATABASE_URL` are stored so the soul knows what environment variables the project expects.
- **Commented lines ignored**: lines starting with `#` are skipped — comments in `.env.example` files often contain example values that look like real secrets.
- **No file**: scan continues without error.

Storing only variable names (not values) is a deliberate design — `.env.example` values might be real credentials accidentally committed.

## Directory Structure

`TestHeuristicScanDirectoryStructure` verifies:

- Top-level directories are stored, giving the soul a project map.
- `soul.remember()` is called at least once (the scan produces output).
- If `soul.remember()` raises an exception (e.g., soul database write failure), the scan continues and does not propagate the error.

The failure resilience test is critical: a transient soul storage error should not abort a project scan, as the scan may be run during initial setup when the soul is not yet fully initialized.

## Known Gaps

- No test for very large README files — truncation behavior (if any) is untested.
- No test for non-UTF-8 encoded files, which would cause a `UnicodeDecodeError`.
- `TestHeuristicScanEnvExample` does not test what happens when `.env.example` contains actual secret values (e.g., a key with 40 chars that resembles an API key).