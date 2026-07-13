# tests/test_store_isolation_lint.py
# Created: 2026-06-26 (ISO-4 — store-isolation lint guard)
#
# AST-based regression guard: ensures no code in src/pocketpaw/ or
# ee/pocketpaw_ee/ directly constructs FabricStore(...) or
# InstinctStore(...) outside the approved factory seam.
#
# WORKSPACE RULE: all store access MUST go through the factories in
# pocketpaw.stores — get_fabric_store() and get_instinct_store(). Direct
# construction bypasses workspace resolution, the fail-closed scope check,
# the bounded LRU cache, and the StoreProvider seam, silently re-opening a
# shared store that breaks per-tenant physical isolation (ISO-1 / ISO-2).
#
# CHECKS IMPLEMENTED:
#   (a) Direct construction guard — ACTIVE.
#       Flags any ast.Call whose func is a Name or Attribute with id/attr
#       equal to "FabricStore" or "InstinctStore". This is the load-bearing
#       guard. The current factory in stores.py uses kind.cls(path) — an
#       Attribute on a dataclass field — which the AST represents as
#       Attribute(value=Name(id='kind'), attr='cls'), NOT as a Name/Attribute
#       ending in FabricStore/InstinctStore. So the factory is correctly
#       transparent to the guard without needing an allowlist exception.
#
#   (b) Bare get_fabric_store() / get_instinct_store() call without
#       workspace_id keyword — DROPPED.
#       The OSS agent-tool path legitimately calls these bare and relies on
#       the current_workspace ContextVar (ISO-3). Distinguishing that
#       legitimate bare call from a bug requires cross-module dataflow
#       analysis that an AST-only pass cannot reliably do. The
#       fail-closed POCKETPAW_REQUIRE_WORKSPACE_SCOPE env flag provides
#       the runtime guard for the cloud path; check (b) would produce
#       too many false positives to be useful as a CI gate.
#
# ALLOWLIST (construction guard):
#   Two files are excluded, each for a precise reason; everything else is
#   protected (a direct FabricStore(...)/InstinctStore(...) anywhere else fails
#   the guard and points the dev at the factory).
#     - the factory implementation (stores.py) — the legitimate construction site.
#     - the ISO-4 migration — its WRITES go through build_workspace_store (the
#       approved seam), but it must also READ the SHARED {fabric,instinct}.db and
#       verify the source audit chain before re-chaining, and the shared file is
#       NOT a workspace path, so no factory call can open it. Direct construction
#       on the shared path is legitimate there and only there.
#
#   ALLOWLIST_PATHS (relative to the worktree root):
#     src/pocketpaw/stores.py
#         The factory module — the ONLY place direct construction is a
#         legitimate implementation choice (today the factory uses kind.cls
#         indirection, not a bare Name call, so it would not be flagged anyway;
#         the exemption future-proofs an explicit refactor).
#     src/pocketpaw/migrations/split_workspace_stores.py
#         The shared->per-workspace migration — must construct a store on the
#         SHARED db path (not a workspace path) to read it + verify its audit
#         chain, which no factory call can do.
#
# NO pytest-asyncio needed — pure synchronous AST walk.

from __future__ import annotations

import ast
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent

_SOURCE_ROOTS = [
    _REPO_ROOT / "src" / "pocketpaw",
    _REPO_ROOT / "ee" / "pocketpaw_ee",
]

# Relative-to-repo-root paths that are ALLOWED to contain direct FabricStore /
# InstinctStore construction calls.
_ALLOWLIST_REL: frozenset[str] = frozenset(
    [
        # The factory module IS the legitimate construction site (the _StoreKind
        # registry + _build_local_workspace_store).
        "src/pocketpaw/stores.py",
        # The ISO-4 migration is the one place that bridges the SHARED stores to
        # the per-workspace ones. Its WRITES go through build_workspace_store (the
        # approved seam), but it must also READ the shared ~/.pocketpaw/{fabric,
        # instinct}.db files + verify the source audit chain before re-chaining —
        # and the shared file is, by definition, NOT a workspace path, so NO
        # factory call can open it. Constructing the store on the shared path
        # directly is therefore legitimate HERE and only here. Exempted with that
        # narrow justification; the guard still protects every other module.
        "src/pocketpaw/migrations/split_workspace_stores.py",
    ]
)

# Store class names whose direct construction is forbidden outside the allowlist.
_FORBIDDEN_NAMES: frozenset[str] = frozenset(["FabricStore", "InstinctStore"])

# ---------------------------------------------------------------------------
# Core checker (reusable — called by both the real-tree test and the self-test)
# ---------------------------------------------------------------------------


def find_store_construction_violations(
    tree: ast.AST,
    filename: str,
    allowlist: frozenset[str],
) -> list[str]:
    """Walk ``tree`` and return a list of violation strings.

    A violation is any ``ast.Call`` whose ``func`` is:
      * an ``ast.Name`` with ``id`` in _FORBIDDEN_NAMES, OR
      * an ``ast.Attribute`` with ``attr`` in _FORBIDDEN_NAMES.

    Each string in the returned list is formatted as::

        path/to/file.py:LINE — FabricStore() called directly; use get_fabric_store()

    ``filename`` is the string used in the violation message (and matched
    against ``allowlist``).  ``allowlist`` is a frozenset of filenames /
    relative paths to skip.
    """
    if filename in allowlist:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in _FORBIDDEN_NAMES:
            violations.append(
                f"{filename}:{node.lineno} — {name}() called directly; "
                f"use get_{name[0].lower() + name[1:]}() from pocketpaw.stores"
            )
    return violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_source_files() -> list[pathlib.Path]:
    """Return all .py files under the source roots, excluding noise."""
    files: list[pathlib.Path] = []
    for root in _SOURCE_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            parts = path.parts
            # Skip bytecode cache and virtual environments.
            if any(segment in ("__pycache__", ".venv") for segment in parts):
                continue
            files.append(path)
    return files


