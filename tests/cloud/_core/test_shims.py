"""Identity tests for the Phase 1 strangler shims.

These assert that ``ee.cloud.shared.<x>`` and ``ee.cloud._core.<x>``
resolve to the same Python object. If a future change accidentally
defines a class in two places, this test catches it before
behavior diverges.
"""

from __future__ import annotations


def test_errors_shim_identity() -> None:
    from pocketpaw_ee.cloud._core import errors as core_errors
    from pocketpaw_ee.cloud.shared import errors as shared_errors

    assert core_errors.CloudError is shared_errors.CloudError
    assert core_errors.NotFound is shared_errors.NotFound
    assert core_errors.Forbidden is shared_errors.Forbidden
    assert core_errors.ConflictError is shared_errors.ConflictError
    assert core_errors.ValidationError is shared_errors.ValidationError
    assert core_errors.SeatLimitError is shared_errors.SeatLimitError


def test_time_shim_identity() -> None:
    from pocketpaw_ee.cloud._core.time import iso_utc as core_iso_utc
    from pocketpaw_ee.cloud.shared.time import iso_utc as shared_iso_utc

    assert core_iso_utc is shared_iso_utc


def test_deps_shim_identity() -> None:
    from pocketpaw_ee.cloud._core import deps as core_deps
    from pocketpaw_ee.cloud.shared import deps as shared_deps

    assert core_deps.current_user is shared_deps.current_user
    assert core_deps.current_user_id is shared_deps.current_user_id
    assert core_deps.current_workspace_id is shared_deps.current_workspace_id
    assert core_deps.optional_workspace_id is shared_deps.optional_workspace_id
    assert core_deps.require_action is shared_deps.require_action
    assert core_deps.require_action_any_workspace is shared_deps.require_action_any_workspace
    assert core_deps.require_membership is shared_deps.require_membership


def test_shared_errors_does_not_re_export_phase0_additions() -> None:
    """RateLimited, Internal, and with_cause are intentionally only in
    _core.errors. Asserts they are NOT exposed via the shared shim."""
    from pocketpaw_ee.cloud.shared import errors as shared_errors

    assert not hasattr(shared_errors, "RateLimited")
    assert not hasattr(shared_errors, "Internal")
    assert not hasattr(shared_errors, "with_cause")


def test_shared_deps_keeps_domain_guards() -> None:
    """The domain-specific guards remain in shared.deps until their
    domains migrate. Phase 1 must not move them."""
    from pocketpaw_ee.cloud.shared import deps as shared_deps

    assert hasattr(shared_deps, "require_group_action")
    assert hasattr(shared_deps, "require_agent_owner_or_admin")
    assert hasattr(shared_deps, "require_pocket_edit")
    assert hasattr(shared_deps, "require_pocket_owner")
