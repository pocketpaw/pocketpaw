# tests/ee/agent/test_sites_mcp_server/test_ready_means_works_guidance.py — the agent is
# told what "ready" means: forms exercised, no invented contacts, statuses forward only,
# honest sharing, no tool narration.
#
# Each line traces to a defect in the 2026-10-31 persona run: a mailing list that saved
# nothing, "[Address]" on a live café page, a made-up WhatsApp number, "Mark packed"
# after Delivered, an agent that said the link could not be renamed and sent the owner
# to a QR maker that does not exist, and "Refero came back empty" in the chat. The text
# lives where it is loaded: the /sites project loop (every turn), pocketpaw-site (every
# new site), sites-check (before any draft is shown) and sites-app-plan (apps).
from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")


def _skill(name: str) -> str:
    from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

    root = bundled_skills_plugin_dir()
    assert root is not None
    return " ".join((root / "skills" / name / "SKILL.md").read_text("utf-8").split())


def test_the_project_loop_defines_ready() -> None:
    from pocketpaw_ee.cloud.surface.handlers import sites

    loop = " ".join(sites._PROJECT_LOOP.split())
    assert "`exercise_forms: true` passed on each form page" in loop
    assert "Never invent contact details" in loop
    assert "Speak in outcomes, never tool names" in loop


def test_a_refine_turn_knows_how_sharing_works() -> None:
    from pocketpaw_ee.cloud.surface.handlers import sites

    rule = sites._SHARING_RULE
    assert "Settings > General > Address > Change" in rule
    assert "old link stops working" in rule
    assert "no QR maker" in rule
    assert "primary action is the next state" in rule
    assert rule not in sites._PROJECT_LOOP


def test_the_site_skill_exercises_forms_and_names_the_gate() -> None:
    text = _skill("pocketpaw-site")
    assert "`exercise_forms: true` on each page with a form" in text
    assert "refuses placeholders, made-up numbers and broken forms" in text


def test_sites_check_blocks_publish_on_the_three_defects() -> None:
    text = _skill("sites-check")
    assert "Placeholders are always WARN" not in text
    assert "Never a publish block" not in text
    for rule in ("SC-HONEST-07 Placeholders left** (BLOCKS PUBLISH)", "SC-LINK-04"):
        assert rule in text, rule
    assert "A GET back to the page saves nothing" in text
    assert "9876543210" in text
    assert 'never "Mark packed" after Delivered' in text


def test_the_app_plan_skill_says_what_ready_means() -> None:
    text = _skill("sites-app-plan")
    section = text[text.index("## 6. Ready means it works") : text.index("## Checklist")]
    for phrase in (
        "POSTs to a real handler",
        "`exercise_forms: true`",
        "Test entries stay in the draft",
        "Ask for them in the plan question",
        "publishing waits for it",
        "The primary button is the next state only",
        "Settings › General › Address › Change",
        "old link stops working",
        "There is no QR maker for the site link",
    ):
        assert phrase in section, phrase