def _rel(path: pathlib.Path) -> str:
    """Return a repo-root-relative POSIX string for ``path``."""
    try:
        return path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Real-tree scan test
# ---------------------------------------------------------------------------


def test_no_direct_store_construction_in_source() -> None:
    """Fail if any source file directly constructs FabricStore or InstinctStore.

    Scans src/pocketpaw/ and ee/pocketpaw_ee/ via the Python AST.  The
    allowlisted files (the factory implementation + the ISO-4 migration) are
    exempted.  The test reports every violation with file and line number so
    a developer knows exactly where to fix the regression.
    """
    source_files = _iter_source_files()
    assert source_files, (
        "No source files found under the expected roots — "
        "check that _SOURCE_ROOTS point to the right directories."
    )

    all_violations: list[str] = []
    parse_errors: list[str] = []

    for path in source_files:
        rel = _rel(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            parse_errors.append(f"{rel}: {exc}")
            continue

        violations = find_store_construction_violations(tree, rel, _ALLOWLIST_REL)
        all_violations.extend(violations)

    if parse_errors:
        pytest.fail(
            "Syntax errors prevented full scan — fix these files first:\n"
            + "\n".join(f"  {e}" for e in parse_errors)
        )

    if all_violations:
        lines = "\n".join(f"  {v}" for v in all_violations)
        pytest.fail(
            f"Store isolation violation(s) detected ({len(all_violations)} total).\n"
            "Direct FabricStore/InstinctStore construction bypasses the factory,\n"
            "workspace resolution, the fail-closed scope check, and the LRU cache.\n"
            "Use get_fabric_store() / get_instinct_store() from pocketpaw.stores.\n"
            f"\nViolations:\n{lines}\n"
            f"\nAllowlisted paths (exempt from this check):\n"
            + "\n".join(f"  {p}" for p in sorted(_ALLOWLIST_REL))
        )


# ---------------------------------------------------------------------------
# Self-test — planted violation must be caught
# ---------------------------------------------------------------------------


def test_checker_catches_planted_violation() -> None:
    """Self-test: the checker must flag a synthetic FabricStore() construction.

    Feeds a small snippet of code containing a forbidden direct construction
    call (parsed in-memory) to find_store_construction_violations and asserts
    at least one violation is returned.  This proves the guard is not vacuously
    passing — it actually detects the pattern it is supposed to catch.
    """
    snippet = """
from pocketpaw.fabric.store import FabricStore

def bad_function(db_path):
    # BUG: direct construction bypasses the factory
    store = FabricStore(db_path)
    return store
"""
    tree = ast.parse(snippet)
    violations = find_store_construction_violations(
        tree,
        filename="hypothetical/bad_module.py",
        allowlist=frozenset(),  # no exemptions — violation must be reported
    )
    assert violations, (
        "Self-test FAILED: find_store_construction_violations did not catch a "
        "planted FabricStore() direct-construction call.  The guard is broken."
    )
    # Confirm the violation message mentions the right class and line.
    assert any("FabricStore" in v for v in violations), (
        f"Expected 'FabricStore' in violation messages, got: {violations}"
    )


def test_checker_catches_planted_instinct_violation() -> None:
    """Self-test: the checker must flag a synthetic InstinctStore() construction.

    Same pattern as the FabricStore self-test but for InstinctStore, ensuring
    both forbidden names are live in the guard.
    """
    snippet = """
from pocketpaw.instinct.store import InstinctStore

class BadService:
    def __init__(self, path):
        # BUG: should call get_instinct_store() instead
        self.store = InstinctStore(path)
"""
    tree = ast.parse(snippet)
    violations = find_store_construction_violations(
        tree,
        filename="hypothetical/bad_service.py",
        allowlist=frozenset(),
    )
    assert violations, (
        "Self-test FAILED: find_store_construction_violations did not catch a "
        "planted InstinctStore() direct-construction call.  The guard is broken."
    )
    assert any("InstinctStore" in v for v in violations), (
        f"Expected 'InstinctStore' in violation messages, got: {violations}"
    )


def test_allowlisted_file_is_not_flagged() -> None:
    """Self-test: files in the allowlist must not produce violations.

    Feeds the same forbidden snippet but uses 'src/pocketpaw/stores.py'
    as the filename, which IS in the allowlist.  Confirms exemptions work
    correctly and the factory file can legitimately contain construction calls.
    """
    snippet = """
from pocketpaw.fabric.store import FabricStore
store = FabricStore('/some/path/fabric.db')
"""
    tree = ast.parse(snippet)
    violations = find_store_construction_violations(
        tree,
        filename="src/pocketpaw/stores.py",
        allowlist=_ALLOWLIST_REL,
    )
    assert not violations, f"Allowlisted file should not produce violations, but got: {violations}"
