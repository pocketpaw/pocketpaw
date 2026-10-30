# Created: 2026-07-10 (feat/verify-mode-shadow) — resolver-precedence unit
#   tests for the three-position Self-Verifying-Loop rollout switches:
#   ``effective_deep_work_verify_mode()`` (OSS executor terminal) and
#   ``effective_cloud_plan_verify_mode()`` (ee/cloud planner terminal).
#   Both mirror the ``effective_spend_mode()`` resolution shape — a
#   non-'off' mode wins outright — EXCEPT the legacy bool maps to
#   'enforce' (its shipped meaning), never to shadow: mapping it to the
#   middle position would silently strip requeue/escalate from a deploy
#   that already set the bool.
"""Precedence tests for the verify_mode resolvers (off | shadow | enforce)."""

from __future__ import annotations

import pytest

from pocketpaw.config import Settings

# Every test passes BOTH the mode and the legacy bool explicitly so a local
# config.json / env can never skew the resolution under test.


class TestDeepWorkVerifyModeResolver:
    """effective_deep_work_verify_mode() — OSS Mission Control terminal."""

    def test_field_default_is_off(self):
        assert Settings.model_fields["deep_work_verify_mode"].default == "off"

    def test_off_plus_bool_false_resolves_off(self):
        s = Settings(deep_work_verify_mode="off", deep_work_verify_loop_enabled=False)
        assert s.effective_deep_work_verify_mode() == "off"

    def test_legacy_bool_true_resolves_enforce_not_shadow(self):
        # BACK-COMPAT SAFETY: the bool's shipped meaning IS the acting loop.
        # Resolving it to 'shadow' would silently weaken any deployment that
        # already set it — so the bool maps to 'enforce', never the middle.
        s = Settings(deep_work_verify_mode="off", deep_work_verify_loop_enabled=True)
        assert s.effective_deep_work_verify_mode() == "enforce"

    def test_explicit_shadow_wins_over_legacy_bool(self):
        s = Settings(deep_work_verify_mode="shadow", deep_work_verify_loop_enabled=True)
        assert s.effective_deep_work_verify_mode() == "shadow"

    def test_explicit_enforce_resolves_without_bool(self):
        s = Settings(deep_work_verify_mode="enforce", deep_work_verify_loop_enabled=False)
        assert s.effective_deep_work_verify_mode() == "enforce"

    def test_invalid_mode_rejected(self):
        with pytest.raises(Exception):
            Settings(deep_work_verify_mode="on")


class TestCloudPlanVerifyModeResolver:
    """effective_cloud_plan_verify_mode() — ee/cloud planner terminal."""

    def test_field_default_is_off(self):
        assert Settings.model_fields["cloud_plan_verify_mode"].default == "off"

    def test_off_plus_bool_false_resolves_off(self):
        s = Settings(cloud_plan_verify_mode="off", cloud_plan_verify_loop_enabled=False)
        assert s.effective_cloud_plan_verify_mode() == "off"

    def test_legacy_bool_true_resolves_enforce_not_shadow(self):
        s = Settings(cloud_plan_verify_mode="off", cloud_plan_verify_loop_enabled=True)
        assert s.effective_cloud_plan_verify_mode() == "enforce"

    def test_explicit_shadow_wins_over_legacy_bool(self):
        s = Settings(cloud_plan_verify_mode="shadow", cloud_plan_verify_loop_enabled=True)
        assert s.effective_cloud_plan_verify_mode() == "shadow"

    def test_explicit_enforce_resolves_without_bool(self):
        s = Settings(cloud_plan_verify_mode="enforce", cloud_plan_verify_loop_enabled=False)
        assert s.effective_cloud_plan_verify_mode() == "enforce"

    def test_terminals_resolve_independently(self):
        # One terminal in shadow must not drag the other along.
        s = Settings(
            deep_work_verify_mode="shadow",
            deep_work_verify_loop_enabled=False,
            cloud_plan_verify_mode="off",
            cloud_plan_verify_loop_enabled=True,
        )
        assert s.effective_deep_work_verify_mode() == "shadow"
        assert s.effective_cloud_plan_verify_mode() == "enforce"
