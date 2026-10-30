# Tests for the Settings field ownership registry (config_tiers.py).
#
# Changes:
#   - 2026-08-16: Initial implementation. Covers the four drift risks: a field
#     renamed in config.py, a miscount from a field landing in two tiers, the
#     unlisted-field default, and a new frontend control shipped without an
#     owner. The last of those reads the sibling paw-enterprise repo and skips
#     when it is absent, so CI without it stays green.

"""Tests for the three-tier field ownership registry."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from pocketpaw.config import Settings
from pocketpaw.config_tiers import TENANT_SECRETS_BLOCKED, TIER_OF, Tier, tier_of
from pocketpaw.credentials import SECRET_FIELDS


def _fields_in(tier: Tier) -> set[str]:
    """The registry's own view of a tier — derived from the exported mapping.

    Deliberately not the private per-tier frozensets that build ``TIER_OF``:
    the mapping is what the resolver will read, so it is what gets asserted.
    """
    return {field for field, owner in TIER_OF.items() if owner is tier}


class TestRegistryIntegrity:
    """The registry must describe fields that actually exist."""

    def test_every_registered_field_exists_on_settings(self):
        """Catches typos, and catches a field renamed in config.py later."""
        unknown = sorted(set(TIER_OF) - set(Settings.model_fields))
        assert unknown == [], f"registered but not a Settings field: {unknown}"

    def test_registry_has_55_entries(self):
        """The UI-exposed set. Not one entry per Settings field — see the docstring."""
        assert len(TIER_OF) == 55

    def test_per_tier_counts(self):
        """A field landing in two tiers would silently shift these."""
        assert len(_fields_in(Tier.USER)) == 5
        assert len(_fields_in(Tier.WORKSPACE)) == 33
        assert len(_fields_in(Tier.PLATFORM)) == 17

    def test_tiers_partition_the_registry(self):
        """Every entry is one of the three tiers, and they sum to the whole."""
        total = sum(len(_fields_in(tier)) for tier in Tier)
        assert total == len(TIER_OF)


class TestTierOf:
    """The lookup helper, including its safe default."""

    def test_unlisted_field_defaults_to_platform(self):
        """The safe default: unclassified means operator-only, i.e. today's behaviour."""
        assert "anthropic_api_key" not in TIER_OF
        assert tier_of("anthropic_api_key") is Tier.PLATFORM

    def test_field_that_does_not_exist_at_all_is_platform(self):
        """Fails closed rather than raising."""
        assert tier_of("no_such_field_anywhere") is Tier.PLATFORM

    def test_known_user_field(self):
        assert tier_of("theme_preference") is Tier.USER

    def test_known_workspace_field(self):
        assert tier_of("memory_backend") is Tier.WORKSPACE

    def test_known_platform_field(self):
        assert tier_of("api_rate_limit_per_key") is Tier.PLATFORM

    def test_default_workspace_dir_is_platform_not_user(self):
        """The trap: it renders on the preferences page but is a server path."""
        assert tier_of("default_workspace_dir") is Tier.PLATFORM


class TestTenantSecretsBlocked:
    """Secret-bearing WORKSPACE fields stay classified but unexposed."""

    def test_blocked_secrets_are_classified_workspace(self):
        assert TENANT_SECRETS_BLOCKED <= _fields_in(Tier.WORKSPACE)

    def test_blocked_secrets_are_real_secrets(self):
        assert TENANT_SECRETS_BLOCKED <= SECRET_FIELDS

    def test_every_workspace_secret_is_blocked(self):
        """No WORKSPACE field may carry a secret without being on the blocklist.

        Without this, adding the next tenant-supplied API key to the WORKSPACE
        tier would quietly expose it at a tier that has no encryption at rest.
        """
        workspace_secrets = _fields_in(Tier.WORKSPACE) & SECRET_FIELDS
        assert workspace_secrets == set(TENANT_SECRETS_BLOCKED)

    def test_no_user_tier_secrets(self):
        """Nothing at the USER tier may carry a secret."""
        assert _fields_in(Tier.USER) & SECRET_FIELDS == set()


