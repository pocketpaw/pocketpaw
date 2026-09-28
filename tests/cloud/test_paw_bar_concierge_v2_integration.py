# tests/cloud/test_paw_bar_concierge_v2_integration.py — the v2 concierge PR
# stack, combined, with no stand-in modules at the seams between the PRs.
#
# Created 2026-09-28 (chore/concierge-v2-integration). Each CR shipped its own
# tests, and the cross-PR seams were only ever exercised with stand-ins: CR-12's
# delete hooks against a fake ``knowledge_routes``, CR-12's create against a
# gate that did not exist yet, CR-3 and CR-8 each ordering the knowledge without
# the other. Here every seam is the real module:
#   (a) DELETE /concierge runs the real ``delete_faqs`` and ``delete_sources``:
#       the FAQs and the uploaded source are gone, the source is un-indexed, and
#       the site (still v2) serves the dead frame.
#   (b) POST /concierge asks the real ``concierge_gate.default_concierge_runtime``:
#       legacy by default and when v2 is asked for without a report; v2 once the
#       settings ask for it and ``run.promote`` has written a passing report.
#   (c) One v2 turn with pinned FAQs, an uploaded source and an indexed page: the
#       knowledge is FAQs, then the page article, then the KB hits (the
#       integration's ordering decision), in the prompt and in ``sources``, and
#       documentation code grounded in an FAQ passes with allow_doc_code on.
#   (d) The CR-5 spend cap degrades a concierge created through CR-12's route.
#   (e) A site with no concierge (CR-12) is dead at the frame and the chat even
#       when its ``concierge_runtime`` is v2.
# Only the external boundaries are faked: the model (FunctionModel), kb-go reads
# and writes, and the background ingest scheduler.
#
# The fixtures are imported from the CR test modules they belong to, and naming
# one as a test parameter is how pytest injects it, hence the F811 waiver.
# ruff: noqa: F811

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.store import PawBarStore
from tests.cloud.test_paw_bar_concierge_page import _block, _seed_page_article
from tests.cloud.test_paw_bar_concierge_sources import (  # noqa: F401 — fixtures
    caps,
    jobs,
    kb,
)
from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 — fixtures
    _ORIGIN,
    _VALID_KEY,
    _chat,
    _frames,
    _seed_kb,
    model,
)
from tests.cloud.test_paw_bar_concierge_v2_degrade import _pin_cap, _seed_spend

_WS = "ws-1"
_POCKET = "pocket-1"
_OWNER = "user:maya"
_GATE_MODEL = "litellm:claude-eval-model"
_MENU_INDEX = {"menu": {"id": "our-menu", "title": "Our menu"}}
_SHIPPING = "Brew & Co ships whole beans to Canada for a flat $9."
_DOC_CODE = "curl -s https://api.brewco.com/v1/orders -H 'Authorization: Bearer $KEY'"
_ROGUE_CODE = "import os\nos.remove('/etc/hosts')\n"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _app() -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.knowledge_routes import router as knowledge_router
    from pocketpaw_ee.paw_bar.router import router

    from tests.cloud.conftest import override_workspace_role

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.include_router(knowledge_router)
    override_workspace_role(app, role="admin", workspace_id=_WS, user_id=_OWNER)
    return app


@pytest_asyncio.fixture
async def store(tmp_path, mongo_db):  # noqa: ARG001 — mongo_db initialises Beanie
    """One paw-bar store behind every resolver: the router's and the one
    ``agent_provisioning`` mints the widget through."""
    from unittest.mock import patch

    s = PawBarStore(tmp_path / "cv2-integration.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=s):
        yield s


@pytest_asyncio.fixture
async def client(store):  # noqa: ARG001 — the store patch must be live
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        yield c


def _gate_settings(**ov: Any) -> Any:
    from pocketpaw.config import get_settings

    update = {"pawbar_concierge_default_runtime": "v2", "pawbar_concierge_model": _GATE_MODEL}
    update.update(ov)
    return get_settings().model_copy(update=update)


@pytest.fixture
def gate_path(tmp_path, monkeypatch):
    """The committed gate report, moved to a temp file that starts absent."""
    from pocketpaw_ee.paw_bar import concierge_gate

    path = tmp_path / "concierge_eval_gate.json"
    monkeypatch.setattr(concierge_gate, "GATE_REPORT_PATH", path)
    return path


@pytest.fixture
def open_gate(tmp_path, gate_path, monkeypatch):
    """The deployment asks for v2 and ``run --promote`` has written a passing
    real-model report for the configured model: the gate is open."""
    from pocketpaw_ee.paw_bar import concierge_gate

    from tests.evals.concierge import run

    settings = _gate_settings()
    monkeypatch.setattr(concierge_gate, "_settings", lambda: settings)
    report = tmp_path / "real-report.json"
    report.write_text(
        json.dumps(
            {
                "schema": 1,
                "mode": "real",
                "status": "complete",
                "model_spec": _GATE_MODEL,
                "created_at": "2026-09-28T00:00:00+00:00",
                "git_sha": "abc",
                "metrics": {
                    "false_refusal_pct": 0.0,
                    "answer_cases": 12,
                    "groundedness_pct": 100.0,
                    "grounded_cases": 6,
                    "adversarial_held_pct": 100.0,
                    "adversarial_cases": 9,
                    "code_leaks": 0,
                    "cases": 30,
                },
            }
        )
    )
    assert run.promote(report, settings) == []
    assert gate_path.exists()
    return settings


async def _site(**ov: Any):
    """A site with NO concierge yet: CR-12's create is what makes one."""
    from pocketpaw_ee.cloud.models.site import Site

    d: dict[str, Any] = dict(
        workspace=_WS,
        pocket_id=_POCKET,
        owner=_OWNER,
        name="Brew & Co",
        script_name="",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
        url="https://brewco.com",
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


async def _reload(site):
    from pocketpaw_ee.cloud.models.site import Site

    return await Site.find_one({"_id": site.id})


async def _create_live(client, site, **settings: Any) -> dict[str, Any]:
    """The owner's flow: create the concierge, then switch it on."""
    created = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
    assert created.status_code == 201, created.text
    patched = await client.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_enabled": True, **settings},
    )
    assert patched.status_code == 200, patched.text
    return created.json()


async def _widget_for(store) -> Any:
    widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=5)
    assert len(widgets) == 1, widgets
    return widgets[0]


