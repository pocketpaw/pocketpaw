# tests/cloud/test_paw_bar_decision_contact.py — the async half of the Paw Bar
# decision loop (2026-07-30).
# Created: 2026-07-30 — a visitor who leaves the page while their request is
# PENDING can leave an email (POST /paw-bar/decision-contact); when the owner
# decides, deliver_customer_decision emails them the SAME customer-facing reply
# the on-page poll returns. Layers:
#   * The public endpoint (httpx + mongomock Site) — shares the concierge armor
#     via _front_gate_for_key: bad key 401, wrong origin 403, sibling pocket
#     403, over-limit 429, unknown widget 404, malformed/oversized email 422;
#     a good post stamps PENDING rows only and returns {ok, attached: N}.
#   * PII posture — the decision poll response NEVER carries the email, and the
#     attach response never echoes it back.
#   * Delivery — a stubbed sender proves exactly ONE email per decision on the
#     PENDING → decided transition (approve AND reject), no re-send on an
#     approve replay, no send without an attached email.
#   * Fail-soft — with no SMTP transport configured the real mailer returns
#     False, nothing raises, and the row still flips (the poll keeps working).
#
# asyncio_mode = "auto" (see pyproject) → async tests need no per-test marker.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.paw_bar.decision_loop import (
    deliver_customer_decision,
    propose_customer_decision,
)

