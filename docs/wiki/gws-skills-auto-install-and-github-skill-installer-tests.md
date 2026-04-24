---
{
  "title": "GWS Skills Auto-Install and GitHub Skill Installer Tests",
  "summary": "This test suite covers `install_skills_from_github`, a shared async helper that clones a GitHub repository and copies SKILL.md-bearing subdirectories into the local skills install directory, with support for prefix filtering and single-skill targeting. It also tests that the GWS preset installation triggers a non-blocking background task that auto-installs Google Workspace skills.",
  "concepts": [
    "skills installer",
    "install_skills_from_github",
    "GitHub clone",
    "SKILL.md",
    "prefix filter",
    "GWS auto-install",
    "MCP preset",
    "non-blocking failure",
    "asyncio subprocess",
    "skill discovery"
  ],
  "categories": [
    "testing",
    "skills system",
    "MCP integration",
    "error handling",
    "test"
  ],
  "source_docs": [
    "de38b28bc30347e6"
  ],
  "backlinks": null,
  "word_count": 479,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's skill system allows the agent to be extended with reusable behavioral modules ("skills") stored in GitHub repositories. The `install_skills_from_github` function provides a programmatic way to pull those skills at runtime — for example, when a user installs the Google Workspace MCP preset, the agent automatically installs the companion GWS skills from GitHub.

The tests in `test_gws_skills_install.py` pin two distinct concerns: the correctness of the core installer helper and the resilience of the auto-install trigger wired into the MCP API.

## `TestInstallSkillsFromGitHub`

The `fake_repo` fixture builds a synthetic cloned repo structure in a temporary directory with four skill directories (`gws-gmail`, `gws-sheets`, `gws-shared`, `persona-exec`), each containing a minimal `SKILL.md` frontmatter file. This isolates tests from real network calls.

All tests patch `asyncio.create_subprocess_exec` with an async mock that simulates a successful `git clone` (returncode 0) and patch `tempfile.TemporaryDirectory` to point at the pre-built fake repo rather than creating a real temp directory.

### Key test cases

- **`test_install_all_skills`** — No filter applied; expects all four skills installed into the target directory. Verifies that `gws-gmail/SKILL.md` physically exists at the install path.
- **`test_install_with_prefix_filter`** — Passing `prefix_filter="gws-"` should select only the three `gws-*` skills and exclude `persona-exec`. This prevents unintended skill pollution when a repo contains mixed content.
- **`test_install_specific_skill`** — Passing `skill_name="gws-gmail"` installs exactly one skill. Used for targeted updates without re-installing a full prefix group.
- **`test_clone_failure_raises`** — When `git clone` returns a non-zero exit code, the function must raise `RuntimeError("Clone failed")`. This prevents silently proceeding with a missing or partial repo, which would leave the skills directory in an inconsistent state.

## `TestGwsAutoInstall`

This class tests the integration point between the MCP preset API and the skill installer:

- **`test_gws_preset_triggers_skill_install`** — Verifies that `_install_gws_skills` exists as a callable in `pocketpaw.api.v1.mcp`. The actual trigger (a `asyncio.create_task` call when the GWS preset is installed) is tested indirectly by confirming the function is importable and callable. This is a lightweight existence check rather than a full end-to-end test.
- **`test_install_gws_skills_failure_non_blocking`** — Patches `install_skills_from_github` to raise `RuntimeError("network error")` and asserts that `_install_gws_skills` does **not** propagate the exception. This is critical: a failed skill install should never crash the MCP preset installation. The user gets their preset; skills can be installed later.

## Why Non-Blocking Failure Matters

Skill installation requires network access. In offline environments, corporate proxies, or GitHub outages, the clone will fail. If that failure propagated up through the MCP API endpoint, users would see a 500 error when installing a preset — even though the preset itself installed successfully. The defensive catch-and-swallow pattern in `_install_gws_skills` decouples skill availability from preset availability.

## Known Gaps

The `test_image_generation_success` analog for network-based install is not present — there is no test that verifies the content of copied `SKILL.md` files is preserved verbatim. The prefix filter logic is tested against exact names but not against edge cases like empty prefix strings or case sensitivity.