# tests/test_foresight_skill_installation.py
# Created: 2026-05-26 (feat/foresight-v12-skill-and-loopback-auth) — RFC 08
# v1.0 wave 4. Verifies the ``foresight-create-sim`` bundled skill ships
# with the right frontmatter shape, lands at the expected mirror path on
# install, and survives the idempotent re-install loop. The installer
# itself is covered by ``test_bundled_skills_installer.py``; this file
# adds the skill-specific assertions so a frontmatter regression (missing
# ``name``, wrong filename, etc.) trips a targeted test.
"""Tests for the ``foresight-create-sim`` bundled skill.

The skill is auto-discovered by the installer's directory iteration —
no code wiring is needed. These tests check the SKILL.md content shape
and the install behavior end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from pocketpaw.bundled_skills.installer import install_bundled_skills

# ---------------------------------------------------------------------------
# Discovery — the installer's directory iteration must pick up the new skill
# ---------------------------------------------------------------------------


def test_foresight_skill_installs_to_destination(tmp_path: Path) -> None:
    """The installer's directory iteration MUST pick up the new
    ``foresight-create-sim`` subdir without any code change. A failed
    discovery here means the SKILL.md is mis-located or the
    ``_bundled/`` parent was renamed."""

    results = install_bundled_skills(destination_root=tmp_path)

    assert any(r.name == "foresight-create-sim" for r in results)

    skill_path = tmp_path / "foresight-create-sim" / "SKILL.md"
    assert skill_path.is_file()


def test_foresight_skill_frontmatter_shape(tmp_path: Path) -> None:
    """The SDK auto-discovery reads YAML frontmatter at the top of
    SKILL.md. ``name`` MUST match the directory; ``description`` MUST
    name the trigger phrases ("rehearse", "simulate", "forecast") so the
    SDK's intent matcher picks the skill up on natural-language
    invocation. A regression that drops one of those triggers would
    silently make the skill non-discoverable in chat."""

    install_bundled_skills(destination_root=tmp_path)

    body = (tmp_path / "foresight-create-sim" / "SKILL.md").read_text()

    # Frontmatter delimiters must be at the very top.
    assert body.startswith("---\n"), "SKILL.md missing leading frontmatter fence"

    # The ``name`` field MUST match the directory name — that's how the
    # SDK keys the skill into its registry.
    assert "\nname: foresight-create-sim\n" in body

    # Description must mention each trigger phrase so the SDK's natural-
    # language matcher fires on the expected user intents.
    for trigger in ("rehearse", "simulate", "forecast"):
        assert trigger in body.lower(), f"missing trigger phrase: {trigger!r}"


# ---------------------------------------------------------------------------
# Content — load-bearing sections that downstream reviewers depend on
# ---------------------------------------------------------------------------


def test_foresight_skill_documents_endpoint_surface(tmp_path: Path) -> None:
    """The skill is the agent's primary doc for the foresight CRUD
    endpoints; if these route stubs drift out of the SKILL.md, the
    agent will guess wrong URLs. Catch that here."""

    install_bundled_skills(destination_root=tmp_path)
    body = (tmp_path / "foresight-create-sim" / "SKILL.md").read_text()

    for endpoint in (
        "/api/v1/foresight/scenarios/custom",
        "/api/v1/foresight/scenarios",
        "/api/v1/foresight/runs",
    ):
        assert endpoint in body, f"SKILL.md missing endpoint doc for {endpoint}"


def test_foresight_skill_lists_three_subtypes(tmp_path: Path) -> None:
    """v1.0 ships three sub_types — ``decision_forecast``, ``market_sim``,
    ``org_change_rehearsal``. The skill MUST enumerate all three so the
    agent can pick the right one from the user's intent. If the engine
    adds a fourth sub_type in a later RFC wave, this test fires and the
    skill body gets an explicit update — drift on the schema is a
    silent failure mode otherwise."""

    install_bundled_skills(destination_root=tmp_path)
    body = (tmp_path / "foresight-create-sim" / "SKILL.md").read_text()

    for sub_type in (
        "decision_forecast",
        "market_sim",
        "org_change_rehearsal",
    ):
        assert sub_type in body, f"SKILL.md missing sub_type: {sub_type}"


def test_foresight_skill_documents_loopback_auth_headers(tmp_path: Path) -> None:
    """The agent uses the loopback header trio to authenticate. If the
    skill body forgets to document any of the three header names, the
    agent will call with the wrong auth shape and get 403s. Pin all
    three header names here."""

    install_bundled_skills(destination_root=tmp_path)
    body = (tmp_path / "foresight-create-sim" / "SKILL.md").read_text()

    for header in (
        "X-PocketPaw-Internal",
        "X-PocketPaw-Workspace-Id",
        "X-PocketPaw-User-Id",
    ):
        assert header in body, f"SKILL.md missing header doc for {header}"


def test_foresight_skill_documents_422_error_envelope(tmp_path: Path) -> None:
    """The cloud's 422 errors carry a ``foresight.invalid_yaml`` /
    ``foresight.sub_type_mismatch`` / ``foresight.invalid_scenario``
    code. The skill MUST teach the agent to surface these verbatim
    rather than swallow them — PR #276 lesson."""

    install_bundled_skills(destination_root=tmp_path)
    body = (tmp_path / "foresight-create-sim" / "SKILL.md").read_text()

    for code in (
        "foresight.invalid_yaml",
        "foresight.sub_type_mismatch",
        "foresight.invalid_scenario",
    ):
        assert code in body, f"SKILL.md missing error code: {code}"


# ---------------------------------------------------------------------------
# Idempotency — re-installing the same skill must be a no-op
# ---------------------------------------------------------------------------


def test_foresight_skill_install_is_idempotent(tmp_path: Path) -> None:
    """First install lands the file with status ``installed``; second
    install with no content drift collapses to ``skipped``. This is the
    steady-state boot — every dashboard restart re-runs the installer."""

    first = install_bundled_skills(destination_root=tmp_path)
    second = install_bundled_skills(destination_root=tmp_path)

    first_result = next(r for r in first if r.name == "foresight-create-sim")
    second_result = next(r for r in second if r.name == "foresight-create-sim")

    assert first_result.status == "installed"
    assert second_result.status == "skipped"


def test_foresight_skill_updates_on_content_drift(tmp_path: Path) -> None:
    """If the user's mirror of the SKILL.md ever drifts from the bundled
    source (older PocketPaw version, hand-edit), the installer
    overwrites it with the canonical bundled body. Status flips to
    ``updated``."""

    skill_dir = tmp_path / "foresight-create-sim"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    stale_marker = "DRIFT_SENTINEL_42"
    skill_file.write_text(f"--- {stale_marker} content from a prior version ---")

    results = install_bundled_skills(destination_root=tmp_path)
    result = next(r for r in results if r.name == "foresight-create-sim")

    assert result.status == "updated"
    body = skill_file.read_text()
    # The drift sentinel was a literal string the installer could not
    # generate on its own — confirms the overwrite happened.
    assert stale_marker not in body
    assert "name: foresight-create-sim" in body