# ---------------------------------------------------------------------------
# Completeness gate against the frontend
# ---------------------------------------------------------------------------

_FIELD_KEY_RE = re.compile(r'fieldKey="([a-zA-Z0-9_]+)"')


_SETTINGS_ROUTES = Path("src") / "routes" / "settings"


def _paw_enterprise_settings_dir() -> Path | None:
    """Locate the sibling paw-enterprise settings routes, or None if absent.

    paw-enterprise is a separate repo checked out beside pocketpaw. It is not
    present in every checkout or CI environment, so this gate skips rather than
    fails when it is missing.

    ``PAW_ENTERPRISE_DIR`` overrides the search and should point at the repo
    root. Without it, every ancestor of this file is checked for a
    ``paw-enterprise`` sibling.

    The ancestor walk alone is not enough, and the reason is worth stating: a
    git worktree of pocketpaw lives OUTSIDE the workspace (``D:/paw-worktrees/
    settings-field-tiers``), so no ancestor of it holds a ``paw-enterprise``
    sibling and the walk returns None. The gate then skips — silently, and
    exactly in the setup an implementer working on a feature branch uses. A
    completeness gate that no-ops in the common case is decoration.

    So the worktree case is resolved explicitly: ask git for the main
    checkout's path (``git rev-parse --path-format=absolute --git-common-dir``
    points at ``<main>/.git`` even from a linked worktree) and look for the
    sibling beside that.
    """
    override = os.environ.get("PAW_ENTERPRISE_DIR")
    if override:
        candidate = Path(override) / _SETTINGS_ROUTES
        return candidate if candidate.is_dir() else None

    here = Path(__file__).resolve()
    roots = list(here.parents)

    # A linked worktree's ancestors don't include the workspace, so resolve the
    # main checkout via git and search from there too.
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if common.returncode == 0 and common.stdout.strip():
            roots.extend(Path(common.stdout.strip()).resolve().parents)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - git absent
        pass

    for ancestor in roots:
        candidate = ancestor / "paw-enterprise" / _SETTINGS_ROUTES
        if candidate.is_dir():
            return candidate
    return None


def _ui_field_keys(settings_dir: Path) -> set[str]:
    keys: set[str] = set()
    for path in settings_dir.rglob("*.svelte"):
        keys.update(_FIELD_KEY_RE.findall(path.read_text(encoding="utf-8")))
    return keys


class TestFrontendCompleteness:
    """Every field the frontend exposes must have a declared owner."""

    def test_every_ui_field_key_is_classified(self):
        settings_dir = _paw_enterprise_settings_dir()
        if settings_dir is None:
            pytest.skip(
                "sibling repo paw-enterprise not checked out beside pocketpaw; "
                "the frontend completeness gate needs its src/routes/settings/"
            )

        ui_keys = _ui_field_keys(settings_dir)
        assert ui_keys, f"no fieldKey= attributes found under {settings_dir} — regex drift?"

        unclassified = sorted(ui_keys - set(TIER_OF))
        assert unclassified == [], (
            f"settings controls exposed by the frontend with no declared owner: "
            f"{unclassified}. Add each to TIER_OF in src/pocketpaw/config_tiers.py."
        )

    def test_registry_does_not_outrun_the_ui(self):
        """The other direction: a stale entry for a control the UI dropped."""
        settings_dir = _paw_enterprise_settings_dir()
        if settings_dir is None:
            pytest.skip("sibling repo paw-enterprise not checked out beside pocketpaw")

        stale = sorted(set(TIER_OF) - _ui_field_keys(settings_dir))
        assert stale == [], (
            f"registered but no longer exposed by the frontend: {stale}. "
            f"Either the control was removed or its fieldKey was renamed."
        )