async def _add_faq(client, site, question: str, answer: str) -> str:
    res = await client.post(
        f"/paw-bar/admin/site/{site.id}/knowledge/faqs",
        json={"question": question, "answer": answer},
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _upload(client, site, jobs, kb, article_id: str) -> None:
    kb.next_ids.append(article_id)
    res = await client.post(
        f"/paw-bar/admin/site/{site.id}/knowledge/sources",
        files={"file": ("shipping.txt", _SHIPPING.encode(), "text/plain")},
    )
    assert res.status_code in (201, 202), res.text
    await jobs.run()
    [row] = (await _reload(site)).concierge_sources
    assert row.status == "ready", row
    assert row.article_ids == [article_id]


async def _frame(client, widget_id: str):
    return await client.get(
        "/paw-bar/frame", params={"key": _VALID_KEY, "w": widget_id, "po": _ORIGIN}
    )


def _item_ids(prompt: str) -> list[str]:
    return re.findall(r'<item id="([^"]*)"', _block(prompt, "knowledge"))


def _text(frames: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["content"] for e, d in frames if e == "chunk")


# --------------------------------------------------------------------------- #
# (a) DELETE runs the real CR-8 / CR-9 hooks
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_clears_real_faqs_and_sources_and_the_bar_goes_dead(
    client, store, open_gate, caps, jobs, kb
):
    site = await _site(kb_article_ids=["page-article"])
    await _create_live(client, site)
    await _add_faq(client, site, "Do you ship to Canada?", "Yes, for a flat $9.")
    await _upload(client, site, jobs, kb, "src-art")
    loaded = await _reload(site)
    assert len(loaded.concierge_faqs) == 1 and len(loaded.concierge_sources) == 1

    res = await client.delete(f"/paw-bar/admin/site/{site.id}/concierge")

    assert res.status_code == 200, res.text
    after = await _reload(site)
    assert after.concierge_created_at is None
    assert after.concierge_faqs == []
    assert after.concierge_sources == []
    # The upload's article left the kb; the page sync's own article did not.
    assert kb.removed == [(f"pocket:{_POCKET}", "src-art")]
    faqs = await client.get(f"/paw-bar/admin/site/{site.id}/knowledge/faqs")
    assert faqs.json()["faqs"] == []
    sources = await client.get(f"/paw-bar/admin/site/{site.id}/knowledge/sources")
    assert sources.json()["sources"] == []
    # (e), closing the loop: still a v2 site, but with no concierge the bar is dead.
    assert after.concierge_runtime == "v2"
    widget = await _widget_for(store)
    frame = await _frame(client, widget.id)
    assert frame.status_code == 403
    assert "pawbar:dead" in frame.text


# --------------------------------------------------------------------------- #
# (b) POST consults the real eval gate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_is_legacy_by_default_with_an_agent_bound(client, store, gate_path):
    site = await _site()

    body = await _create_live(client, site)

    assert body["concierge_runtime"] == "legacy"
    assert (await _reload(site)).concierge_runtime == "legacy"
    assert (await _widget_for(store)).agent_id


@pytest.mark.asyncio
async def test_create_asking_for_v2_without_a_report_is_legacy(
    client, store, gate_path, monkeypatch
):
    from pocketpaw_ee.paw_bar import concierge_gate

    settings = _gate_settings()
    monkeypatch.setattr(concierge_gate, "_settings", lambda: settings)
    site = await _site()

    await _create_live(client, site)

    assert not gate_path.exists()
    assert (await _reload(site)).concierge_runtime == "legacy"
    assert (await _widget_for(store)).agent_id


@pytest.mark.asyncio
async def test_create_is_v2_once_the_gate_is_open_and_binds_no_agent(client, store, open_gate):
    site = await _site()

    body = await _create_live(client, site)

    assert body["concierge_runtime"] == "v2"
    assert (await _reload(site)).concierge_runtime == "v2"
    assert (await _widget_for(store)).agent_id == ""


# --------------------------------------------------------------------------- #
# (c) FAQs, then the page article, then KB hits; FAQ-grounded doc code passes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_v2_turn_orders_faqs_then_the_page_then_kb_and_shows_faq_code(
    client, store, open_gate, model, caps, jobs, kb, monkeypatch
):
    from pocketpaw_ee.paw_bar.concierge_runtime import CODE_REPLACEMENT

    site = await _site(kb_page_index=dict(_MENU_INDEX))
    await _create_live(client, site, concierge_allow_doc_code=True)
    faq_code = await _add_faq(client, site, "How do I list my orders?", f"Run this: {_DOC_CODE}")
    faq_hours = await _add_faq(client, site, "Are you open Sundays?", "Yes, from 7:30am.")
    await _upload(client, site, jobs, kb, "src-art")
    # The search returns the upload's article, the page's own article (which must
    # not appear twice) and a page article of the site.
    _seed_kb(
        monkeypatch,
        {
            f"pocket:{_POCKET}": [
                {"id": "src-art", "title": "shipping.txt", "summary": "", "content": _SHIPPING},
                {"id": "our-menu", "title": "Our menu", "summary": "", "content": "Menu."},
                {"id": "site-hours", "title": "Hours", "summary": "", "content": "7:30am."},
            ]
        },
    )
    _seed_page_article(monkeypatch)
    model.reply = [
        "Here you go:\n",
        f"```bash\n{_DOC_CODE}\n```",
        "\nNot this one:\n",
        f"```python\n{_ROGUE_CODE}```",
    ]
    widget = await _widget_for(store)

    res = await _chat(client, widget.id, page={"url": "https://brewco.com/menu", "title": "Menu"})

    assert res.status_code == 200, res.text
    expected = [faq_code, faq_hours, "our-menu", "src-art", "site-hours"]
    assert _item_ids(model.user_prompt()) == expected
    frames = _frames(res.text)
    [sources] = [d for e, d in frames if e == "sources"]
    assert [s["id"] for s in sources["items"]] == expected
    text = _text(frames)
    assert f"```bash\n{_DOC_CODE}\n```" in text
    assert "os.remove" not in text
    assert CODE_REPLACEMENT in text


