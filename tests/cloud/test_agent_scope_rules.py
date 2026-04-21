# tests/cloud/test_agent_scope_rules.py — Unit tests for scope validation +
# assignment authorisation.
# Created: 2026-04-19 (feat/cluster-d-agent-scope-picker) — Covers the
# grammar rules mirrored from the frontend normaliseScope helper, the
# forbidden-universal-wildcard rule that's server-only, and the
# admin_can_assign_scopes containment check used when a scope-narrowed
# admin tries to assign a scope outside their own grant.

from __future__ import annotations

import pytest

from ee.cloud.agents.scope_rules import (
    FORBIDDEN_SCOPES,
    ScopeValidationError,
    admin_can_assign_scopes,
    normalise_and_validate_scopes,
)


class TestNormaliseAndValidateScopes:
    def test_empty_list_returns_empty(self):
        assert normalise_and_validate_scopes([]) == []

    def test_lowercase_and_strip(self):
        out = normalise_and_validate_scopes(["  Org:Sales:Leads  "])
        assert out == ["org:sales:leads"]

    def test_dedupe_preserves_order(self):
        out = normalise_and_validate_scopes(
            ["org:sales:*", "org:sales:*", "org:marketing"]
        )
        assert out == ["org:sales:*", "org:marketing"]

    def test_universal_wildcard_rejected(self):
        with pytest.raises(ScopeValidationError) as exc:
            normalise_and_validate_scopes(["*"])
        assert "not assignable" in str(exc.value)

    def test_forbidden_scopes_includes_universal(self):
        assert "*" in FORBIDDEN_SCOPES

    def test_namespaced_wildcard_accepted(self):
        out = normalise_and_validate_scopes(["org:sales:*"])
        assert out == ["org:sales:*"]

    def test_mid_segment_wildcard_rejected(self):
        with pytest.raises(ScopeValidationError) as exc:
            normalise_and_validate_scopes(["org:*:leads"])
        assert "mid-segment wildcard" in str(exc.value)

    def test_empty_segment_rejected(self):
        with pytest.raises(ScopeValidationError):
            normalise_and_validate_scopes(["org::leads"])

    def test_leading_colon_rejected(self):
        with pytest.raises(ScopeValidationError):
            normalise_and_validate_scopes([":leads"])

    def test_uppercase_mixed_segment_rejected_after_normalise(self):
        # Weird chars that survive lowercase still fail the [a-z0-9]+ rule.
        with pytest.raises(ScopeValidationError):
            normalise_and_validate_scopes(["org:sales-team"])

    def test_non_string_rejected(self):
        with pytest.raises(ScopeValidationError):
            normalise_and_validate_scopes([123])  # type: ignore[list-item]

    def test_empty_string_rejected(self):
        with pytest.raises(ScopeValidationError):
            normalise_and_validate_scopes([""])


class TestAdminCanAssignScopes:
    def test_empty_admin_scope_permits_anything_non_forbidden(self):
        # Admins without an explicit scope narrowing sit at workspace root.
        assert admin_can_assign_scopes(None, ["org:sales:leads"]) is True
        assert admin_can_assign_scopes([], ["org:sales:*"]) is True

    def test_empty_request_is_always_allowed(self):
        # Clearing scopes on an agent (assigning []) is always fine.
        assert admin_can_assign_scopes(["org:sales:*"], []) is True

    def test_exact_match_allowed(self):
        assert admin_can_assign_scopes(["org:sales:leads"], ["org:sales:leads"]) is True

    def test_glob_admin_covers_descendant(self):
        assert admin_can_assign_scopes(["org:sales:*"], ["org:sales:leads"]) is True

    def test_glob_admin_covers_itself(self):
        assert admin_can_assign_scopes(["org:sales:*"], ["org:sales"]) is True

    def test_admin_cannot_escape_own_scope(self):
        # Admin scoped to sales cannot assign the agent to marketing.
        assert admin_can_assign_scopes(["org:sales:*"], ["org:marketing:leads"]) is False

    def test_admin_cannot_assign_wider_glob(self):
        # Admin scoped to org:sales:leads cannot assign org:sales:* (wider).
        assert admin_can_assign_scopes(["org:sales:leads"], ["org:sales:*"]) is False

    def test_partial_overlap_rejected(self):
        # Any request scope outside admin's grant flips the whole check.
        granted = ["org:sales:*"]
        requested = ["org:sales:leads", "org:marketing:emails"]
        assert admin_can_assign_scopes(granted, requested) is False

    def test_multi_admin_scopes_cover_union(self):
        granted = ["org:sales:*", "org:marketing:*"]
        requested = ["org:sales:leads", "org:marketing:emails"]
        assert admin_can_assign_scopes(granted, requested) is True

    def test_wildcard_admin_covers_all(self):
        assert admin_can_assign_scopes(["*"], ["org:anything:here"]) is True
