---
{
  "title": "Fleet Installer: Manifest Loading, Path Traversal Guard, and Install Orchestration Tests",
  "summary": "This test suite covers PocketPaw's fleet installer, which provisions a full AI agent environment (soul, pocket, connectors) from a declarative manifest. It validates YAML and JSON manifest loading, a P0 path-traversal security fix that clamps string inputs to the bundled fleet directory, install orchestration with partial failure handling, and the InstallReport shape.",
  "concepts": [
    "fleet installer",
    "FleetTemplate",
    "load_fleet",
    "path traversal",
    "bundled fleets",
    "install orchestration",
    "soul creation",
    "connector provisioning",
    "FleetInstallReport",
    "security clamp",
    "partial failure"
  ],
  "categories": [
    "testing",
    "security",
    "fleet management",
    "orchestration",
    "test"
  ],
  "source_docs": [
    "c5a6414caa0697be"
  ],
  "backlinks": null,
  "word_count": 505,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

A PocketPaw "fleet" is a packaged AI agent deployment manifest that specifies a soul template, a pocket name, scopes, widgets, and connectors to install. The fleet installer reads this manifest and orchestrates creation of all components in the correct order. This test suite locks in the loader, the security boundary around bundled fleet names, and the orchestration logic.

## Manifest Loading (`TestLoader`)

`load_fleet` accepts either a `Path` to a YAML/JSON file or a string name that references a bundled fleet. Tests verify:

- YAML manifests parse correctly into `FleetTemplate` objects with fields like `name`, `soul_template`, `scopes`.
- JSON manifests are also valid inputs.
- Bundled fleets load by name via `list_bundled_fleets()`.
- Missing files raise `FileNotFoundError` immediately rather than silently producing an empty template.

## Path Traversal Security Fix (`TestLoaderBundledNameClamp`)

This class was added in `fix/fleet-install-auth-guard` and is explicitly labeled a P0 security fix. The problem: the REST router passes a user-controlled string directly to `load_fleet`. Before the fix, a workspace admin could pass `"../../etc/passwd"` as a fleet name, causing the server to read arbitrary filesystem paths.

The fix clamps all string inputs to `_BUNDLED_DIR`. The test class pins four contract cases:

1. **`test_bundled_name_still_loads`** — Legitimate bundled names still work after the clamp. This is the regression guard: the fix must not break the happy path.
2. **`test_relative_traversal_rejects_as_not_found`** — `"../../etc/passwd"` returns a generic "not found" error, not an OS error or file contents.
3. **`test_absolute_path_rejects_as_not_found`** — Absolute paths like `/etc/passwd` also return "not found".
4. **`test_unknown_bundled_name_rejects_as_not_found`** — Names not in the bundled directory return "not found".
5. **`test_dotdot_segment_in_name_rejects`** — A name like `sales/../../../etc/passwd` is rejected.

The deliberate choice of a generic "not found" error (rather than "access denied" or "invalid path") is a security posture: the 4xx response never reveals whether the path exists on the filesystem, preventing directory enumeration.

## Install Orchestration (`TestInstallOrchestration`)

`install_fleet` coordinates soul creation, pocket creation, and connector provisioning. Tests use `fake_pocket_creator` and `fake_registry` fixtures that mock these dependencies:

- **`test_install_creates_soul_pocket_and_connectors`** — All three components are created in order for a basic fleet.
- **`test_install_skips_pocket_when_creator_missing`** — If the pocket creator is absent, the step is skipped but the install continues.
- **`test_install_marks_optional_missing_connector_as_skipped`** — Optional connectors that aren't available produce a `skipped` step, not a failure.
- **`test_install_marks_required_missing_connector_as_failed`** — Required connectors that are unavailable produce a `failed` step.
- **`test_install_swallows_per_step_exceptions`** — An exception in a single step doesn't abort the entire install.
- **`test_install_returns_early_on_soul_failure`** — If soul creation fails, the install aborts immediately. This makes sense because the pocket and connectors depend on the soul for identity.

## Sales Fleet Bundle (`TestSalesFleetBundle`)

Verifies that the `sales` fleet is bundled with PocketPaw and has expected properties (soul template, scopes, widgets, connectors). This is a contract test that prevents accidental deletion of the production fleet definition.

## Install Report (`TestInstallReport`)

`FleetInstallReport.succeeded` returns `True` only when no steps have `failed` status. `test_failed_steps_filters` confirms that only `failed` steps appear in the `failed_steps` list.

## Known Gaps

No TODOs observed. The path-traversal fix is explicitly tracked in the file header with a PR reference (`fix/fleet-install-auth-guard`).