# --------------------------------------------------------------------------- #
# (d) The spend cap degrades a concierge created through the route
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_spend_cap_degrades_a_created_v2_concierge(
    client, store, open_gate, model, monkeypatch
):
    from pocketpaw_ee.paw_bar.concierge_runtime import DEGRADE_HANDED_OFF

    site = await _site()
    await _create_live(client, site)
    assert (await _reload(site)).concierge_runtime == "v2"
    _seed_kb(monkeypatch, {})
    _pin_cap(monkeypatch, 0.5)
    await _seed_spend(0.5, pocket_id=_POCKET, workspace=_WS)
    widget = await _widget_for(store)

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    frames = _frames(res.text)
    assert [e for e, _d in frames] == ["chunk", "stream_end"]
    assert _text(frames) == DEGRADE_HANDED_OFF
    assert model.calls == []


# --------------------------------------------------------------------------- #
# (e) No concierge means dead, whatever the runtime says
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_v2_site_with_no_concierge_is_dead_at_the_frame_and_the_chat(client, store, model):
    from pocketpaw.paw_bar.models import PawBarBlock, PawBarSpec, PawBarWidget

    site = await _site(concierge_runtime="v2", concierge_enabled=True)
    assert site.concierge_created_at is None
    widget = await store.create_widget(
        PawBarWidget(
            pocket_id=_POCKET,
            owner=_OWNER,
            name="Brew & Co",
            spec=PawBarSpec(
                widget_id="pp_seed",
                pocket_id=_POCKET,
                blocks=[PawBarBlock(type="text", content="Hi")],
            ),
            allowed_domains=["brewco.com"],
            agent_id="",
            workspace_id=_WS,
        )
    )

    frame = await _frame(client, widget.id)
    chat = await _chat(client, widget.id)

    assert frame.status_code == 403
    assert "pawbar:dead" in frame.text
    assert chat.status_code == 403, chat.text
    assert model.calls == []