from pocketpaw.instinct.store import InstinctStore
from pocketpaw.paw_bar.models import (
    DecisionState,
    DecisionStatus,
    PawBarBlock,
    PawBarEvent,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore

_VALID_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"
_CUST = "cust-0001"
_EMAIL = "visitor@example.com"


def _spec(pocket_id: str = "pocket-1") -> PawBarSpec:
    return PawBarSpec(
        widget_id="pp_seed",
        pocket_id=pocket_id,
        blocks=[PawBarBlock(type="text", content="Brew & Co")],
    )


def _widget(**ov: Any) -> PawBarWidget:
    d: dict[str, Any] = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=_spec(),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        script_name="",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


def _pending_row(widget_id: str, customer_ref: str = _CUST, **ov: Any) -> DecisionStatus:
    d: dict[str, Any] = dict(
        widget_id=widget_id,
        customer_ref=customer_ref,
        event_type="appointment_request",
        instinct_action_id="",
        workspace_id="ws-1",
        state=DecisionState.PENDING,
    )
    d.update(ov)
    return DecisionStatus(**d)


def _contact_body(widget_id: str, **ov: Any) -> dict[str, Any]:
    b = dict(widget_id=widget_id, signed_key=_VALID_KEY, customer_ref=_CUST, email=_EMAIL)
    b.update(ov)
    return b


@pytest_asyncio.fixture
async def contact_client(tmp_path, mongo_db):
    """Public app client for POST /paw-bar/decision-contact, backed by a tmp
    store (widget + decisions) + Beanie (Site). Yields (client, store).
    Mirrors test_paw_bar_actions.action_client."""
    from unittest.mock import patch

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    store = PawBarStore(tmp_path / "contact.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, store


# --------------------------------------------------------------------------- #
# Layer 1 — the public endpoint's fail-closed gates + the attach itself
# --------------------------------------------------------------------------- #


class TestContactEndpoint:
    async def test_attach_stamps_pending_rows_only(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        # Two pending rows for this visitor + one already-decided one.
        await store.create_decision(_pending_row(widget.id))
        await store.create_decision(_pending_row(widget.id, event_type="paw_bar_action:book"))
        await store.create_decision(
            _pending_row(widget.id, state=DecisionState.DELIVERED, reply="done")
        )

        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200, res.text
        assert res.json() == {"ok": True, "attached": 2}

        rows = await store.list_decisions_for_widget(widget.id)
        pending = [r for r in rows if r.state == DecisionState.PENDING]
        decided = [r for r in rows if r.state == DecisionState.DELIVERED]
        assert all(r.contact_email == _EMAIL for r in pending)
        # The already-decided row was answered on-page — never re-armed.
        assert all(r.contact_email == "" for r in decided)

    async def test_attach_response_never_echoes_the_email(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        await store.create_decision(_pending_row(widget.id))
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200
        assert _EMAIL not in res.text

    async def test_attach_other_visitor_rows_untouched(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        await store.create_decision(_pending_row(widget.id, customer_ref="sibling-visitor"))
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200
        assert res.json()["attached"] == 0
        (row,) = await store.list_decisions_for_widget(widget.id)
        assert row.contact_email == ""

    async def test_unknown_widget_is_404(self, contact_client) -> None:
        client, _store_ = contact_client
        await _site()
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body("pp_ghost"),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 404

    async def test_bad_key_is_401(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id, signed_key="short"),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 401

    async def test_wrong_origin_is_403(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id),
            headers={"Origin": "https://evil.example"},
        )
        assert res.status_code == 403

    async def test_widget_bound_to_sibling_pocket_is_403(self, contact_client) -> None:
        client, store = contact_client
        await _site(pocket_id="pocket-A")
        widget = await store.create_widget(
            _widget(pocket_id="pocket-B", spec=_spec(pocket_id="pocket-B"))
        )
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 403

    async def test_rate_limit_is_429(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget(per_customer_limit_per_min=2))
        for _ in range(2):
            await store.record_event(
                PawBarEvent(widget_id=widget.id, type="concierge_message", customer_ref=_CUST)
            )
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 429

    @pytest.mark.parametrize(
        "bad_email",
        [
            "not-an-email",
            "missing@tld",
            "two@@example.com",
            "spaces in@example.com",
            "a" * 250 + "@example.com",  # over the 254-char cap
            "",
        ],
    )
    async def test_malformed_email_is_422(self, contact_client, bad_email: str) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        await store.create_decision(_pending_row(widget.id))
        res = await client.post(
            "/paw-bar/decision-contact",
            json=_contact_body(widget.id, email=bad_email),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 422, bad_email
        # Nothing was stamped.
        (row,) = await store.list_decisions_for_widget(widget.id)
        assert row.contact_email == ""


# --------------------------------------------------------------------------- #
# Layer 2 — PII posture: the public decision poll never carries the email
# --------------------------------------------------------------------------- #


class TestPollNeverLeaksEmail:
    async def test_poll_response_omits_contact_email(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        await store.create_decision(_pending_row(widget.id, contact_email=_EMAIL))

        res = await client.get(
            f"/paw-bar/events/{widget.id}/decision/{_CUST}",
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["found"] is True
        assert "contact_email" not in body
        assert _EMAIL not in res.text

    async def test_poll_after_delivery_still_omits_email(self, contact_client) -> None:
        client, store = contact_client
        await _site()
        widget = await store.create_widget(_widget())
        await store.create_decision(
            _pending_row(
                widget.id,
                state=DecisionState.DELIVERED,
                reply="Confirmed!",
                contact_email=_EMAIL,
            )
        )
        res = await client.get(
            f"/paw-bar/events/{widget.id}/decision/{_CUST}",
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200
        assert res.json()["reply"] == "Confirmed!"
        assert _EMAIL not in res.text


# --------------------------------------------------------------------------- #
# Layer 3 — delivery sends exactly one email on the PENDING → decided flip
# --------------------------------------------------------------------------- #


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Isolated Instinct + PawBar stores patched onto the lazy-imported
    singletons (mirrors test_paw_bar_decision_loop.stores)."""
    pp_store = PawBarStore(tmp_path / "pb_contact_loop.db")
    instinct_store = InstinctStore(tmp_path / "instinct_contact_loop.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: instinct_store)
    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda: pp_store)
    return pp_store, instinct_store


@pytest.fixture
def sent(monkeypatch):
    """Stub the mailer's send with a recorder; returns the list of sends."""
    calls: list[dict[str, str]] = []

    async def _record(to_addr: str, subject: str, body: str) -> bool:
        calls.append({"to": to_addr, "subject": subject, "body": body})
        return True

    monkeypatch.setattr("pocketpaw_ee.paw_bar.mailer.send_decision_email", _record)
    return calls


async def _open_loop_with_email(pp_store: PawBarStore) -> tuple[PawBarWidget, str]:
    """Create a widget + event, open the loop, attach the visitor's email.
    Returns (widget, instinct_action_id)."""
    widget = await pp_store.create_widget(_widget(workspace_id="", owner="ws:brew"))
    event = PawBarEvent(
        widget_id=widget.id,
        type="appointment_request",
        payload={"when": "Tuesday 3pm"},
        customer_ref=_CUST,
    )
    action_id = await propose_customer_decision(widget=widget, event=event, paw_bar_store=pp_store)
    assert action_id is not None
    attached = await pp_store.attach_contact_email(widget.id, _CUST, _EMAIL, workspace_id="ws:brew")
    assert attached == 1
    return widget, action_id


class TestDeliveryEmail:
    async def test_approve_sends_exactly_one_email(self, stores, sent) -> None:
        pp_store, instinct_store = stores
        widget, action_id = await _open_loop_with_email(pp_store)

        approved = await instinct_store.approve(action_id, approver="user:owner")
        await deliver_customer_decision(approved, declined=False)

        assert len(sent) == 1
        assert sent[0]["to"] == _EMAIL
        # Subject names the site; body is the SAME reply the poll returns.
        assert sent[0]["subject"] == f"Update from {widget.name}"
        row = await pp_store.get_latest_decision(widget.id, _CUST)
        assert row is not None and row.state == DecisionState.DELIVERED
        assert sent[0]["body"] == row.reply
        assert row.reply  # non-empty customer-facing reply

    async def test_replayed_delivery_does_not_resend(self, stores, sent) -> None:
        pp_store, instinct_store = stores
        _widget_, action_id = await _open_loop_with_email(pp_store)

        approved = await instinct_store.approve(action_id, approver="user:owner")
        await deliver_customer_decision(approved, declined=False)
        # A replayed approve delivery (retry / sweep) must NOT email again —
        # the guard is the row's PENDING → decided transition, already spent.
        await deliver_customer_decision(approved, declined=False)
        assert len(sent) == 1

    async def test_reject_sends_the_decline_reply(self, stores, sent) -> None:
        pp_store, instinct_store = stores
        widget, action_id = await _open_loop_with_email(pp_store)

        rejected = await instinct_store.reject(
            action_id, reason="No slots Tuesday — pick another day", rejector="user:owner"
        )
        await deliver_customer_decision(rejected, declined=True)

        assert len(sent) == 1
        assert sent[0]["to"] == _EMAIL
        assert "No slots Tuesday" in sent[0]["body"]
        row = await pp_store.get_latest_decision(widget.id, _CUST)
        assert row is not None and row.state == DecisionState.DECLINED
        assert sent[0]["body"] == row.reply

    async def test_no_attached_email_sends_nothing(self, stores, sent) -> None:
        pp_store, instinct_store = stores
        widget = await pp_store.create_widget(_widget(workspace_id="", owner="ws:brew"))
        event = PawBarEvent(
            widget_id=widget.id,
            type="appointment_request",
            payload={},
            customer_ref=_CUST,
        )
        action_id = await propose_customer_decision(
            widget=widget, event=event, paw_bar_store=pp_store
        )
        approved = await instinct_store.approve(action_id, approver="user:owner")
        await deliver_customer_decision(approved, declined=False)
        assert sent == []
        row = await pp_store.get_latest_decision(widget.id, _CUST)
        assert row is not None and row.state == DecisionState.DELIVERED


# --------------------------------------------------------------------------- #
# Layer 4 — fail-soft: no SMTP transport leaves the whole flow green
# --------------------------------------------------------------------------- #


_SMTP_ENV = (
    "POCKETPAW_SMTP_HOST",
    "POCKETPAW_SMTP_PORT",
    "POCKETPAW_SMTP_USER",
    "POCKETPAW_SMTP_PASSWORD",
    "POCKETPAW_SMTP_FROM",
    "POCKETPAW_SMTP_STARTTLS",
)


class TestFailSoft:
    async def test_no_transport_still_delivers_on_poll(self, stores, monkeypatch) -> None:
        """With NO SMTP env at all the REAL mailer runs, returns False, and the
        approve flow stays green — the row flips and the poll works."""
        for var in _SMTP_ENV:
            monkeypatch.delenv(var, raising=False)
        pp_store, instinct_store = stores
        widget, action_id = await _open_loop_with_email(pp_store)

        approved = await instinct_store.approve(action_id, approver="user:owner")
        await deliver_customer_decision(approved, declined=False)  # must not raise

        row = await pp_store.get_latest_decision(widget.id, _CUST)
        assert row is not None
        assert row.state == DecisionState.DELIVERED
        assert row.reply

    async def test_mailer_unconfigured_returns_false(self, monkeypatch) -> None:
        from pocketpaw_ee.paw_bar import mailer

        for var in _SMTP_ENV:
            monkeypatch.delenv(var, raising=False)
        assert mailer.smtp_configured() is False
        assert await mailer.send_decision_email(_EMAIL, "s", "b") is False

    async def test_mailer_send_error_returns_false(self, monkeypatch) -> None:
        """A configured transport whose send blows up is swallowed to False."""
        from pocketpaw_ee.paw_bar import mailer

        monkeypatch.setenv("POCKETPAW_SMTP_HOST", "smtp.example.test")
        monkeypatch.setenv("POCKETPAW_SMTP_FROM", "noreply@example.test")

        def _boom(*_a: Any, **_k: Any) -> None:
            raise ConnectionRefusedError("no smtp here")

        monkeypatch.setattr(mailer, "_send_sync", _boom)
        assert await mailer.send_decision_email(_EMAIL, "s", "b") is False


# --------------------------------------------------------------------------- #
# Layer 5 — store: additive migration + workspace scoping of the attach
# --------------------------------------------------------------------------- #


class TestStore:
    async def test_attach_is_workspace_scoped(self, tmp_path: Path) -> None:
        store = PawBarStore(tmp_path / "scoped.db")
        await store.create_decision(
            _pending_row("pp_a", workspace_id="ws-a", instinct_action_id="act-1")
        )
        # A cross-tenant attach stamps nothing.
        assert await store.attach_contact_email("pp_a", _CUST, _EMAIL, workspace_id="ws-b") == 0
        # The owning tenant stamps it.
        assert await store.attach_contact_email("pp_a", _CUST, _EMAIL, workspace_id="ws-a") == 1
        row = await store.get_decision_by_action("act-1")
        assert row is not None and row.contact_email == _EMAIL

    async def test_pre_existing_db_gains_contact_email_column(self, tmp_path: Path) -> None:
        """A paw_bar.db created before this slice ALTER-gains the column on the
        next _ensure_schema (same additive pattern as workspace_id)."""
        import aiosqlite

        db_path = tmp_path / "legacy.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE paw_bar_decisions ("
                " id TEXT PRIMARY KEY, widget_id TEXT NOT NULL,"
                " customer_ref TEXT NOT NULL, event_type TEXT DEFAULT '',"
                " instinct_action_id TEXT DEFAULT '', workspace_id TEXT DEFAULT '',"
                " state TEXT DEFAULT 'pending', reply TEXT DEFAULT '',"
                " decided_by TEXT DEFAULT '',"
                " created_at TEXT DEFAULT (datetime('now')),"
                " updated_at TEXT DEFAULT (datetime('now')))"
            )
            await db.execute(
                "INSERT INTO paw_bar_decisions (id, widget_id, customer_ref)"
                " VALUES ('d1', 'pp_a', ?)",
                (_CUST,),
            )
            await db.commit()

        store = PawBarStore(db_path)
        # The legacy row reads back with an empty contact_email…
        row = await store.get_latest_decision("pp_a", _CUST)
        assert row is not None and row.contact_email == ""
        # …and the new column is writable.
        assert await store.attach_contact_email("pp_a", _CUST, _EMAIL) == 1
        row = await store.get_latest_decision("pp_a", _CUST)
        assert row is not None and row.contact_email == _EMAIL
