# Tests for packaging and dependency sanity.
#
# Created: 2026-02-14
# Reproduces: FastAPI/starlette version conflict (claude-agent-sdk -> mcp ->
# starlette 0.52, but fastapi>=0.109.0 needs starlette<0.36), and default-mode
# deps (dashboard) not being in core.

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


# ---------------------------------------------------------------------------
# Bug 1: FastAPI version pin too low for starlette 0.52+
#   claude-agent-sdk -> mcp 1.26 -> starlette 0.52
#   fastapi 0.109.0 requires starlette<0.36.0  →  CONFLICT
# ---------------------------------------------------------------------------
def test_fastapi_version_allows_modern_starlette():
    """FastAPI lower-bound must be >= 0.115.0 to coexist with mcp's starlette."""
    data = _load_pyproject()

    # FastAPI can be in core deps OR in the dashboard extra
    all_deps = list(data["project"]["dependencies"])
    for extra_deps in data["project"].get("optional-dependencies", {}).values():
        all_deps.extend(extra_deps)

    fastapi_specs = [d for d in all_deps if d.lower().startswith("fastapi")]
    assert fastapi_specs, "fastapi not found in any dependency list"

    for spec in fastapi_specs:
        # Extract minimum version from spec like "fastapi>=0.115.0"
        if ">=" in spec:
            min_ver = spec.split(">=")[1].split(",")[0].strip()
            parts = [int(x) for x in min_ver.split(".")]
            # Must be at least 0.115.0
            assert parts >= [0, 115, 0], (
                f"fastapi>={min_ver} is too low — mcp pulls starlette 0.52+ which "
                f"needs fastapi>=0.115.0. Got: {spec}"
            )


# ---------------------------------------------------------------------------
# Bug 2: Default mode (dashboard) deps not in core
#   `pip install pocketpaw` → `pocketpaw` → "Missing fastapi" error
#   The default mode should work without extras.
# ---------------------------------------------------------------------------
def test_default_mode_deps_in_core():
    """Core deps must include everything needed for the default mode (dashboard)."""
    data = _load_pyproject()
    core_deps = [
        d.lower().split(">=")[0].split("[")[0].strip() for d in data["project"]["dependencies"]
    ]

    required_for_default = ["fastapi", "uvicorn", "jinja2"]
    for pkg in required_for_default:
        assert pkg in core_deps, (
            f"'{pkg}' must be a core dependency — the default mode (dashboard) "
            f"needs it, but it's only in an optional extra. Users who run "
            f"'pip install pocketpaw' then 'pocketpaw' will get an import error."
        )


# ---------------------------------------------------------------------------
# Bug 3: Duplicate dependencies
# ---------------------------------------------------------------------------
def test_no_duplicate_core_deps():
    """Core deps should not have duplicate entries."""
    data = _load_pyproject()
    core_deps = data["project"]["dependencies"]
    # Normalize: lowercase, strip version specifiers
    names = [
        d.lower().split(">=")[0].split(">")[0].split("==")[0].split("[")[0].strip()
        for d in core_deps
    ]
    dupes = [n for n in set(names) if names.count(n) > 1]
    assert not dupes, f"Duplicate core dependencies: {dupes}"


# ---------------------------------------------------------------------------
# Bug 4: Version string mismatch
# ---------------------------------------------------------------------------
def test_version_consistency():
    """__main__.py --version should use dynamic version or match pyproject.toml."""
    data = _load_pyproject()
    pyproject_version = data["project"]["version"]

    main_path = Path(__file__).resolve().parent.parent / "src" / "pocketclaw" / "__main__.py"
    main_text = main_path.read_text()

    # Either the version string is hardcoded and matches pyproject.toml,
    # or __main__.py uses dynamic version via importlib.metadata.version()
    uses_dynamic = "get_version(" in main_text or "importlib.metadata" in main_text
    has_literal = pyproject_version in main_text

    assert uses_dynamic or has_literal, (
        f"__main__.py --version does not contain '{pyproject_version}' from pyproject.toml "
        f"and does not use dynamic versioning (importlib.metadata). Version string is out of sync."
    )


# ---------------------------------------------------------------------------
# Bug 5: dashboard extra should still exist for backward compat
# ---------------------------------------------------------------------------
def test_dashboard_extra_exists():
    """The [dashboard] extra must exist for backward compat (even if empty)."""
    data = _load_pyproject()
    extras = data["project"].get("optional-dependencies", {})
    assert "dashboard" in extras, (
        "The 'dashboard' extra must exist for backward compatibility — "
        "users may have 'pip install pocketpaw[dashboard]' in their scripts."
    )


# ---------------------------------------------------------------------------
# Sanity: uvicorn version constraint aligns with mcp's transitive requirement
# ---------------------------------------------------------------------------
def test_uvicorn_version_not_too_old():
    """uvicorn lower-bound should be >=0.31.1 to align with mcp's requirement."""
    data = _load_pyproject()
    all_deps = list(data["project"]["dependencies"])
    for extra_deps in data["project"].get("optional-dependencies", {}).values():
        all_deps.extend(extra_deps)

    uvicorn_specs = [d for d in all_deps if "uvicorn" in d.lower()]
    assert uvicorn_specs, "uvicorn not found in any dependency list"

    for spec in uvicorn_specs:
        if ">=" in spec:
            min_ver = spec.split(">=")[1].split(",")[0].strip()
            parts = [int(x) for x in min_ver.split(".")]
            assert parts >= [0, 31, 1], (
                f"uvicorn>={min_ver} is too low — mcp requires >=0.31.1. Got: {spec}"
            )


# ---------------------------------------------------------------------------
# Sanity: installer VERSION matches pyproject.toml
# ---------------------------------------------------------------------------
def test_installer_version_matches():
    """installer/installer.py VERSION should match pyproject.toml."""
    data = _load_pyproject()
    pyproject_version = data["project"]["version"]

    installer_path = Path(__file__).resolve().parent.parent / "installer" / "installer.py"
    if not installer_path.exists():
        return  # Skip if installer not present

    installer_text = installer_path.read_text()
    # VERSION = "0.2.0" line
    for line in installer_text.splitlines():
        if line.startswith("VERSION"):
            installer_ver = line.split("=")[1].strip().strip('"').strip("'")
            assert installer_ver == pyproject_version, (
                f"installer.py VERSION={installer_ver} != pyproject.toml version={pyproject_version}"
            )
            break
